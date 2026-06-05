from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import time

import httpx

from sqlalchemy import select

from packages.core.providers.registry import ProviderRegistry
from packages.core.db.models import Account, Creator, LiveSession, LiveStatus, Task
from packages.core.db.session import db_get_session_factory
from packages.core.utils import build_creator_dir, now_utc_naive
from packages.core.db.urls import redis_get_url
from services.worker.runtime import get_media_root


_LOG = logging.getLogger(__name__)
_BG_DOWNLOAD_TASKS: set[asyncio.Task] = set()


def _stop_key(account_id: int) -> str:
    """Redis key: when set, signals that account's live recording should stop."""
    return f"{_ACCOUNT_KEY_PREFIX}{account_id}"



# 鈹€鈹€ live state updater 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def _update_last_live(account_id: int) -> None:
    """Record this account's last live timestamp in Redis for adaptive scheduling."""
    try:
        from redis import Redis
        with redis_sync() as r:
            r.set(f"polycrawl:live:last_live:{account_id}", str(int(datetime.now(UTC).timestamp())))
        r.close()
    except Exception:
        pass


@dataclass(slots=True)
class LiveMonitorResult:
    status: str
    is_live: bool


@dataclass(slots=True)
class LiveRecordResult:
    session_id: str = ""
    status: str = ""  # "completed" | "interrupted" | "offline"
    reconnect_attempts: int = 0


async def execute_live_monitor(task_id: str, account_id: int) -> LiveMonitorResult:
    session_factory = db_get_session_factory()

    async with session_factory() as session:
        task = await session.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")

        account = await session.get(Account, account_id)
        if account is None:
            raise ValueError(f"Account not found: {account_id}")

        creator = await session.get(Creator, account.creator_id)

        provider = ProviderRegistry().get(account.platform)
        _LOG.info("[API] %s | live-check %s/%s %s",
            (creator.display_name if creator else "?") or "?",
            account.platform, account.account_type,
            account.account_url or "")
        # Provider live detection may run its own event loop; run it off-thread
        # to avoid nested asyncio.run() failures inside this async executor.
        is_live = await asyncio.to_thread(
            provider.detect_live_status,
            task.params,
            account.account_url,
        )

        await _upsert_live_status(session, account_id, status="probing")
        final_status = "recording" if is_live else "offline"
        await _upsert_live_status(session, account_id, status=final_status)

        await session.commit()
        return LiveMonitorResult(status=final_status, is_live=is_live)


async def execute_live_record(task_id: str, account_id: int) -> LiveRecordResult:
    """Check live status and start background recording if live.

    Fast path 鈥?does NOT block on download.  The actual stream download
    runs in a background asyncio task that updates Task/LiveSession on
    completion.
    """
    session_factory = db_get_session_factory()

    async with session_factory() as session:
        task = await session.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")

        account = await session.get(Account, account_id)
        if account is None:
            raise ValueError(f"Account not found: {account_id}")

        creator = await session.get(Creator, account.creator_id)
        if creator is None:
            raise ValueError(f"Creator not found: {account.creator_id}")

        provider = ProviderRegistry().get(account.platform)
        payload = provider.build_live_session_payload(task.params, account.account_url)

    # 鈹€鈹€ resolve live stream (fast API call) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    _LOG.info("[API] %s | live-stream %s/%s %s",
        creator.display_name or creator.creator_key,
        account.platform, account.account_type,
        account.account_url or "")
    stream_info = await asyncio.to_thread(
        provider.resolve_live_stream,
        task.params,
        account.account_url,
    )
    if not stream_info or not bool(stream_info.get("is_live", False)):
        async with session_factory() as session:
            await _upsert_live_status(session, account_id, status="offline")
            await session.commit()
        return LiveRecordResult(status="offline")

    stream_url = str(stream_info.get("stream_url") or "").strip()
    if not stream_url:
        raise RuntimeError("live stream url is empty")
    stream_url = provider.normalize_stream_url(stream_url)

    # 鈹€鈹€ extract room_id for path disambiguation 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    room_id = str(stream_info.get("room_id") or provider.extract_account_key(account.account_url, account.account_type) or "")

    # 鈹€鈹€ prepare download parameters 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    download_req = provider.build_live_download_request(account.account_url)
    download_headers = dict(download_req.get("headers") or {})
    download_cookies = dict(download_req.get("cookies") or {})

    duration_limit = int(payload.get("duration_seconds", 0) or 0)
    bytes_limit = int(payload.get("total_bytes", 0) or 0)
    creator_dir = build_creator_dir(creator.display_name, creator.creator_key)
    now = now_utc_naive().strftime("%Y%m%d_%H%M%S")
    output_file_path = f"{creator_dir}/{account.platform}/live/{room_id}/{now}.flv"
    media_root = get_media_root()
    final_path = media_root / output_file_path

    # 鈹€鈹€ create LiveSession record 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    async with session_factory() as session:
        live_session = LiveSession(
            account_id=account_id,
            started_at=now_utc_naive(),
            status="recording",
            output_file_path=output_file_path,
        )
        session.add(live_session)
        await session.flush()

        await _upsert_live_status(
            session, account_id, status="recording",
            current_recording_session_id=live_session.id,
        )
        await session.commit()
        ls_id = live_session.id

    # 鈹€鈹€ spawn background download 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    bg_task = asyncio.create_task(_background_record(
        task_id=task_id, account_id=account_id,
        session_id=str(ls_id),
        stream_url=stream_url,
        headers=download_headers, cookies=download_cookies,
        final_path=final_path,
        duration_limit=duration_limit, bytes_limit=bytes_limit,
        output_file_path=output_file_path,
    ))
    _BG_DOWNLOAD_TASKS.add(bg_task)
    bg_task.add_done_callback(_BG_DOWNLOAD_TASKS.discard)

    return LiveRecordResult(
        session_id=str(ls_id),
        status="recording_started",
    )


async def _background_record(
    task_id: str, account_id: int, session_id: str,
    stream_url: str,
    headers: dict, cookies: dict,
    final_path: Path,
    duration_limit: int, bytes_limit: int,
    output_file_path: str,
) -> None:
    """Download live stream in background, then update task & session."""
    import uuid
    from packages.core.db.session import db_get_session_factory
    from services.worker.runtime import mark_task_success, mark_task_failed
    from services.worker.scheduler import notify_live_done
    session_factory = db_get_session_factory()
    ls_uuid = uuid.UUID(session_id)

    _LOG.info("[bg] start account=%d session=%s path=%s", account_id, session_id, output_file_path)
    started_at = now_utc_naive()

    # 鈹€鈹€ stop-signal monitor 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    # The API sets polycrawl:live:stop:{account_id} when a user cancels.
    stop_event = asyncio.Event()
    _redis_url = redis_get_url()
    stop_monitor = asyncio.create_task(_poll_stop_signal(account_id, _redis_url, stop_event))

    try:
        download_timeout = (duration_limit + 45) if duration_limit > 0 else None
        _LOG.info("[bg] downloading account=%d timeout=%s", account_id, download_timeout)

        # Check stop signal before starting download
        if stop_event.is_set():
            raise RuntimeError(f"live recording cancelled by user for account {account_id}")

        downloaded_bytes = await asyncio.wait_for(
            _download_live_stream(
                stream_url, final_path,
                headers=headers, cookies=cookies,
                duration_limit_seconds=duration_limit,
                bytes_limit=bytes_limit,
                stop_event=stop_event,
            ),
            timeout=download_timeout,
        )
        ended_at = now_utc_naive()
        actual_duration = max(int((ended_at - started_at).total_seconds()), 0)
        session_status = "completed"
        _LOG.info("[bg] success account=%d bytes=%d duration=%d", account_id, downloaded_bytes, actual_duration)

        # Update LiveSession
        async with session_factory() as session:
            ls = await session.get(LiveSession, ls_uuid)
            if ls:
                ls.status = session_status
                ls.ended_at = ended_at
                ls.total_duration_seconds = actual_duration
                ls.total_bytes = downloaded_bytes
            await _upsert_live_status(
                session, account_id, status="offline",
                recorded_seconds=actual_duration,
                recorded_bytes=downloaded_bytes,
            )
            await session.commit()

        await mark_task_success(task_id)
        _update_last_live(account_id)
        notify_live_done(account_id)

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _LOG.exception("[bg] failed account=%d", account_id)
        ended_at = now_utc_naive()
        async with session_factory() as session:
            ls = await session.get(LiveSession, ls_uuid)
            if ls:
                ls.status = "interrupted"
                ls.ended_at = ended_at
            await _upsert_live_status(
                session, account_id, status="offline",
                error_message=str(exc),
            )
            await session.commit()

        await mark_task_failed(task_id, str(exc))
        notify_live_done(account_id)
    finally:
        stop_monitor.cancel()
        try:
            await stop_monitor
        except asyncio.CancelledError:
            pass
        # Clean up Redis stop key if set
        try:
            from redis.asyncio import Redis as AsyncRedis
            r2 = AsyncRedis.from_url(_redis_url, decode_responses=True)
            await r2.delete(_stop_key(account_id))
            await r2.aclose()
        except Exception:
            pass

    # Fire queue-drain event for next dispatch
    try:
        from redis.asyncio import Redis
        r = Redis.from_url(redis_get_url(), decode_responses=True)
        # Find which queue this task belongs to and check if it's empty
        # (simplified: just publish and let scheduler's guard handle it)
        await r.publish("polycrawl:live:dispatch", "1")
        await r.aclose()
    except Exception:
        pass


async def _download_live_stream(
    stream_url: str,
    dest: Path,
    *,
    headers: dict[str, str],
    cookies: dict[str, str],
    duration_limit_seconds: int,
    bytes_limit: int,
    stop_event: asyncio.Event | None = None,
) -> int:
    _LOG.info("[bg] stream-open url=%s", (stream_url[:120] + "...") if len(stream_url) > 120 else stream_url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    has_raw_cookie = any(k.lower() == "cookie" for k in headers.keys())
    effective_cookies = {} if has_raw_cookie else cookies
    bytes_written = 0
    start = time.monotonic()

    response_open_timeout = 20
    first_byte_timeout = 25
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=20.0, read=None, write=None, pool=20.0),
            follow_redirects=True,
            headers=headers,
            cookies=effective_cookies,
            trust_env=False,
        ) as client:
            stream_ctx = client.stream("GET", stream_url)
            try:
                resp = await asyncio.wait_for(stream_ctx.__aenter__(), timeout=response_open_timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"live stream response-header timeout ({response_open_timeout}s)"
                ) from exc
            try:
                resp.raise_for_status()
                _LOG.info("[bg] stream-response status=%d dest=%s", resp.status_code, str(dest))
                with dest.open("wb") as fh:
                    aiter = resp.aiter_bytes(chunk_size=65536)

                    # Guard against hanging forever before the first media byte.
                    try:
                        first_chunk = await asyncio.wait_for(aiter.__anext__(), timeout=first_byte_timeout)
                    except StopAsyncIteration:
                        first_chunk = b""
                    except asyncio.TimeoutError as exc:
                        raise TimeoutError(f"live stream first byte timeout ({first_byte_timeout}s)") from exc

                    if first_chunk:
                        _LOG.info("[bg] first-chunk size=%d", len(first_chunk))
                        fh.write(first_chunk)
                        bytes_written += len(first_chunk)

                    async for chunk in aiter:
                        if not chunk:
                            continue
                        fh.write(chunk)
                        bytes_written += len(chunk)
                        if bytes_limit > 0 and bytes_written >= bytes_limit:
                            break
                        if duration_limit_seconds > 0 and (time.monotonic() - start) >= duration_limit_seconds:
                            break
                        if stop_event is not None and stop_event.is_set() and bytes_written > 0:
                            _LOG.info("[bg] stop signal received account=?, bytes=%d", bytes_written)
                            break
            finally:
                await stream_ctx.__aexit__(None, None, None)
        return bytes_written
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 403:
            _LOG.warning("[bg] flv httpx 403, fallback to requests")
            return await asyncio.to_thread(
                _download_live_stream_requests,
                stream_url,
                dest,
                headers,
                effective_cookies,
                duration_limit_seconds,
                bytes_limit,
            )
        raise
    except (TimeoutError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ReadError, httpx.RemoteProtocolError):
        # Some CDN routes stall in httpx but work with requests/urllib3.
        _LOG.warning("[bg] flv httpx stalled, fallback to requests")
        return await asyncio.to_thread(
            _download_live_stream_requests,
            stream_url,
            dest,
            headers,
            effective_cookies,
            duration_limit_seconds,
            bytes_limit,
        )
    except Exception:
        raise


def _download_live_stream_requests(
    stream_url: str,
    dest: Path,
    headers: dict[str, str],
    cookies: dict[str, str],
    duration_limit_seconds: int,
    bytes_limit: int,
) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    bytes_written = 0
    start = time.monotonic()
    try:
        with httpx.Client() as client:
            with client.stream(
                "GET",
                stream_url,
                headers=headers,
                cookies=cookies,
                timeout=30,
                follow_redirects=True,
            ) as resp:
                resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    bytes_written += len(chunk)
                    if bytes_limit > 0 and bytes_written >= bytes_limit:
                        break
                    if duration_limit_seconds > 0 and (time.monotonic() - start) >= duration_limit_seconds:
                        break
        return bytes_written
    except Exception:
        raise


async def _poll_stop_signal(account_id: int, redis_url: str, stop_event: asyncio.Event) -> None:
    """Periodically check Redis for a stop signal for this account.

    The API sets ``polycrawl:live:stop:{account_id}`` when a user cancels
    a live recording. This task polls every 3 s and sets ``stop_event``
    when found.
    """
    from redis.asyncio import Redis as AsyncRedis
    r = await AsyncRedis.from_url(redis_url, decode_responses=True)
    try:
        key = _stop_key(account_id)
        while not stop_event.is_set():
            exists = await r.exists(key)
            if exists:
                stop_event.set()
                break
            await asyncio.sleep(3)
    finally:
        await r.aclose()


async def mark_live_error(account_id: int, error_message: str) -> None:
    session_factory = db_get_session_factory()
    async with session_factory() as session:
        await _upsert_live_status(session, account_id, status="error", error_message=error_message)
        await session.commit()


async def _upsert_live_status(
    session,
    account_id: int,
    *,
    status: str,
    current_recording_session_id=None,
    recorded_seconds: int | None = None,
    recorded_bytes: int | None = None,
    error_message: str | None = None,
) -> None:
    now = now_utc_naive()
    result = await session.execute(select(LiveStatus).where(LiveStatus.account_id == account_id))
    item = result.scalars().first()

    if item is None:
        item = LiveStatus(
            account_id=account_id,
            status=status,
            status_since=now,
            current_recording_session_id=current_recording_session_id,
            recorded_seconds=recorded_seconds,
            recorded_bytes=recorded_bytes,
            error_message=error_message,
            error_time=now if error_message else None,
            updated_at=now,
        )
        session.add(item)
        return

    if item.status != status:
        item.status_since = now
    item.status = status
    item.updated_at = now
    item.current_recording_session_id = current_recording_session_id
    item.recorded_seconds = recorded_seconds
    item.recorded_bytes = recorded_bytes
    item.error_message = error_message
    item.error_time = now if error_message else None

