"""Xiaohongshu API signer 鈥?uses RedCrack synchronously."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

# RedCrack uses absolute imports (from request.web...), so add its root to path
_RCROOT = str(Path(__file__).resolve().parent / "redcrack")
if _RCROOT not in sys.path:
    sys.path.insert(0, _RCROOT)

from request.web.xhs_session import (  # noqa: E402
    XHS_Session,
    create_xhs_session,
)

logger = logging.getLogger(__name__)

_EDITH = "https://edith.xiaohongshu.com"
_API_FEED = "/api/sns/web/v1/feed"


class _XHSClient:
    """Sync context manager wrapping a RedCrack session.

    Follows the same pattern as ``test_user_profile.py``:
    create session (full init), then override cookies with config values.
    """

    def __init__(self, cookies: dict[str, str], proxy: str | None = None) -> None:
        self._cookies = dict(cookies)
        self._proxy = proxy
        self._session: XHS_Session | None = None

    def __enter__(self) -> _XHSClient:
        web_session = self._cookies.get("web_session", "")
        self._session = create_xhs_session(web_session=web_session, proxy=self._proxy)
        # Clear all cookies then set config cookies (avoid CookieConflict)
        for key in list(self._session._session.cookies.keys()):
            self._session._session.cookies.pop(key)
        for key, value in self._cookies.items():
            self._session._session.cookies.set(key, value, domain="")
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._session:
            self._session.close_session()

    def search_user_notes(
        self, user_id: str, num: int = 30, cursor: str = ""
    ) -> dict[str, Any]:
        resp = self._session.apis.note.search_user_notes(
            user_id=user_id, num=num, cursor=cursor
        )
        return resp.json()

    def fetch_note_detail(
        self, note_id: str, xsec_token: str
    ) -> dict[str, Any] | None:
        body = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": 1},
            "xsec_token": xsec_token,
        }
        resp = self._session.request(method="post", url=_EDITH + _API_FEED, data=body)
        d = resp.json()
        if not (d.get("success") or d.get("msg") == "鎴愬姛"):
            return None
        nc = (d.get("data", {}).get("items", []) or [{}])[0].get("note_card", {})
        if not nc:
            return None
        urls, lvs = [], []
        for img in nc.get("image_list", []) or []:
            if u := img.get("url_default", "") or img.get("url", "") or img.get("url_pre", ""):
                urls.append(u)
            if img.get("live_photo") and (
                vu := (img.get("stream", {}) or {}).get("h264", [{}])[0].get("master_url", "")
            ):
                lvs.append({"video_url": vu})
        h264 = ((nc.get("video", {}) or {}).get("media", {}) or {}).get("stream", {}) or {}
        return {
            "time": nc.get("time", 0),
            "note_type": nc.get("type", "normal"),
            "image_urls": urls,
            "live_photo_videos": lvs,
            "video_url": h264.get("h264", [{}])[0].get("master_url", "") if h264.get("h264") else "",
        }


def fetch_notes(
    user_id: str,
    cookies: dict[str, str],
    cursor: str = "",
    num: int = 30,
    proxy: str | None = None,
) -> dict[str, Any]:
    with _XHSClient(cookies, proxy=proxy) as client:
        result = client.search_user_notes(user_id=user_id, num=num, cursor=cursor)
        if result.get("success") or result.get("msg") == "鎴愬姛":
            rd = result.get("data") or {}
            return {
                "notes": rd.get("notes", []),
                "has_more": rd.get("has_more", False),
                "cursor": rd.get("cursor", ""),
            }
        return {"notes": [], "has_more": False, "cursor": ""}


def fetch_note_detail(
    note_id: str,
    xsec_token: str,
    cookies: dict[str, str],
    proxy: str | None = None,
) -> dict[str, Any] | None:
    with _XHSClient(cookies, proxy=proxy) as client:
        return client.fetch_note_detail(note_id, xsec_token)
