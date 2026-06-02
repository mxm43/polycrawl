"""
Douyin HTTP API client.

All network I/O is async (httpx).  X-Bogus request signing is applied
automatically on every GET so that the API returns real data instead of
being blocked by Douyin's anti-scraping layer.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.parse
from typing import Any

import httpx

from packages.core.providers.base import BaseAPIClient
from packages.provider_impls.douyin.signing import DEFAULT_UA, sign_query

logger = logging.getLogger(__name__)

# Must match the UA used for signing 鈥?do NOT change one without the other.
_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Referer": "https://www.douyin.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

_BASE_URL = "https://www.douyin.com"
_LIVE_BASE_URL = "https://live.douyin.com"
_POST_LIST_ENDPOINT = "/aweme/v1/web/aweme/post/"
_POST_DETAIL_ENDPOINT = "/aweme/v1/web/aweme/detail/"
_LIVE_ENTER_ENDPOINT = "/webcast/room/web/enter/"
_PAGE_SIZE = 18


def _extract_sec_uid(account_url: str) -> str:
    """Parse sec_uid from a Douyin profile URL."""
    parsed = urllib.parse.urlparse(account_url)
    parts = [p for p in parsed.path.split("/") if p]
    if "user" in parts:
        idx = parts.index("user")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    raise ValueError(f"Cannot extract sec_uid from URL: {account_url}")


def _extract_room_id(account_url: str) -> str:
    """Parse room_id from a Douyin live URL."""
    parsed = urllib.parse.urlparse(account_url)
    parts = [p for p in parsed.path.split("/") if p]
    if parts:
        return parts[-1]
    raise ValueError(f"Cannot extract room_id from URL: {account_url}")


class DouyinAPIClient(BaseAPIClient):
    """
    Thin async HTTP client for the Douyin web API.

    Args:
        cookies: dict of Douyin cookies (from sites/douyin.jsonc platform.cookies)
        tick: minimum interval between API calls, e.g. "10s", "30s"
    """

    def __init__(self, cookies: dict[str, str], *, tick: str) -> None:
        super().__init__(tick=tick)
        self._cookies = cookies
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "DouyinAPIClient":
        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers=_HEADERS,
            cookies=self._cookies,
            timeout=30.0,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get_signed(
        self,
        path: str,
        params: dict[str, Any],
        *,
        live_host: bool = False,
    ) -> dict[str, Any]:
        """
        Build a signed URL and make a GET request.

        X-Bogus is appended to the query string (not as a header), which is
        what the Douyin web API requires.
        """
        assert self._client is not None, "Client not entered"
        await self._rate_limit()

        # Build unsigned query string, sign it, then request via pre-built URL.
        unsigned_qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        signed_qs = sign_query(unsigned_qs)
        if live_host:
            url = f"{_LIVE_BASE_URL}{path}?{signed_qs}"
        else:
            url = f"{path}?{signed_qs}"

        response = await self._client.get(url)
        self._last_request_ts = time.monotonic()
        response.raise_for_status()
        if not response.content:
            raise ValueError(f"Empty response body (status={response.status_code})")
        return response.json()

    async def fetch_post_page(
        self,
        sec_uid: str,
        cursor: int = 0,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Returns (items, next_cursor, has_more)."""
        # Keep parity with proven legacy request shape; minimal params can return
        # sparse pages (e.g. page3 empty while has_more=true).
        params: dict[str, Any] = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "pc_client_type": "1",
            "version_code": "170400",
            "version_name": "17.4.0",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "123.0.0.0",
            "browser_online": "true",
            "engine_name": "Blink",
            "engine_version": "123.0.0.0",
            "os_name": "Windows",
            "os_version": "10",
            "cpu_core_num": "8",
            "device_memory": "8",
            "platform": "PC",
            "downlink": "10",
            "effective_type": "4g",
            "round_trip_time": "50",
            "msToken": self._cookies.get("msToken", ""),
            "sec_user_id": sec_uid,
            "max_cursor": cursor,
            "count": max(_PAGE_SIZE, 20),
            "locate_query": "false",
            "show_live_replay_strategy": "1",
            "need_time_list": "1",
            "time_list_query": "0",
            "whale_cut_token": "",
            "cut_version": "1",
            "publish_video_strategy_type": "2",
        }
        data = await self._get_signed(_POST_LIST_ENDPOINT, params)
        # Check for API-level errors (status_code != 0)
        status_code = data.get("status_code", 0)
        if status_code != 0:
            err_msg = f"Douyin API error: status_code={status_code}"
            if status_code == 6:
                raise RuntimeError(f"{err_msg} 鈥?cookies may be invalid or expired")
            logger.warning("%s", err_msg)
        items: list[dict[str, Any]] = data.get("aweme_list") or []
        next_cursor: int = int(data.get("max_cursor") or 0)
        has_more: bool = bool(data.get("has_more", False))
        return items, next_cursor, has_more

    async def check_live_status(self, web_rid: str) -> bool:
        """
        Returns True if the live room is currently broadcasting.

        Args:
            web_rid: the numeric room ID from a live.douyin.com URL
        """
        params: dict[str, Any] = {
            "aid": 6383,
            "device_platform": "web",
            "web_rid": web_rid,
        }
        try:
            data = await self._get_signed(_LIVE_ENTER_ENDPOINT, params, live_host=True)
            if data.get("status_code") != 0:
                return False
            rooms: list[Any] = (data.get("data") or {}).get("data") or []
            if not rooms:
                return False
            status: int = int(rooms[0].get("status", 4))
            # status 2 = live; status 4 = offline
            return status == 2
        except Exception:
            return False

    async def fetch_live_room(self, web_rid: str) -> dict[str, Any] | None:
        """Return first live room payload from enter API, or None on failure."""
        params: dict[str, Any] = {
            "aid": 6383,
            "device_platform": "web",
            "web_rid": web_rid,
        }
        try:
            data = await self._get_signed(_LIVE_ENTER_ENDPOINT, params, live_host=True)
            if data.get("status_code") != 0:
                return None
            rooms: list[Any] = (data.get("data") or {}).get("data") or []
            if not rooms:
                return None
            room = rooms[0]
            if not isinstance(room, dict):
                return None
            return room
        except Exception:
            return None


def _normalise_items(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a raw aweme dict to one or more canonical content items.

    A regular video post yields a single item with media_kind='video'.
    An image/slideshow post (鍥炬枃) yields one item per image with media_kind='image'.
    """
    aweme_id: str = str(raw.get("aweme_id") or raw.get("id") or "")
    desc: str = str(raw.get("desc") or "")[:200]
    create_time: int = int(raw.get("create_time") or 0)

    author: dict[str, Any] = raw.get("author") or {}
    author_name: str = str(author.get("nickname") or "")

    # 鈹€鈹€ Image / slideshow post (鍥炬枃) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    images: list[dict[str, Any]] = raw.get("images") or []
    if images:
        items: list[dict[str, Any]] = []
        for i, img in enumerate(images, start=1):
            # Each element in images[] may be a static image OR an embedded
            # short-video clip.  Check for the "video" sub-field (same structure
            # as a top-level video post) to distinguish them.
            img_video: dict[str, Any] | None = img.get("video") if img.get("video") else None
            if img_video:
                # Embedded video inside a 鍥炬枃 carousel.
                # Prefer play_addr_h264 for H.264 MP4; fall back to play_addr.
                iv_addr: dict[str, Any] = (
                    img_video.get("play_addr_h264")
                    or img_video.get("play_addr")
                    or {}
                )
                iv_urls: list[str] = iv_addr.get("url_list") or []
                items.append({
                    "content_id": aweme_id,
                    "media_kind": "video",
                    "sequence": i - 1,
                    "title": desc,
                    "author": author_name,
                    "file_size": int(iv_addr.get("data_size") or 0),
                    "file_path": "",
                    "download_url": iv_urls[0] if iv_urls else "",
                    "create_time": create_time,
                })
            else:
                # Static image.
                img_url_list: list[str] = img.get("url_list") or []
                items.append({
                    "content_id": aweme_id,
                    "media_kind": "image",
                    "sequence": i - 1,
                    "title": desc,
                    "author": author_name,
                    "file_size": 0,
                    "file_path": "",
                    "download_url": img_url_list[0] if img_url_list else "",
                    "create_time": create_time,
                })
        return items

    # 鈹€鈹€ Regular video post 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    video: dict[str, Any] = raw.get("video") or {}
    # Keep parity with legacy downloader: prefer bit_rate[0].play_addr first.
    bit_rate: list[dict[str, Any]] = video.get("bit_rate") or []
    br_play_addr: dict[str, Any] = {}
    if bit_rate and isinstance(bit_rate, list):
        first = bit_rate[0] or {}
        br_play_addr = first.get("play_addr") or {}

    play_addr: dict[str, Any] = (
        br_play_addr
        or video.get("play_addr_h264")
        or video.get("play_addr")
        or {}
    )
    url_list: list[str] = play_addr.get("url_list") or []
    download_url: str = url_list[0] if url_list else ""
    file_size: int = int(play_addr.get("data_size") or 0)

    return [{
        "content_id": aweme_id,
        "media_kind": "video",
        "sequence": 0,
        "title": desc,
        "author": author_name,
        "file_size": file_size,
        "file_path": "",           # filled in by executor
        "download_url": download_url,
        "create_time": create_time,
    }]


async def fetch_post_detail(
    aweme_id: str,
    cookies: dict[str, str],
    *,
    tick: str = "10s",
) -> list[dict[str, Any]]:
    """Fetch one aweme via detail endpoint and normalize to canonical items."""
    params: dict[str, Any] = {
        "aweme_id": aweme_id,
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "pc_client_type": "1",
        "version_code": "170400",
        "version_name": "17.4.0",
        "cookie_enabled": "true",
        "screen_width": "1920",
        "screen_height": "1080",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "123.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "123.0.0.0",
        "os_name": "Windows",
        "os_version": "10",
        "cpu_core_num": "8",
        "device_memory": "8",
        "platform": "PC",
        "downlink": "10",
        "effective_type": "4g",
        "round_trip_time": "50",
        "update_version_code": "170400",
        "msToken": cookies.get("msToken", ""),
    }

    async with DouyinAPIClient(cookies, tick=tick) as client:
        data = await client._get_signed(_POST_DETAIL_ENDPOINT, params)

    raw = data.get("aweme_detail") or {}
    if not raw:
        return []
    return _normalise_items(raw)


async def fetch_all_posts(
    sec_uid: str,
    cookies: dict[str, str],
    *,
    tick: str = "10s",
    known_ids: set[str] | None = None,
    look_ahead_pages: int = 3,
) -> list[dict[str, Any]]:
    """
    Paginate through all posts for a given sec_uid.

    Args:
        known_ids: set of already-downloaded content_ids for dedup
        look_ahead_pages: how many pages to continue fetching after the first
                          fully-skipped page (cover recently hidden/unlocked posts)
    """
    known = known_ids or set()
    results: list[dict[str, Any]] = []
    cursor: int = 0
    skipped_pages_after_stop = 0

    async with DouyinAPIClient(cookies, tick=tick) as client:
        while True:
            items, cursor, has_more = await client.fetch_post_page(sec_uid, cursor)
            if not items:
                break

            page_new = 0
            for raw in items:
                for item in _normalise_items(raw):
                    if not item["content_id"]:
                        continue
                    if item["content_id"] in known:
                        continue
                    results.append(item)
                    page_new += 1

            if page_new == 0:
                skipped_pages_after_stop += 1
                if skipped_pages_after_stop >= look_ahead_pages:
                    break
            else:
                skipped_pages_after_stop = 0

            if not has_more:
                break

    return results
