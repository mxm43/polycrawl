"""Xiaohongshu API signer — local reimplementation (no xhshow dependency)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from .xs_config import CryptoConfig
from .xs_signer import XHSignatureSigner, SessionManager

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)

_config = CryptoConfig().with_overrides(PUBLIC_USERAGENT=_USER_AGENT)
_signer = XHSignatureSigner(_config)
_session = SessionManager(_config)


def _cookie_string(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


_COMMON_HEADERS: dict[str, str] = {
    "authority": "edith.xiaohongshu.com",
    "origin": "https://www.xiaohongshu.com",
    "referer": "https://www.xiaohongshu.com/",
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json;charset=UTF-8",
    "accept-language": "zh-CN,zh;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),

    "x-mns": "unload",
}


def _build_full_headers(
    api: str,
    signed: dict[str, str],
    cookies: dict[str, str],
    payload: dict[str, Any] | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build complete request headers including x-rap-param and browser-like headers."""
    headers = dict(_COMMON_HEADERS)
    headers["cookie"] = _cookie_string(cookies)
    headers.update(signed)
    if extra:
        headers.update(extra)
    # x-rap-param — only for endpoints that need it
    from .xhs_rap import generate_rap_param, is_rap_needed
    if is_rap_needed(api) and payload is not None:
        try:
            rap = generate_rap_param(api, payload)
            if rap:
                headers["x-rap-param"] = rap
                if "/api/sns/web/v1/feed" in api:
                    headers["xy-direction"] = "13"
        except Exception:
            pass
    return headers


def _parse_ts_from_note_detail(note_card: dict[str, Any]) -> int:
    """Extract Unix timestamp from a feed API note_card response.

    The feed API returns ``time`` in milliseconds.
    Returns 0 if unavailable.
    """
    raw = note_card.get("time", 0) or 0
    try:
        ts = int(raw)
        if ts > 1_000_000_000_000:
            ts //= 1000
        if ts < 100_000_000:
            return 0
        return ts
    except (ValueError, TypeError):
        return 0


def _extract_video_url_from_detail(note_card: dict[str, Any]) -> str:
    """Extract the best-quality video URL from a feed API response.

    Structure: note_card.video.media.stream.h264[0].master_url
    Falls back to h265 or other streams.
    """
    video = note_card.get("video") or {}
    media = video.get("media") or {}
    stream = media.get("stream") or {}

    for codec in ("h264", "h265", "av1"):
        streams = stream.get(codec) or []
        if streams and isinstance(streams, list):
            best = streams[0]
            if isinstance(best, dict):
                url = best.get("master_url") or ""
                if url:
                    return str(url)
    return ""


def _extract_images_from_detail(note_card: dict[str, Any]) -> list[str]:
    """Extract all image URLs from a feed API note_card.

    Returns list of url_default for each image in image_list.
    """
    images: list[str] = []
    for img in note_card.get("image_list") or []:
        if isinstance(img, dict):
            url = img.get("url_default") or img.get("url_pre") or ""
            if url:
                images.append(str(url))
    return images


def _extract_live_photo_videos(note_card: dict[str, Any]) -> list[dict[str, str]]:
    """Extract live photo video URLs from a feed API note_card.

    Each live photo image in ``image_list`` may have ``live_photo: true``
    and a ``stream`` dict containing video URLs in the same format as
    regular video notes (``h264[0].master_url``).

    Returns list of {"image_url": str, "video_url": str}.
    """
    live_videos: list[dict[str, str]] = []
    for img in note_card.get("image_list") or []:
        if not isinstance(img, dict) or not img.get("live_photo"):
            continue
        image_url = str(img.get("url_default") or img.get("url_pre") or "")
        stream = img.get("stream") or {}
        video_url = ""
        for codec in ("h264", "h265", "av1"):
            streams = stream.get(codec) or []
            if streams and isinstance(streams, list) and isinstance(streams[0], dict):
                video_url = str(streams[0].get("master_url") or "")
                if video_url:
                    break
        if image_url and video_url:
            live_videos.append({"image_url": image_url, "video_url": video_url})
    return live_videos


def fetch_notes(
    user_id: str,
    cookies: dict[str, str],
    cursor: str = "",
) -> dict[str, Any]:
    """Fetch one page of notes for *user_id* using signed API.

    Returns:
        {"notes": [...], "has_more": bool, "cursor": str}
    """
    params: dict[str, str | int] = {
        "num": 30,
        "cursor": cursor,
        "user_id": user_id,
        "image_formats": "jpg,webp,avif",
        "xsec_token": "",
        "xsec_source": "",
    }

    headers = _signer.sign_headers_get(
        "/api/sns/web/v1/user_posted",
        cookies,
        params=params,
        timestamp=None,
        session=_session,
    )

    url = "https://edith.xiaohongshu.com/api/sns/web/v1/user_posted?" + "&".join(
        f"{k}={v}" for k, v in params.items()
    )

    full_headers = _build_full_headers(
        "/api/sns/web/v1/user_posted", headers, cookies, payload=params,
    )
    resp = httpx.get(url, headers=full_headers)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("success"):
        msg = str(data.get("msg", "unknown error"))
        logger.warning("[xhs] API error: %s", msg)
        # Treat auth failures as exceptions so the error is recorded in task.error_message
        if any(kw in msg.lower() for kw in ("verify", "login", "auth", "expired", "461", "471")):
            raise RuntimeError(f"Xiaohongshu API auth failure: {msg}")
        return {"notes": [], "has_more": False, "cursor": ""}

    result = data.get("data", {})
    return {
        "notes": result.get("notes", []),
        "has_more": result.get("has_more", False),
        "cursor": result.get("cursor", ""),
    }


def fetch_note_detail(
    note_id: str,
    xsec_token: str,
    cookies: dict[str, str],
) -> dict[str, Any] | None:
    """Fetch full detail for a single note via the feed API.

    Returns a dict with keys extracted from the note_card, or None on failure:
        {"note_id": str, "type": str, "create_time": int,
         "video_url": str, "image_urls": [str, ...]}
    """
    payload = {
        "source_note_id": note_id,
        "image_formats": ["jpg", "webp", "avif"],
        "extra": {"need_body_topic": 1},
        "xsec_token": xsec_token,
    }

    headers = _signer.sign_headers_post(
        "/api/sns/web/v1/feed",
        cookies,
        payload=payload,
        timestamp=None,
        session=_session,
    )

    try:
        full_headers = _build_full_headers(
            "/api/sns/web/v1/feed", headers, cookies, payload=payload,
        )
        resp = httpx.post(
            "https://edith.xiaohongshu.com/api/sns/web/v1/feed",
            headers=full_headers,
            json=payload,
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (461, 471):
            raise RuntimeError(f"Xiaohongshu API auth failure: HTTP {exc.response.status_code}")
        logger.warning("[xhs] fetch_note_detail(%s) HTTP error: %s", note_id, exc)
        return None
    except Exception as exc:
        logger.warning("[xhs] fetch_note_detail(%s) failed: %s", note_id, exc)
        return None

    if not data.get("success"):
        msg = str(data.get("msg", "unknown error"))
        logger.warning("[xhs] fetch_note_detail(%s) success=false: %s", note_id, msg)
        if any(kw in msg.lower() for kw in ("verify", "login", "auth", "expired", "461", "471")):
            raise RuntimeError(f"Xiaohongshu API auth failure: {msg}")
        return None

    items = (data.get("data") or {}).get("items") or []
    if not items:
        return None

    note_card = items[0].get("note_card") or items[0]
    note_type = note_card.get("type") or ""
    create_time = _parse_ts_from_note_detail(note_card)
    video_url = _extract_video_url_from_detail(note_card) if note_type == "video" else ""
    image_urls = _extract_images_from_detail(note_card)
    live_photo_videos = _extract_live_photo_videos(note_card)

    result: dict[str, Any] = {
        "note_id": note_card.get("note_id") or note_id,
        "type": note_type,
        "create_time": create_time,
        "video_url": video_url,
        "image_urls": image_urls,
        "live_photo_videos": live_photo_videos,
    }
    return result
