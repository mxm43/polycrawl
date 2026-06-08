from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from packages.core.providers.base import BaseProvider, SyncRateLimiter, _safe
from packages.core.utils import sanitize_filename, build_creator_dir
from packages.provider_impls.douyin.api_client import (
    _extract_room_id,
    _extract_sec_uid,
    fetch_all_posts,
    fetch_post_detail,
    DouyinAPIClient,
)

# /app/packages/providers/douyin/__init__.py -> project root is parents[3] (/app)
_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_DIR = _ROOT / "config"
_CST = timezone(timedelta(hours=8))


def _replace_str(value: str, max_len: int = 20) -> str:
    parts = re.findall(r"[0-9A-Za-z\u4e00-\u9fa5]+", value)
    text = "".join(parts).strip()
    return text[:max_len] if text else ""


def _load_site_config() -> dict[str, Any]:
    return BaseProvider.load_site_config("douyin")


def _load_base_config() -> dict[str, Any]:
    """Read base config lazily; returns empty dict on any error."""
    try:
        from packages.core.config.jsonc import load_jsonc
        return load_jsonc(_CONFIG_DIR / "base.jsonc")
    except Exception:
        return {}


def _get_strategy_tick(base_cfg: dict[str, Any], strategy_name: str = "incremental") -> str:
    """Extract tick from strategy config, default '10s'."""
    strategy = base_cfg.get("strategy") or {}
    item = strategy.get(strategy_name) or {}
    return str(item.get("tick") or "10s")


def _redis(rurl: str):
    from redis import Redis
    return Redis.from_url(rurl, decode_responses=True)


class DouyinProvider(BaseProvider, SyncRateLimiter):
    platform = "douyin"

    def healthcheck(self) -> dict[str, str]:
        return {"platform": self.platform, "status": "ok"}

    # ------------------------------------------------------------------
    # Content fetch 鈥?one page per call
    # ------------------------------------------------------------------

    def fetch_content_items(
        self,
        task_params: dict[str, Any],
        account_url: str,
    ) -> list[dict[str, Any]]:
        """
        Fetch ONE page of posts.  Returns normalized items list.
        Pagination state (next cursor, has_more) is returned via
        the provider instance for the executor to loop on.
        """
        # Store tick for downstream refresh calls to share the rate limiter
        self._tick = str(task_params.get("tick") or "10s")

        site_cfg = _load_site_config()
        cookies: dict[str, str] = {
            k: str(v) for k, v in (site_cfg.get("cookies") or {}).items() if v
        }

        cursor: int = int(task_params.get("cursor") or 0)
        tick: str = str(task_params.get("tick") or "10s")
        strategy: str = str(task_params.get("strategy") or "incremental")
        account_id: int | None = task_params.get("account_id")

        try:
            sec_uid = _extract_sec_uid(account_url)
        except ValueError:
            return []

        if not cookies:
            return [{
                "content_id": f"stub-{abs(hash(account_url)) % 100_000:05d}",
                "media_kind": "video",
                "file_size": 0,
                "title": f"[stub] {account_url}",
                "author": "",
                "download_url": "",
            }]

        return asyncio.run(
            self._fetch_and_store_page_cursor(sec_uid, cookies, cursor, tick, strategy, account_id)
        )

    async def _fetch_and_store_page_cursor(
        self,
        sec_uid: str,
        cookies: dict[str, str],
        cursor: int,
        tick: str,
        strategy: str,
        account_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch one page, store pagination state on instance, return items."""
        # Class-level rate limit: shared across all API calls (fetch + refresh)
        self.rate_limit_with_jitter(tick)
        from packages.provider_impls.douyin.api_client import DouyinAPIClient, _normalise_items

        async with DouyinAPIClient(cookies, tick=tick) as client:
            raw_items, self._next_cursor, self._has_more = await client.fetch_post_page(sec_uid, cursor=cursor)

        results = []
        for raw in raw_items:
            for item in _normalise_items(raw):
                if item.get("content_id"):
                    results.append(item)
        return results

    @property
    def has_more(self) -> bool:
        return getattr(self, "_has_more", False)

    @property
    def next_cursor(self) -> int:
        return getattr(self, "_next_cursor", 0)

    def fetch_content_item_by_id(
        self,
        content_id: str,
        account_url: str,
    ) -> dict[str, Any] | None:
        """Refresh one content item using detail endpoint (legacy-compatible fallback)."""
        site_cfg = _load_site_config()
        cookies: dict[str, str] = {
            k: str(v) for k, v in (site_cfg.get("cookies") or {}).items() if v
        }
        if not cookies:
            return None

        # Use stored tick from fetch_content_items, or fallback to config
        tick: str = getattr(self, "_tick", None) or _get_strategy_tick(_load_base_config())

        proxy: str | None = site_cfg.get("proxy") or None
        self.rate_limit_with_jitter(tick)

        items = asyncio.run(fetch_post_detail(content_id, cookies, tick=tick))
        if not items:
            return None

        for item in items:
            if str(item.get("content_id") or "") == str(content_id):
                return item
        return items[0]

    def build_account_dir(self, account: Any) -> str:
        def _clean_or_empty(value: str | None, max_len: int) -> str:
            text = (value or "").strip()
            if not text:
                return ""
            return _safe(text, max_len)

        platform_id = _clean_or_empty(getattr(account, "platform_account_id", None), 50)
        alias = _clean_or_empty(getattr(account, "account_alias", None), 50)

        account_url = str(getattr(account, "account_url", "") or "").strip()
        sec_uid = ""
        if account_url:
            m = re.search(r"/user/([^/?#]+)", account_url)
            if m:
                sec_uid = _safe(m.group(1), 255)

        account_url_safe = _clean_or_empty(account_url, 80)
        return platform_id or sec_uid or alias or account_url_safe or "account"

    def build_content_file_path(
        self,
        item: dict[str, Any],
        *,
        creator_dir: str,
        account_dir: str,
        account_url: str,
    ) -> str:
        create_time = item.get("create_time") or 0
        try:
            dt = datetime.fromtimestamp(float(create_time), tz=_CST)
            date_str = dt.strftime("%Y-%m-%d %H.%M.%S")
        except (ValueError, OSError):
            date_str = "0000-00-00 00.00.00"

        title = _replace_str(str(item.get("title") or ""))
        prefix = f"{date_str}_{title}" if title else date_str

        media_kind = str(item.get("media_kind") or "video")
        seq = int(item.get("sequence", 0))
        ext = "mp4" if media_kind == "video" else "jpeg"
        filename = f"{prefix}_{media_kind}_{seq}.{ext}"

        return f"{creator_dir}/{self.platform}/{account_dir}/{filename}"

    def build_download_request(self, account_url: str) -> dict[str, Any]:
        site_cfg = _load_site_config()
        cookies: dict[str, str] = {
            k: str(v) for k, v in (site_cfg.get("cookies") or {}).items() if v
        }
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/109.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.douyin.com/",
            "accept-encoding": "None",
        }
        if cookie_header:
            headers["Cookie"] = cookie_header
        # Keep download path cookie handling legacy-compatible: raw Cookie header only.
        return {"headers": headers, "cookies": {}}

    def refresh_content_item_for_download(
        self,
        *,
        content_id: str,
        media_kind: str,
        account_url: str,
    ) -> dict[str, Any] | None:
        if media_kind != "video" or not content_id.isdigit():
            return None
        return self.fetch_content_item_by_id(content_id, account_url)

    # ------------------------------------------------------------------
    # Live detection
    # ------------------------------------------------------------------

    def detect_live_status(
        self,
        task_params: dict[str, Any],
        account_url: str,
    ) -> bool:
        """
        Checks whether the live room is currently broadcasting.
        Uses X-Bogus signing 鈥?no cookies required.
        """
        site_cfg = _load_site_config()
        cookies: dict[str, str] = {
            k: str(v) for k, v in (site_cfg.get("cookies") or {}).items() if v
        }
        base_cfg = _load_base_config()
        tick: str = str(task_params.get("tick") or _get_strategy_tick(base_cfg))
        jitter = task_params.get("jitter")
        self.rate_limit_with_jitter(tick, jitter, bucket="live", label="live-detect")

        try:
            room_id = _extract_room_id(account_url)
        except ValueError:
            return bool(task_params.get("is_live", False))

        async def _check() -> bool:
            async with DouyinAPIClient(cookies, tick=tick) as client:
                return await client.check_live_status(room_id)

        return asyncio.run(_check())

    # ------------------------------------------------------------------
    # Live recording parameters
    # ------------------------------------------------------------------

    def build_live_session_payload(
        self,
        task_params: dict[str, Any],
        account_url: str,
    ) -> dict[str, Any]:
        """
        Merges site-level live defaults with task_params overrides.
        """
        site_cfg = _load_site_config()
        live_defaults: dict[str, Any] = site_cfg.get("live") or {}
        record_defaults: dict[str, Any] = live_defaults.get("record") or {}

        return {
            "duration_seconds": int(task_params.get("duration_seconds", 0) or 0),
            "total_bytes": int(task_params.get("total_bytes", 0) or 0),
            "segment_count": int(task_params.get("segment_count", 0) or 0),
            "output_file_path": task_params.get("output_file_path"),
            "stop_requested": bool(task_params.get("stop_requested", False)),
            "simulate_disconnect": bool(task_params.get("simulate_disconnect", False)),
            "recover_window_seconds": int(
                task_params.get("recover_window_seconds")
                or record_defaults.get("recover_window_seconds")
                or 120
            ),
            "fast_reconnect_seconds": (
                task_params.get("fast_reconnect_seconds")
                or record_defaults.get("fast_reconnect_seconds")
                or [1, 2, 3, 5, 8]
            ),
        }

    def resolve_live_stream(
        self,
        task_params: dict[str, Any],
        account_url: str,
    ) -> dict[str, Any] | None:
        """
        Resolve live stream URL from a Douyin live room.
        Uses X-Bogus signing 鈥?no cookies required.
        """
        site_cfg = _load_site_config()
        cookies: dict[str, str] = {
            k: str(v) for k, v in (site_cfg.get("platform") or {}).get("cookies", {}).items() if v
        }
        base_cfg = _load_base_config()
        tick: str = str(task_params.get("tick") or _get_strategy_tick(base_cfg))
        jitter = task_params.get("jitter")
        self.rate_limit_with_jitter(tick, jitter, bucket="live", label="live-resolve")

        direct_url = str(task_params.get("stream_url") or "").strip()
        if direct_url:
            return {
                "is_live": True,
                "stream_url": direct_url,
                "room_id": None,
                "title": None,
                "nickname": None,
            }

        try:
            room_id = _extract_room_id(account_url)
        except ValueError:
            return None

        async def _resolve() -> dict[str, Any] | None:
            async with DouyinAPIClient(cookies, tick=tick) as client:
                room = await client.fetch_live_room(room_id)
                if not room:
                    return None

                status = int(room.get("status", 4) or 4)
                if status != 2:
                    return {
                        "is_live": False,
                        "stream_url": "",
                        "room_id": room_id,
                        "title": room.get("title"),
                        "nickname": (room.get("owner") or {}).get("nickname"),
                    }

                stream_url_obj: dict[str, Any] = room.get("stream_url") or {}
                flv_map: dict[str, str] = stream_url_obj.get("flv_pull_url") or {}
                hls_map: dict[str, str] = stream_url_obj.get("hls_pull_url_map") or {}

                quality_order = ["FULL_HD1", "HD1", "SD1", "SD2"]
                candidates: list[str] = []
                for q in quality_order:
                    u = str((flv_map or {}).get(q) or "").strip()
                    if u:
                        candidates.append(u)
                for q in quality_order:
                    u = str((hls_map or {}).get(q) or "").strip()
                    if u:
                        candidates.append(u)

                selected = ""
                if isinstance(flv_map, dict) and flv_map:
                    selected = str(next(iter(flv_map.values())) or "")
                if not selected and isinstance(hls_map, dict) and hls_map:
                    selected = str(next(iter(hls_map.values())) or "")
                if not selected and candidates:
                    selected = candidates[0]

                return {
                    "is_live": True,
                    "stream_url": selected,
                    "stream_candidates": candidates,
                    "room_id": room_id,
                    "title": room.get("title"),
                    "nickname": (room.get("owner") or {}).get("nickname"),
                }

        return asyncio.run(_resolve())

    def build_live_download_request(self, account_url: str) -> dict[str, Any]:
        """Live stream CDN URLs are self-authenticated 鈥?no cookies needed."""
        return {
            "headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/109.0.0.0 Safari/537.36"
                ),
                "Referer": "https://live.douyin.com/",
            },
            "cookies": {},
        }

    def normalize_stream_url(self, url: str) -> str:
        u = str(url or "").strip()
        if not u:
            return ""
        if u.startswith("http://") and "douyincdn.com/" in u:
            return "https://" + u[len("http://"):]
        return u

    def extract_account_key(self, account_url: str, account_type: str) -> str:
        if account_type == "live":
            # https://live.douyin.com/774314130552 -> 774314130552
            return (account_url or "").rstrip("/").rsplit("/", 1)[-1]
        # profile: https://www.douyin.com/user/MS4wLjABAAAA... -> sec_uid
        return _extract_sec_uid(account_url)


def build_provider() -> DouyinProvider:
    return DouyinProvider()
