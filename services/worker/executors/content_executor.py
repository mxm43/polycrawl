from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from sqlalchemy import and_, select

from packages.core.providers.registry import ProviderRegistry
from packages.core.db.models import Account, Artifact, Creator, Task
from packages.core.events import publish_event
from packages.core.utils import build_creator_dir, parse_publish_date
from packages.core.db import db_get_session_factory
from packages.core.db.urls import redis_get_url
from services.worker.runtime import get_media_root

logger = logging.getLogger(__name__)


@dataclass
class ContentExecutionResult:
    items_fetched: int = 0
    items_downloaded: int = 0
    items_skipped: int = 0
    bytes_downloaded: int = 0


# Per-platform download rate limiting (keyed by platform name).
_download_last_ts: dict[str, float] = {}


async def _download_rate_limit(platform: str, rps: float) -> None:
    """Wait if needed to enforce per-platform download requests per second."""
    if rps <= 0:
        return
    interval = 1.0 / max(rps, 0.1)
    now = time.monotonic()
    last = _download_last_ts.get(platform, 0.0)
    wait = interval - (now - last)
    if wait > 0:
        await asyncio.sleep(wait)
    _download_last_ts[platform] = time.monotonic()


def _record_adaptive_state(account_id: int, downloaded: int) -> None:
    """Update Redis adaptive idle/active tracking after a fetch run."""
    try:
        from packages.core.db.urls import redis_get_url
        from redis import Redis
        r = Redis.from_url(redis_get_url(), decode_responses=True)

        now_ts = int(datetime.now(UTC).timestamp())
        active_key = f"polycrawl:adaptive:active:{account_id}"
        idle_key = f"polycrawl:adaptive:idle:{account_id}"

        if downloaded > 0:
            # Active — record last active time
            r.set(active_key, str(now_ts))
            r.set(idle_key, str(now_ts))
        else:
            # No new content — set idle to last_active (if known)
            last_active = r.get(active_key)
            if last_active is not None:
                r.set(idle_key, str(int(float(last_active))))

        r.close()
    except Exception as e:
        logger.warning("Failed to record adaptive state for account %d: %s", account_id, e)


async def _download_file(
    download_url: str,
    dest: Path,
    *,
    timeout: float = 180.0,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> int:
    """Stream-download *download_url* to *dest*.  Returns bytes written."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    bytes_written = 0
    has_raw_cookie = any(k.lower() == "cookie" for k in (headers or {}).keys())
    effective_cookies = {} if has_raw_cookie else (cookies or {})
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers=headers,
            cookies=effective_cookies,
        ) as client:
            async with client.stream("GET", download_url) as resp:
                resp.raise_for_status()
                with tmp.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        fh.write(chunk)
                        bytes_written += len(chunk)
        tmp.replace(dest)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        # Legacy-compatible fallback for protected media URLs.
        if status == 403:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            try:
                return await asyncio.to_thread(
                    _download_file_requests_http,
                    download_url,
                    dest,
                    timeout,
                    headers,
                    effective_cookies,
                )
            except Exception:
                return await asyncio.to_thread(
                    _download_file_requests,
                    download_url,
                    dest,
                    timeout,
                    headers,
                )
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    return bytes_written


def _download_file_requests(
    download_url: str,
    dest: Path,
    timeout: float,
    headers: dict[str, str],
) -> int:
    """Legacy fallback path using stdlib urllib (different HTTP stack)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    bytes_written = 0
    try:
        req = Request(download_url, headers=headers, method="GET")
        with urlopen(req, timeout=timeout) as resp:
            with tmp.open("wb") as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    bytes_written += len(chunk)
        tmp.replace(dest)
    except (HTTPError, URLError, TimeoutError, OSError):
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    return bytes_written


def _download_file_requests_http(
    download_url: str,
    dest: Path,
    timeout: float,
    headers: dict[str, str],
    cookies: dict[str, str],
) -> int:
    """Requests-based fallback path to match legacy downloader behavior."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    bytes_written = 0
    try:
        with httpx.Client() as client:
            with client.stream(
                "GET",
                download_url,
                headers=headers,
                cookies=cookies,
                timeout=timeout,
                follow_redirects=True,
            ) as resp:
                resp.raise_for_status()
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        if not chunk:
                            continue
                        fh.write(chunk)
                    bytes_written += len(chunk)
        tmp.replace(dest)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    return bytes_written


async def execute_content_fetch(task_id: str, account_id: int) -> ContentExecutionResult:
    session_factory = db_get_session_factory()
    media_root = get_media_root()

    # Resolve download rate: site config > base config
    download_rps = 0.0

    async with session_factory() as session:
        task = await session.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")

        account = await session.get(Account, account_id)
        if account is None:
            raise ValueError(f"Account not found: {account_id}")

        # Read download rate limit from config (site > base)
        try:
            from packages.core.config import ConfigLoader
            cfg = ConfigLoader(Path(__file__).resolve().parents[3] / "config").load_all()
            base_rps = cfg.base.download.download_requests_per_second
            site_cfg = cfg.sites.get(account.platform, {})
            site_rps = (site_cfg.get("download") or {}).get("download_requests_per_second")
            download_rps = float(site_rps) if site_rps is not None else base_rps
        except Exception:
            download_rps = 0.0

        creator = await session.get(Creator, account.creator_id)
        if creator is None:
            raise ValueError(f"Creator not found for account_id={account_id}")

        creator_dir = build_creator_dir(creator.display_name, creator.creator_key)
        provider = ProviderRegistry().get(account.platform)
        account_dir = provider.build_account_dir(account)
        download_request = provider.build_download_request(account.account_url)
        download_headers = dict(download_request.get("headers") or {})
        download_cookies = dict(download_request.get("cookies") or {})

        # ── streaming fetch + download (producer-consumer) ────────────
        # Producer fetches pages, consumer downloads items concurrently.
        strategy = task.params.get("strategy", "incremental")
        site_cfg = cfg.sites.get(account.platform, {})
        cf = site_cfg.get("content_fetch") or {}
        look_ahead = int(cf.get("look_ahead_pages") or 0)

        download_queue: asyncio.Queue[list[dict[str, Any]] | None] = asyncio.Queue(maxsize=4)

        async def _producer() -> int:
            """Fetch pages and push items to the download queue."""
            total_fetched = 0
            skipped_pages = 0
            while True:
                logger.info("[API] %s | content-fetch %s/%s %s",
                    creator.display_name or creator.creator_key,
                    account.platform, account.account_type,
                    account.account_url or "")
                try:
                    items = await asyncio.to_thread(provider.fetch_content_items, task.params, account.account_url)
                except Exception as exc:
                    exc_str = str(exc).lower()
                    # Auto-invalidate cookies on auth-related failures
                    if any(kw in exc_str for kw in ("401", "403", "auth", "login", "cookie", "verify", "461", "471", "status_code.:.6")):
                        try:
                            from packages.core.config.cookie_verify import invalidate as _inval
                            _inval(account.platform)
                            logger.warning("[auth] Invalidated cookies for %s due to: %s", account.platform, exc)
                        except Exception:
                            pass
                    raise
                if not items:
                    break
                total_fetched += len(items)
                await download_queue.put(items)

                if not provider.has_more:
                    break

                if strategy == "incremental" and look_ahead > 0:
                    if len(items) == 0:
                        skipped_pages += 1
                        if skipped_pages >= look_ahead:
                            break
                    else:
                        skipped_pages = 0

                task.params["cursor"] = provider.next_cursor

            await download_queue.put(None)  # sentinel
            return total_fetched

        async def _consumer() -> tuple[int, int, int]:
            """Download items from the queue as they arrive."""
            downloaded = 0
            skipped = 0
            bytes_downloaded = 0
            while True:
                items = await download_queue.get()
                if items is None:
                    break
                for idx, item in enumerate(items, start=1):
                    content_id = str(item.get("content_id") or f"{task_id}-{idx}")
                    media_kind = str(item.get("media_kind") or "video")
                    file_path = provider.build_content_file_path(
                        item, creator_dir=creator_dir, account_dir=account_dir, account_url=account.account_url,
                    )
                    download_url = str(item.get("download_url") or "")
                    title = item.get("title")
                    author = item.get("author")
                    file_size = int(item.get("file_size") or 0)
                    publish_date = parse_publish_date(item.get("publish_date") or item.get("create_time"))

                    seq = int(item.get("sequence", 0))
                    existed = await _find_artifact(session=session, account_id=account_id, platform=account.platform, content_id=content_id, media_kind=media_kind, sequence=seq)

                    if existed is not None and existed.status == "completed" and existed.file_size and existed.file_size > 0:
                        # Skip if stored file is at least as large as server's version
                        if file_size <= existed.file_size:
                            skipped += 1
                            continue
                        # Otherwise re-download: server has a larger/clearer version
                        logger.info("Re-downloading %s: server size %d > stored %d", content_id, file_size, existed.file_size)

                    is_new = existed is None
                    if is_new:
                        artifact = Artifact(
                            account_id=account_id, task_id=task.id, platform=account.platform,
                            content_id=content_id, media_kind=media_kind, sequence=seq, file_path=file_path,
                            file_size=file_size, title=str(title) if title else None,
                            author=str(author) if author else None, publish_date=publish_date,
                            download_date=datetime.now(UTC).replace(tzinfo=None), status="pending",
                        )
                        session.add(artifact)
                    else:
                        artifact = existed
                        artifact.file_path = file_path
                        artifact.title = str(title) if title else artifact.title
                        artifact.author = str(author) if author else artifact.author
                        artifact.publish_date = publish_date or artifact.publish_date
                        # Update stored server-reported size for comparison
                        if file_size > artifact.file_size:
                            artifact.file_size = file_size

                    actual_size = 0
                    if download_url:
                        dest = media_root / file_path
                        try:
                            await _download_rate_limit(account.platform, download_rps)
                            actual_size = await _download_file(download_url, dest, headers=download_headers, cookies=download_cookies)
                            artifact.status = "completed"
                            logger.info("Downloaded %s -> %s (%d bytes)", content_id, dest, actual_size)
                        except Exception as exc:
                            retried = False
                            try:
                                refreshed = await asyncio.to_thread(
                                    provider.refresh_content_item_for_download, content_id=content_id, media_kind=media_kind, account_url=account.account_url, item=item,
                                )
                                refreshed_url = str((refreshed or {}).get("download_url") or "")
                                # Also propagate create_time from enriched detail
                                refreshed_ts = (refreshed or {}).get("create_time") or 0
                                if refreshed_ts:
                                    artifact.publish_date = parse_publish_date(refreshed_ts)
                                # Refresh may also change media_kind (e.g. image→video for XHS)
                                refreshed_kind = str((refreshed or {}).get("media_kind") or "")
                                if refreshed_kind:
                                    artifact.media_kind = refreshed_kind
                                if refreshed_url and refreshed_url != download_url:
                                    await _download_rate_limit(account.platform, download_rps)
                                    actual_size = await _download_file(refreshed_url, dest, headers=download_headers, cookies=download_cookies)
                                    artifact.status = "completed"
                                    retried = True
                            except Exception:
                                pass
                            if not retried:
                                artifact.status = "download_failed"
                                logger.warning("Download failed for %s: %s", content_id, exc)
                    elif media_kind == "video":
                        # No download_url from listing API (e.g. XHS video notes).
                        # Try enrichment via feed API.
                        refreshed = None
                        try:
                            refreshed = await asyncio.to_thread(
                                provider.refresh_content_item_for_download, content_id=content_id, media_kind=media_kind, account_url=account.account_url, item=item,
                            )
                        except Exception:
                            pass
                        refreshed_url = str((refreshed or {}).get("download_url") or "") if refreshed else ""
                        refreshed_ts = (refreshed or {}).get("create_time") or 0 if refreshed else 0
                        if refreshed_ts:
                            artifact.publish_date = parse_publish_date(refreshed_ts)
                        if refreshed_url:
                            dest = media_root / file_path
                            try:
                                actual_size = await _download_file(refreshed_url, dest, headers=download_headers, cookies=download_cookies)
                                artifact.status = "completed"
                                logger.info("Enriched download %s -> %s (%d bytes)", content_id, dest, actual_size)
                            except Exception as exc:
                                artifact.status = "download_failed"
                                logger.warning("Enriched download failed for %s: %s", content_id, exc)
                        else:
                            artifact.status = "no_url"
                            logger.warning("No download_url for content_id=%s (video note)", content_id)
                    else:
                        artifact.status = "no_url"
                        logger.warning("No download_url for content_id=%s", content_id)

                    artifact.file_size = actual_size
                    artifact.download_date = datetime.now(UTC).replace(tzinfo=None)

                    if actual_size > 0:
                        downloaded += 1
                    bytes_downloaded += actual_size

                # Commit after each item so web UI sees progress in real time
                await session.commit()

            return downloaded, skipped, bytes_downloaded

        producer_task = asyncio.create_task(_producer())
        consumer_task = asyncio.create_task(_consumer())

        # Wait for both to finish
        total_fetched = await producer_task
        downloaded, skipped, bytes_downloaded = await consumer_task

    # ── adaptive tracking ──────────────────────────────────────
    _record_adaptive_state(account_id, downloaded)

    # ── persist cursor so next dispatch resumes pagination ─────
    try:
        cursor_val = task.params.get("cursor", 0)
        if not provider.has_more:
            cursor_val = 0
        from redis import Redis as _SyncRedis
        _r = _SyncRedis.from_url(redis_get_url(), decode_responses=True)
        _r.set(f"polycrawl:incremental:cursor:{account_id}", str(cursor_val))
        _r.close()
    except Exception:
        pass

    # ── notify frontend ────────────────────────────────────────
    try:
        publish_event("creators_updated", {"account_id": account_id})
    except Exception:
        pass

    return ContentExecutionResult(
        items_fetched=total_fetched,
        items_downloaded=downloaded,
        items_skipped=skipped,
        bytes_downloaded=bytes_downloaded,
    )


async def _find_artifact(
    session,
    account_id: int,
    platform: str,
    content_id: str,
    media_kind: str,
    sequence: int = 0,
) -> Artifact | None:
    query = select(Artifact).where(
        and_(
            Artifact.account_id == account_id,
            Artifact.platform == platform,
            Artifact.content_id == content_id,
            Artifact.media_kind == media_kind,
            Artifact.sequence == sequence,
        )
    )
    result = await session.execute(query)
    return result.scalars().first()
