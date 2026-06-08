"""Xiaohongshu (灏忕孩涔? content provider.

Uses a pure-Python reimplementation of the browser's ``window.mnsv2``
signing algorithm to sign API requests for fetching user notes.
Cookies (from ``config/sites/xiaohongshu.jsonc``) are required 鈥?
the ``a1`` cookie is the main auth token.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from packages.core.providers.base import BaseProvider, SyncRateLimiter
from packages.provider_impls.xiaohongshu.xhs_signer import fetch_notes, fetch_note_detail

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_DIR = _ROOT / "config"


def _load_site_config() -> dict[str, Any]:
    return BaseProvider.load_site_config("xiaohongshu")


def _extract_user_id(account_url: str) -> str:
    """Parse user_id from a Xiaohongshu profile URL."""

    account_url = account_url.strip().rstrip("/")
    # Bare user_id 鈥?accept both upper and lower case hex
    m = re.match(r"^([a-fA-F0-9]{24})$", account_url)
    if m:
        return m.group(1).lower()
    # Full URL 鈥?look for hex segment in path
    parts = account_url.split("/")
    for part in reversed(parts):
        m = re.match(r"^([a-fA-F0-9]{24})$", part)
        if m:
            return m.group(1).lower()
    raise ValueError(f"Cannot extract user_id from Xiaohongshu URL: {account_url}")


def _extract_user_id_or_none(account_url: str) -> str | None:
    try:
        return _extract_user_id(account_url)
    except ValueError:
        return None


def _get_image_url(note: dict[str, Any]) -> list[str]:
    """Extract the cover image URL from a listing API note.

    The listing API only returns the **cover** image, not all images in a
    multi-image note.  ``cover.info_list`` contains only different resolutions
    of the same cover (WB_PRV / WB_DFT), not distinct note images.

    Returns a single-element list with the cover URL, or empty list.
    Full multi-image support requires a separate feed API call
    (see ``refresh_content_item_for_download``).
    """
    cover = note.get("cover") or {}
    url = cover.get("url_default") or cover.get("url_pre") or ""
    if url:
        return [url]
    # Try info_list as last resort
    for entry in cover.get("info_list") or []:
        if isinstance(entry, dict):
            u = entry.get("url") or ""
            if u:
                return [u]
    return []


def _get_video_url(note: dict[str, Any]) -> str:
    """Extract video URL from a note dict (video notes only).

    The listing API only returns cover images.  Real video URLs need
    a separate note detail call.  The ``video`` dict in listing
    responses may contain ``media`` with stream info 鈥?try that first,
    otherwise return empty (consumer falls back to cover image).
    """
    # Try the note-level video.media stream info
    video = note.get("video") or {}
    media = video.get("media") or video.get("stream") or {}
    if isinstance(media, dict):
        for candidate in ("master_url", "url", "play_url", "video_url"):
            val = media.get(candidate) or ""
            if val and str(val).startswith("http"):
                return str(val)
    # Try media_list array
    media_list = video.get("media_list") or video.get("stream_list") or []
    if media_list:
        first = media_list[0]
        if isinstance(first, dict):
            for candidate in ("master_url", "url", "play_url"):
                val = first.get(candidate) or ""
                if val and str(val).startswith("http"):
                    return str(val)
    return ""


def _parse_create_time(note: dict[str, Any]) -> int:
    """Extract create_time from a note.  Returns Unix timestamp."""
    # 1. Try the API's time field (listing API doesn't have it, feed API does)
    raw = note.get("time", 0) or 0
    if raw:
        try:
            ts = int(raw)
            if ts > 1_000_000_000_000:
                ts //= 1000
            if ts < 100_000_000:
                return 0
            return ts
        except (ValueError, TypeError):
            pass

    # 2. Fallback: extract timestamp from note_id (first 8 hex chars).
    #    Xiaohongshu note_id format: hex(<unix_ts_seconds>) + padding + suffix
    #    e.g. "6a1413420000000008003702" 鈫?0x6a141342 = 1779700546
    note_id = str(note.get("note_id") or "")
    if len(note_id) >= 8:
        try:
            ts = int(note_id[:8], 16)
            if 100_000_000 < ts < 2_000_000_000:
                return ts
        except (ValueError, TypeError):
            pass

    return 0


class XiaohongshuProvider(BaseProvider, SyncRateLimiter):
    platform = "xiaohongshu"
    account_types: list[str] = ["profile"]

    def __init__(self) -> None:
        self._next_cursor: int = 0
        self._has_more: bool = False

    def healthcheck(self) -> dict[str, str]:
        return {"platform": self.platform, "status": "ok"}

    # Content fetch

    def fetch_content_items(
        self,
        task_params: dict[str, Any],
        account_url: str,
    ) -> list[dict[str, Any]]:
        user_id = _extract_user_id(account_url)
        site_cfg = _load_site_config()
        cookies: dict[str, str] = {
            k: str(v) for k, v in (site_cfg.get("cookies") or {}).items() if v
        }
        proxy: str | None = site_cfg.get("proxy") or None

        tick: str = str(task_params.get("tick") or "1s")

        # Rate-limit before listing API call
        self.rate_limit_with_jitter(tick, task_params.get("jitter"), label="listing")
        cursor: str = str(task_params.get("cursor") or "")
        result = fetch_notes(user_id, cookies, cursor=cursor, proxy=proxy)
        raw_notes = result.get("notes", [])

        # Enrich each note via feed API
        # Reuse a single session for all note detail calls within this account,
        # matching the RedCrack test flow (one session, many requests).
        enriched_notes: list[dict[str, Any]] = []
        from packages.provider_impls.xiaohongshu.xhs_signer import _XHSClient
        with _XHSClient(cookies, proxy=proxy) as client:
            for note in raw_notes:
                note_id = str(note.get("note_id") or "")
                xsec_token = str(note.get("xsec_token") or "")
                if note_id and xsec_token and cookies:
                    try:
                        self.rate_limit_with_jitter(tick, task_params.get("jitter"), label="enrich")
                        detail = client.fetch_note_detail(note_id, xsec_token)
                        if detail:
                            note["_enriched"] = detail
                    except RuntimeError:
                        raise
                    except Exception:
                        pass
                enriched_notes.append(note)

        items = self._parse_notes(enriched_notes)
        has_more = result.get("has_more", False)
        self._next_cursor = result.get("cursor", "")
        self._has_more = has_more
        return items

    def _parse_notes(self, raw_notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert Xiaohongshu API notes to canonical content items.

        Uses ``_enriched`` data from the feed API (attached by ``fetch_content_items``)
        when available, falling back to listing-only data otherwise.
        """
        results: list[dict[str, Any]] = []

        for note in raw_notes:
            note_id = str(note.get("note_id") or "")
            if not note_id:
                continue

            enriched: dict[str, Any] | None = note.get("_enriched")
            note_type = note.get("type", "normal")
            is_image = note_type in ("normal", "image")
            title = str(note.get("display_title") or "")
            author = str((note.get("user") or {}).get("nickname", ""))
            # Use enriched create_time if available, otherwise fall back to note_id hex
            create_time = (enriched or {}).get("create_time", 0) or _parse_create_time(note)
            xsec_token = str(note.get("xsec_token") or "")

            if is_image:
                # Enriched: use real image_list from feed API
                enriched_images = (enriched or {}).get("image_urls") or []
                if enriched_images:
                    for seq, url in enumerate(enriched_images):
                        results.append({
                            "content_id": note_id,
                            "media_kind": "image",
                            "sequence": seq,
                            "title": title,
                            "author": author,
                            "file_size": 0,
                            "file_path": "",
                            "download_url": url,
                            "create_time": create_time,
                        })
                    # Live photo videos from enriched data
                    live_videos = (enriched or {}).get("live_photo_videos") or []
                    for lv_idx, lv in enumerate(live_videos):
                        video_url = lv.get("video_url", "")
                        if video_url:
                            # Use note_id + image index as content_id so each
                            # live photo video is a unique artifact
                            live_content_id = f"{note_id}_live_{lv_idx}"
                            results.append({
                                "content_id": live_content_id,
                                "media_kind": "video",
                                "sequence": lv_idx,
                                "title": title,
                                "author": author,
                                "file_size": 0,
                                "file_path": "",
                                "download_url": video_url,
                                "create_time": create_time,
                            })
                else:
                    # Fallback: cover image from listing
                    cover_urls = _get_image_url(note)
                    for seq, url in enumerate(cover_urls):
                        results.append({
                            "content_id": note_id,
                            "media_kind": "image",
                            "sequence": seq,
                            "title": title,
                            "author": author,
                            "file_size": 0,
                            "file_path": "",
                            "download_url": url,
                            "create_time": create_time,
                        })

            elif note_type == "video":
                # Enriched: use real video URL from feed API
                enriched_video = (enriched or {}).get("video_url") or ""
                if enriched_video:
                    results.append({
                        "content_id": note_id,
                        "media_kind": "video",
                        "sequence": 0,
                        "title": title,
                        "author": author,
                        "file_size": 0,
                        "file_path": "",
                        "download_url": enriched_video,
                        "create_time": create_time,
                    })
                else:
                    # Fallback: empty URL 鈫?consumer tries refresh
                    results.append({
                        "content_id": note_id,
                        "media_kind": "video",
                        "sequence": 0,
                        "title": title,
                        "author": author,
                        "file_size": 0,
                        "file_path": "",
                        "download_url": "",
                        "create_time": create_time,
                        "xsec_token": xsec_token,
                        "_fallback_image": (_get_image_url(note) or [""])[0],
                    })

        return results

    # Pagination state

    @property
    def next_cursor(self) -> str:
        return getattr(self, "_next_cursor", "")

    @property
    def has_more(self) -> bool:
        return getattr(self, "_has_more", False)

    # Live (not applicable)

    def detect_live_status(self, task_params: dict[str, Any], account_url: str) -> bool:
        return False

    def build_live_session_payload(self, task_params: dict[str, Any], account_url: str) -> dict[str, Any]:
        return {}

    def refresh_content_item_for_download(
        self,
        *,
        content_id: str,
        media_kind: str,
        account_url: str,
        item: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Enrich a note via the feed API to get actual video URL and creation time.

        The listing API doesn't return ``time`` or video URLs.  This method
        calls the note detail endpoint to fill in those gaps.
        """
        site_cfg = _load_site_config()
        cookies: dict[str, str] = {
            k: str(v) for k, v in (site_cfg.get("cookies") or {}).items() if v
        }
        if not cookies:
            return None

        note_id = content_id
        xsec_token = (item or {}).get("xsec_token") or ""

        detail = fetch_note_detail(note_id, xsec_token, cookies)
        if detail is None:
            return None

        result: dict[str, Any] = {}

        # For video notes, return the actual video URL
        if detail.get("type") == "video":
            video_url = detail.get("video_url") or ""
            if video_url:
                result["download_url"] = video_url
                result["media_kind"] = "video"

        # Always return create_time so the consumer can update artifact.publish_date
        create_time = detail.get("create_time", 0)
        if create_time:
            result["create_time"] = create_time

        return result if result else None

    # Download headers

    def build_download_request(self, account_url: str) -> dict[str, Any]:
        return {
            "headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/147.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.xiaohongshu.com/",
                "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            "cookies": {},
        }

    # Account directory

    def build_account_dir(self, account: Any) -> str:
        """Use the user_id extracted from account_url as directory name."""
        from packages.core.providers.base import _safe

        account_url = str(getattr(account, "account_url", "") or "").strip()
        user_id = _extract_user_id_or_none(account_url)
        if user_id:
            return _safe(user_id, 50)

        pid = str(getattr(account, "platform_account_id", "") or "").strip()
        if pid:
            return _safe(pid, 50)
        alias = str(getattr(account, "account_alias", "") or "").strip()
        if alias:
            return _safe(alias, 50)
        return _safe(account_url, 80) or "account"

    # URL parsing

    def extract_account_key(self, account_url: str, account_type: str) -> str:
        return _extract_user_id(account_url)


def build_provider() -> XiaohongshuProvider:
    return XiaohongshuProvider()
