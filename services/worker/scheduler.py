
"""Scheduler — config-driven task dispatch for content_fetch and live_record.

- content_fetch: timer-based dispatch (every N seconds/minutes/hours).
- live_record:   tier-driven event dispatch. Accounts are classified into
  tiers by idle time on startup.  Per-account asyncio timers fire when
  it's time to check again.  Recording accounts are moved to a separate
  set and exempted from checking.  No polling, no pubsub storm.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy import select

from packages.core.config import ConfigLoader
from packages.core.config.models import parse_duration_to_seconds
from packages.core.config.watcher import watch_config_dir
from packages.core.db.models import Account, Creator, Task, TaskRun
from packages.core.db.urls import redis_get_url
from packages.core.events import publish_event
from packages.core.sync import sync_creators_to_db
from packages.core.db import db_get_session_factory

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"

# Module-level event queue for live signals: ("timer", account_id) when a
# per-account check timer fires, ("done", account_id) when a recording ends.
# Consumed by _handle_live_events — pure event-driven, no polling.
_live_event_queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()


def notify_live_done(account_id: int) -> None:
    """Called by any task (consumer, background_record) when a live
    recording session finishes for an account."""
    _live_event_queue.put_nowait(("done", account_id))


def _fs_supports_inotify() -> bool:
    """Check if the config directory is on a filesystem that supports inotify.

    9p (Docker Desktop for Windows/Mac) does NOT support inotify; native
    Linux bind mounts (NAS, Linux Docker) do.
    """
    try:
        resolved = str(CONFIG_DIR.resolve())
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    mount_point = parts[1]
                    fs_type = parts[2]
                    # Check if config dir is on this mount
                    if resolved == mount_point or resolved.startswith(mount_point + "/"):
                        return fs_type != "9p"
        # If /proc/mounts not available, assume yes (will behave correctly)
        return True
    except Exception:
        return True


class Scheduler:
    """Manages a set of per-task schedulers with config hot-reload."""

    def __init__(self) -> None:
        self._timers: dict[int, asyncio.TimerHandle] = {}
        self._live_tasks: list[asyncio.Task] = []
        self._redis: Redis | None = None

        # ── live tier-driven architecture ─────────────────────────
        # Tier name (e.g. "30s", "5m") → set of account_ids
        self._live_tier_groups: dict[str, set[int]] = {}
        # Accounts currently being recorded (exempt from checking)
        self._live_recording: set[int] = set()
        # Per-account timer handles
        self._live_timers: dict[int, asyncio.TimerHandle] = {}
        # Live schedule index (from base.jsonc tasks[]) for queue key
        self._live_idx: int = -1
        # Cached live adapt_cfg for timer dispatch
        self._live_adapt_cfg = None
        # Shortest tier interval (first tier after=0d)
        self._live_shortest_tier: str = "30s"
        # Live strategy tick from config
        self._live_tick: str = "1s"
        # Pre-resolved per-platform (tick, jitter) for live_record dispatch
        self._live_resolved: dict[str, tuple[str, tuple[str, str]]] = {}

    # ?? lifecycle ????????????????????????????????????????????????

    async def start(self) -> None:
        self._redis = Redis.from_url(redis_get_url(), decode_responses=True)
        _state = ConfigLoader(CONFIG_DIR).load_all()

        # Sync creators.jsonc -> DB so live accounts are available
        try:
            session_factory = db_get_session_factory()
            await sync_creators_to_db(session_factory, _state.creators)
        except Exception:
            logger.exception("[scheduler] failed to sync creators to DB")

        self._register_all(_state)
        self._cache_live_config(_state)
        # Init live tier groups and start live dispatch loop
        await self._init_live_tiers(_state)
        self._live_tasks.append(asyncio.create_task(self._handle_live_events()))
        # Start file watcher for config hot-reload
        config_queue, self._observer = await watch_config_dir(CONFIG_DIR)
        asyncio.create_task(self._handle_config_events(config_queue))

    async def stop(self) -> None:
        for h in self._timers.values():
            h.cancel()
        self._timers.clear()
        for h in self._live_timers.values():
            h.cancel()
        self._live_timers.clear()
        for t in self._live_tasks:
            t.cancel()
        self._live_tasks.clear()
        if hasattr(self, "_observer"):
            self._observer.stop()
            self._observer.join(timeout=5)
        if self._redis:
            await self._redis.aclose()

    # ?? config watcher ????????????????????????????????????????????

    async def _handle_config_events(self, queue: asyncio.Queue[str]) -> None:
        """Watchdog watcher with polling fallback for 9p filesystems."""
        use_polling = not _fs_supports_inotify()
        if use_polling:
            logger.info("[scheduler] 9p filesystem detected — using polling fallback (5s)")

        # Use second-level mtime for change detection (nanosecond precision is
        # unstable on WSL2/Windows volume mounts).
        _mtimes: dict[str, float] = {}
        _last_reload: float = 0.0
        _min_reload_interval: float = 10.0

        while True:
            try:
                now = time.monotonic()
                if not use_polling:
                    # Pure event-driven: wait for watchdog
                    await queue.get()
                    while True:
                        try:
                            await asyncio.wait_for(queue.get(), timeout=0.5)
                        except (asyncio.TimeoutError, asyncio.QueueEmpty):
                            break
                    if now - _last_reload >= _min_reload_interval:
                        _last_reload = time.monotonic()
                        await self._reload()
                else:
                    # Polling fallback: check every 5s
                    try:
                        await asyncio.wait_for(queue.get(), timeout=5.0)
                        while True:
                            try:
                                await asyncio.wait_for(queue.get(), timeout=0.5)
                            except (asyncio.TimeoutError, asyncio.QueueEmpty):
                                break
                        if now - _last_reload >= _min_reload_interval:
                            _last_reload = time.monotonic()
                            await self._reload()
                        continue
                    except asyncio.TimeoutError:
                        pass

                    for fname in ("base.jsonc",):
                        fpath = CONFIG_DIR / fname
                        if not fpath.exists():
                            continue
                        # Use second-level mtime — nanosecond precision is
                        # unstable on WSL2/Windows volume mounts.
                        mtime = fpath.stat().st_mtime  # float seconds
                        if _mtimes.get(fname) and abs(mtime - _mtimes[fname]) > 0.5:
                            if now - _last_reload >= _min_reload_interval:
                                logger.info("[scheduler] config file changed: %s", fname)
                                _last_reload = time.monotonic()
                                await self._reload()
                            break
                        _mtimes[fname] = mtime
            except Exception:
                await asyncio.sleep(1)

    async def _reload(self) -> None:
        logger.info("[scheduler] config changed — reloading")
        state = ConfigLoader(CONFIG_DIR).load_all()

        # Sync creators.jsonc -> DB so new accounts are picked up
        try:
            session_factory = db_get_session_factory()
            await sync_creators_to_db(session_factory, state.creators)
        except Exception:
            logger.exception("[scheduler] failed to sync creators to DB")

        # Cancel all timer-based tasks
        for h in self._timers.values():
            h.cancel()
        self._timers.clear()
        # Cancel live timers and re-init tiers
        for h in self._live_timers.values():
            h.cancel()
        self._live_timers.clear()
        self._live_tier_groups.clear()
        self._live_recording.clear()
        self._register_all(state)
        self._cache_live_config(state)
        await self._init_live_tiers(state)

    # ?? timer management ??????????????????????????????????????????

    def _register_all(self, state) -> None:
        for idx, entry in enumerate(state.base.schedules):
            if not entry.enabled:
                continue
            if entry.type == "live_record":
                continue  # managed by tier-driven _live_dispatch_loop
            if not entry.interval:
                continue
            self._schedule_next(idx, entry, state, first=True)

    def _schedule_next(self, idx: int, entry, state, *, first: bool = False) -> None:
        interval_secs = self._parse_interval(entry.interval)
        if first and entry.start_at:
            # Calculate delay until next occurrence of start_at (HH:MM)
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            target = datetime.strptime(f"{today} {entry.start_at}", "%Y-%m-%d %H:%M")
            if target <= now:
                # Today's window already passed — schedule for tomorrow
                target += timedelta(days=1)
            delay = (target - now).total_seconds()
            logger.info("[scheduler] task_%d: first dispatch at %s (in %.0fs)", idx, target.strftime("%H:%M"), delay)
        else:
            delay = interval_secs if not first else 0.0
        loop = asyncio.get_event_loop()
        h = loop.call_later(
            delay,
            lambda: asyncio.create_task(self._dispatch(idx, entry, state)),
        )
        self._timers[idx] = h

    @staticmethod
    def _parse_interval(s: str) -> int:
        try:
            return int(parse_duration_to_seconds(s))
        except ValueError:
            raise ValueError(f"Invalid interval: {s!r}")

    # ── live tier-driven architecture ────────────────────────────
    # No polling, no event-driven dispatch storm.
    # Accounts are classified into tiers by idle time on startup.
    # Per-account timers fire when it's time to check again.
    # Recording accounts are moved to _live_recording (exempt).
    # ─────────────────────────────────────────────────────────────

    def _cache_live_config(self, state) -> None:
        """Cache live schedule index and config from base.jsonc."""
        for idx, entry in enumerate(state.base.schedules):
            if entry.type == "live_record" and entry.enabled:
                self._live_idx = idx
                self._live_adapt_cfg = self._resolve_adapt_cfg(entry, state)
                # Shortest tier = first tier with after=0d
                if self._live_adapt_cfg and self._live_adapt_cfg.tiers:
                    self._live_shortest_tier = self._live_adapt_cfg.tiers[0].interval
                # Pre-resolve per-platform tick/jitter for live dispatch
                # Priority: entry.platforms > entry.request > site request > hardcoded
                r = entry.request
                self._live_resolved = {}
                for platform, site_cfg in state.sites.items():
                    site_req = site_cfg.get("request") or {}
                    pr = (r.platforms.get(platform) if r and r.platforms else None)
                    tick = (pr.tick if pr and pr.tick else None) or \
                           (r.tick if r and r.tick else None) or \
                           site_req.get("tick") or "1s"
                    jitter = (pr.jitter if pr and pr.jitter else None) or \
                             (r.jitter if r and r.jitter else None) or \
                             tuple(site_req.get("jitter", ("0s", "0.5s")))
                    self._live_resolved[platform] = (tick, jitter)
                break

    @staticmethod
    def _resolve_adapt_cfg(entry, state) -> any:
        strat = entry.strategy
        if strat and strat.adaptive:
            return strat.adaptive
        return getattr(getattr(state.base.strategy, "live", None), "adaptive", None)

    def _tier_to_interval(self, tier: str) -> float:
        """Convert a tier name like '30s' to seconds."""
        return float(self._parse_interval(tier))

    def _tier_for_idle(self, idle_seconds: float, adapt_cfg) -> str:
        """Return the tier name for a given idle duration."""
        if adapt_cfg is None or not adapt_cfg.enabled or not adapt_cfg.tiers:
            raise ValueError("live_record schedule requires strategy.live.adaptive with at least one tier")
        def _to_secs(s: str) -> int:
            try:
                return int(parse_duration_to_seconds(s))
            except ValueError:
                return 0
        best_tier = adapt_cfg.tiers[0].interval
        for t in adapt_cfg.tiers:
            after_secs = _to_secs(t.after)
            if idle_seconds >= after_secs:
                best_tier = t.interval
        return best_tier

    def _start_live_timer(self, account_id: int, delay: float) -> None:
        """Start or restart the per-account check timer."""
        self._cancel_live_timer(account_id)
        h = asyncio.get_event_loop().call_later(
            delay,
            lambda: _live_event_queue.put_nowait(("timer", account_id)),
        )
        self._live_timers[account_id] = h

    def _cancel_live_timer(self, account_id: int) -> None:
        h = self._live_timers.pop(account_id, None)
        if h is not None:
            h.cancel()

    async def _init_live_tiers(self, state) -> None:
        """Classify all live accounts into tier groups on startup/reload."""
        self._live_tier_groups.clear()
        self._live_recording.clear()
        # Cancel all existing timers
        for h in self._live_timers.values():
            h.cancel()
        self._live_timers.clear()

        session_factory = db_get_session_factory()
        query = select(Account.id).where(
            Account.account_type == "live",
            Account.scheduled == True,
        )
        async with session_factory() as session:
            all_ids = [row[0] for row in (await session.execute(query)).all()]
        if not all_ids:
            return

        # If live_record schedule is disabled, don't start any timers
        if self._live_idx < 0 or self._live_adapt_cfg is None:
            logger.info("[scheduler] live_record schedule disabled — no live timers started")
            return

        # Batch read last_live timestamps from Redis
        idle_keys = [f"polycrawl:live:last_live:{a}" for a in all_ids]
        idle_raw = await self._redis.mget(idle_keys)
        idle_map: dict[int, int] = {}
        missing_ids: list[int] = []
        for i, aid in enumerate(all_ids):
            v = idle_raw[i] if i < len(idle_raw) else None
            if v:
                idle_map[aid] = int(float(v))
            else:
                missing_ids.append(aid)

        # DB fallback for missing
        if missing_ids:
            from packages.core.db.models import LiveSession
            async with session_factory() as session:
                rows = await session.execute(
                    select(LiveSession.account_id, LiveSession.started_at)
                    .where(
                        LiveSession.account_id.in_(missing_ids),
                        LiveSession.status == "completed",
                    )
                    .distinct(LiveSession.account_id)
                    .order_by(LiveSession.account_id, LiveSession.started_at.desc())
                )
                for aid, ts in rows:
                    idle_map[aid] = int(ts.timestamp())
                    await self._redis.set(f"polycrawl:live:last_live:{aid}", str(idle_map[aid]))

        adapt_cfg = self._live_adapt_cfg
        now = time.time()
        for account_id in all_ids:
            idle_ts = idle_map.get(account_id, 0) or 0
            # No history → start from shortest tier, escalate naturally over time
            tier = self._tier_for_idle(now - idle_ts, adapt_cfg) if idle_ts > 0 else self._live_shortest_tier
            self._live_tier_groups.setdefault(tier, set()).add(account_id)
            # First check immediately for newly added accounts; delay=interval for others
            first_delay = 0.0 if idle_ts == 0 else self._tier_to_interval(tier)
            self._start_live_timer(account_id, first_delay)

        logger.info(
            "[scheduler] live tiers initialized: %s",
            {k: len(v) for k, v in self._live_tier_groups.items()},
        )

    async def _handle_live_events(self) -> None:
        """Pure event-driven dispatch for live_record tasks.

        Blocks on _live_event_queue.  Events come from:
          - ``("timer", aid)`` — per-account check timer expired
          - ``("done", aid)``  — recording ended (success or failure)
        No polling, no asyncio.sleep.
        """
        session_factory = db_get_session_factory()
        while True:
            try:
                # Block until the first event arrives, then drain any backlog.
                kind, aid = await _live_event_queue.get()
                events = [(kind, aid)]
                while not _live_event_queue.empty():
                    events.append(_live_event_queue.get_nowait())

                done_ids: list[int] = []
                timer_ids: list[int] = []
                for kind, aid in events:
                    if kind == "done":
                        self._live_recording.discard(aid)
                        done_ids.append(aid)
                    else:
                        timer_ids.append(aid)

                # Process recording-ended: re-classify + start timer
                for aid in done_ids:
                    raw = await self._redis.get(f"polycrawl:live:last_live:{aid}")
                    idle_ts = int(float(raw)) if raw else 0
                    now = time.time()
                    tier = self._tier_for_idle(now - idle_ts, self._live_adapt_cfg) if idle_ts > 0 else self._live_shortest_tier
                    self._live_tier_groups.setdefault(tier, set()).add(aid)
                    interval = self._tier_to_interval(tier)
                    self._start_live_timer(aid, interval)
                    logger.info("[scheduler] live recording ended account=%d → tier=%s", aid, tier)

                # Process timer expiries: dispatch accounts not currently recording
                to_check = [aid for aid in timer_ids if aid not in self._live_recording]
                if not to_check:
                    continue
                # Don't dispatch if live_record schedule is disabled
                if self._live_idx < 0 or self._live_adapt_cfg is None:
                    logger.debug("[scheduler] live_record disabled — skipping dispatch")
                    # Still re-arm timers so we keep checking (they'll re-check on re-enable)
                    # Actually don't — re-enable triggers _reload which re-starts everything
                    continue
                for account_id in to_check:
                    async with session_factory() as session:
                        acc = await session.get(Account, account_id)
                        plat = acc.platform if acc else None
                        rcfg = self._live_resolved.get(plat) if plat else None
                        live_tick = rcfg[0] if rcfg else "1s"
                        raw_jitter = rcfg[1] if rcfg else ("0s", "0.5s")
                        params_dict = {"tick": live_tick, "account_id": account_id}
                        if raw_jitter and len(raw_jitter) == 2:
                            params_dict["jitter"] = [
                                parse_duration_to_seconds(raw_jitter[0]),
                                parse_duration_to_seconds(raw_jitter[1]),
                            ]
                        task = Task(
                            account_id=account_id,
                            task_type="live_record",
                            status="pending",
                            params=params_dict,
                            max_retries=3,
                        )
                        session.add(task)
                        await session.flush()
                        msg = json.dumps({
                            "task_id": str(task.id),
                            "account_id": account_id,
                            "task_type": "live_record",
                            "params": task.params,
                        })
                        await session.commit()
                    await self._redis.rpush(self._queue_key(self._live_idx), msg)
                    logger.debug("[scheduler] live dispatched account=%d", account_id)
                    # Mark as recording to prevent re-dispatch until done
                    self._live_recording.add(account_id)
                    # Re-arm timer for next check
                    self._start_live_timer(account_id, self._tier_to_interval(self._live_shortest_tier))

            except Exception:
                logger.exception("[scheduler] live dispatch error")

    # dispatch (content_fetch only)
    # Live_record no longer uses this path; see _live_dispatch_loop.

    async def _dispatch(self, idx: int, entry, state) -> None:
        try:
            await self._dispatch_generic(idx, entry, state)
        except Exception:
            logger.exception("[scheduler] task_%d dispatch failed", idx)
        finally:
            self._schedule_next(idx, entry, state)

    # ?? adaptive helpers ??????????????????????????????????????????

    @staticmethod
    def _resolve_adaptive_interval(
        idle_seconds: float,
        adapt_cfg,
    ) -> float:
        if adapt_cfg is None or not adapt_cfg.enabled or not adapt_cfg.tiers:
            return 0.0
        def _to_secs(s: str) -> int:
            try:
                return int(parse_duration_to_seconds(s))
            except ValueError:
                return 0
        best = 0.0
        for tier in adapt_cfg.tiers:
            after_secs = _to_secs(tier.after)
            if idle_seconds >= after_secs:
                best = float(_to_secs(tier.interval))
        return best

    # ?? generic dispatch (content_fetch only) ???????????????????????
    # live_record dispatch now handled by _live_dispatch_loop above.

    async def _dispatch_generic(self, idx: int, entry, state) -> None:
        """Unified dispatch for content_fetch tasks — auto-split by platform.

        Groups accounts by platform and dispatches each platform to an
        independent queue (``task_{idx}:{platform}``) with its own in-flight
        gate, so platforms never block each other.
        """
        session_factory = db_get_session_factory()

        # ── type-specific parameters ─────────────────────────────
        account_type = "profile"
        task_type = "content_fetch"
        strategy_cfg = state.base.strategy.incremental
        idle_key_prefix = "polycrawl:adaptive:idle:"
        check_key_prefix = "polycrawl:adaptive:last_check:"
        extra_params = {"strategy": entry.strategy.use} if entry.strategy else {}
        strat = entry.strategy
        adapt_cfg = strat.adaptive if strat and strat.adaptive else strategy_cfg.adaptive

        # ── resolve accounts ────────────────────────────────────
        query = select(Account.id, Account.platform).where(
            Account.account_type == account_type,
            Account.scheduled == True,
        )
        if entry.tag_filter:
            matching_keys = {
                c.creator_key for c in state.creators.creators
                if c.tags and all(tag in c.tags for tag in entry.tag_filter)
            }
            if matching_keys:
                query = query.join(Creator, Account.creator_id == Creator.id).where(
                    Creator.creator_key.in_(matching_keys)
                )
            else:
                logger.info("[scheduler] task_%d: no creators match tag_filter", idx)
                return

        async with session_factory() as session:
            all_accounts = [(row[0], row[1]) for row in (await session.execute(query)).all()]

        if not all_accounts:
            logger.info("[scheduler] task_%d: no %s accounts", idx, account_type)
            return

        # ── group by platform ────────────────────────────────────
        platform_groups: dict[str, list[int]] = {}
        for aid, plat in all_accounts:
            platform_groups.setdefault(plat, []).append(aid)

        total_dispatched = 0
        total_skipped = 0
        for platform, account_ids in platform_groups.items():
            d, s = await self._dispatch_platform_batch(
                idx, platform, account_ids, entry, state,
                session_factory, task_type, adapt_cfg,
                idle_key_prefix, check_key_prefix, extra_params,
            )
            total_dispatched += d
            total_skipped += s

        logger.info(
            "[scheduler] task_%d: dispatched=%d skipped_adaptive=%d (%s)",
            idx, total_dispatched, total_skipped, task_type,
        )

        # Notify frontend
        try:
            from packages.core.db.urls import redis_get_url
            publish_event("task_created", {})
        except Exception:
            pass

    async def _dispatch_platform_batch(
        self, idx: int, platform: str, account_ids: list[int],
        entry, state, session_factory, task_type: str, adapt_cfg,
        idle_key_prefix: str, check_key_prefix: str, extra_params: dict,
    ) -> tuple[int, int]:
        """Dispatch accounts of a single platform to ``task_{idx}:{platform}``.

        Returns (dispatched, skipped_adaptive).
        """
        # ── per-platform in-flight gate ──────────────────────────
        # Only blocks if THIS platform has pending/running tasks.
        async with session_factory() as session:
            result = await session.execute(
                select(Task.id).join(Account, Task.account_id == Account.id).where(
                    Task.task_type == task_type,
                    Account.platform == platform,
                    Task.status.in_(["pending", "running"]),
                ).limit(1)
            )
            if result.first() is not None:
                logger.info("[scheduler] task_%d: %s in-flight, skip this tick", idx, platform)
                return 0, 0

        queue_key = self._queue_key(idx, platform)
        dispatched = 0
        skipped_adaptive = 0

        # ── batch: read all Redis timestamps at once ──────────────
        idle_map: dict[int, int] = {}
        check_map: dict[int, int] = {}
        if adapt_cfg.enabled and adapt_cfg.tiers:
            idle_keys = [f"{idle_key_prefix}{a}" for a in account_ids]
            check_keys = [f"{check_key_prefix}{a}" for a in account_ids]
            idle_raw = await self._redis.mget(idle_keys) if idle_keys else []
            check_raw = await self._redis.mget(check_keys) if check_keys else []
            for i, aid in enumerate(account_ids):
                v = idle_raw[i] if i < len(idle_raw) else None
                idle_map[aid] = int(float(v)) if v else 0
                v = check_raw[i] if i < len(check_raw) else None
                check_map[aid] = int(float(v)) if v else 0

        now = time.time()
        pipeline = self._redis.pipeline()
        tasks_to_create: list[Task] = []

        # ── site config for this platform (resolved once) ─────────
        site_cfg = state.sites.get(platform, {})
        req_site = site_cfg.get("request") or {}
        r = entry.request
        default_tick = req_site.get("tick") or "1s"
        default_jitter = req_site.get("jitter") or ("0s", "0s")

        # Per-platform task override (task entry > site config > hardcoded default)
        platform_req = r.platforms.get(platform) if r and r.platforms else None

        for account_id in account_ids:
            # resolve tick & jitter
            tick = (platform_req.tick if platform_req and platform_req.tick else None) or \
                   (r.tick if r and r.tick else None) or default_tick
            raw_jitter = (platform_req.jitter if platform_req and platform_req.jitter else None) or \
                         (r.jitter if r and r.jitter else None) or default_jitter

            # ── adaptive skip ────────────────────────────────────
            if adapt_cfg.enabled and adapt_cfg.tiers:
                idle_ts = idle_map.get(account_id, 0)
                if idle_ts == 0:
                    idle_ts = int(now)
                idle_secs = now - idle_ts
                min_interval = self._resolve_adaptive_interval(idle_secs, adapt_cfg)
                if min_interval > 0:
                    last_check = check_map.get(account_id, 0)
                    if now - last_check < min_interval:
                        skipped_adaptive += 1
                        continue

            # ── cursor ──────────────────────────────────────────
            params: dict = {"tick": tick, "account_id": account_id, **extra_params}
            cursor_key = f"polycrawl:incremental:cursor:{account_id}"
            raw_cursor = await self._redis.get(cursor_key)
            if raw_cursor is not None and raw_cursor != b"0" and raw_cursor != "0":
                params["cursor"] = raw_cursor.decode() if isinstance(raw_cursor, bytes) else str(raw_cursor)
            else:
                params["cursor"] = ""
            if raw_jitter and len(raw_jitter) == 2:
                params["jitter"] = [parse_duration_to_seconds(raw_jitter[0]), parse_duration_to_seconds(raw_jitter[1])]

            # ── batch task record ────────────────────────────────
            tasks_to_create.append(Task(
                account_id=account_id,
                task_type=task_type,
                status="pending",
                params=params,
                max_retries=3,
            ))

            # ── batch last_check update ──────────────────────────
            if adapt_cfg.enabled and adapt_cfg.tiers:
                pipeline.set(f"{check_key_prefix}{account_id}", str(int(now)))

            dispatched += 1

        # ── flush DB creates + Redis pushes in one batch ────────
        if tasks_to_create:
            async with session_factory() as session:
                for task in tasks_to_create:
                    session.add(task)
                await session.flush()
                for task in tasks_to_create:
                    msg = json.dumps({
                        "task_id": str(task.id),
                        "account_id": task.account_id,
                        "task_type": task_type,
                        "params": task.params,
                    })
                    pipeline.rpush(queue_key, msg)
                await session.commit()
            await pipeline.execute()

        return dispatched, skipped_adaptive

    @staticmethod
    def _queue_key(task_idx: int, platform: str | None = None) -> str:
        if platform:
            return f"task_{task_idx}:{platform}"
        return f"task_{task_idx}"
