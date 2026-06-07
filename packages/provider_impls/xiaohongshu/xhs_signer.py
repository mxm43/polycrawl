"""Xiaohongshu API signer — uses RedCrack as a library for authentication."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

from packages.provider_impls.xiaohongshu.redcrack.request.web.xhs_session import (
    create_xhs_session,
)

logger = logging.getLogger(__name__)

_EDITH = "https://edith.xiaohongshu.com"
_API_FEED = "/api/sns/web/v1/feed"


class _XHSClient:
    """Async context manager wrapping a RedCrack session.

    Usage::

        async with _XHSClient(cookies) as client:
            notes = await client.search_user_notes(user_id, cursor)
    """

    def __init__(self, cookies: dict[str, str]) -> None:
        self._cookies = dict(cookies)
        self._session = None

    async def __aenter__(self) -> _XHSClient:
        web_session = self._cookies.get("web_session", "")
        self._session = await create_xhs_session(web_session=web_session)
        # Override auto-generated cookies with config cookies
        for key, value in self._cookies.items():
            self._session._session.cookie_jar.update_cookies({key: value})
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._session:
            await self._session.close_session()

    async def search_user_notes(
        self, user_id: str, num: int = 30, cursor: str = ""
    ) -> dict[str, Any]:
        """Fetch user's posted notes (listing API)."""
        resp = await self._session.apis.note.search_user_notes(
            user_id=user_id, num=num, cursor=cursor
        )
        return await resp.json()

    async def fetch_note_detail(
        self, note_id: str, xsec_token: str
    ) -> dict[str, Any] | None:
        """Fetch full note detail (feed API) for enrichment."""
        body = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": 1},
            "xsec_token": xsec_token,
        }
        resp = await self._session.request(
            method="post", url=_EDITH + _API_FEED, data=body
        )
        d = await resp.json()
        if not (d.get("success") or d.get("msg") == "成功"):
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
) -> dict[str, Any]:
    """Fetch user's posted notes (synchronous wrapper)."""

    async def _run() -> dict[str, Any]:
        async with _XHSClient(cookies) as client:
            result = await client.search_user_notes(
                user_id=user_id, num=num, cursor=cursor
            )
            if result.get("success") or result.get("msg") == "成功":
                rd = result.get("data") or {}
                return {
                    "notes": rd.get("notes", []),
                    "has_more": rd.get("has_more", False),
                    "cursor": rd.get("cursor", ""),
                }
            return {"notes": [], "has_more": False, "cursor": ""}

    return asyncio.run(_run())


def fetch_note_detail(
    note_id: str,
    xsec_token: str,
    cookies: dict[str, str],
) -> dict[str, Any] | None:
    """Fetch full note detail (synchronous wrapper)."""

    async def _run() -> dict[str, Any] | None:
        async with _XHSClient(cookies) as client:
            return await client.fetch_note_detail(note_id, xsec_token)

    return asyncio.run(_run())
