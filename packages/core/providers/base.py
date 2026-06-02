from __future__ import annotations

import asyncio
import logging
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

from packages.core.config.models import parse_duration_to_seconds

logger = logging.getLogger(__name__)


def _parse_tick(tick: str) -> float:
    """Convert tick string like '10s', '30s', '1m' to seconds."""
    return parse_duration_to_seconds(tick)


class BaseAPIClient(ABC):
    """
    Async HTTP API client base with tick-based rate limiting.

    Subclasses must implement ``__aenter__`` / ``__aexit__`` (async context manager)
    and call ``await self._rate_limit()`` before each API request.
    """

    def __init__(self, *, tick: str) -> None:
        self._min_interval = _parse_tick(tick)
        self._last_request_ts: float = 0.0

    async def _rate_limit(self) -> None:
        """Wait if needed to enforce the configured tick interval."""
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_request_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_ts = time.monotonic()


class SyncRateLimiter:
    """Thread-safe synchronous rate limiter mixin for sync providers.

    Usage::

        class MyProvider(BaseProvider, SyncRateLimiter):
            ...

            def some_method(self):
                self.rate_limit("1s")
    """

    _api_lock: threading.Lock = threading.Lock()
    _last_api_ts: float = 0.0

    @classmethod
    def rate_limit(cls, tick: str) -> None:
        """Enforce minimum interval across ALL API calls for this provider."""
        interval = _parse_tick(tick)
        with cls._api_lock:
            now = time.monotonic()
            wait = interval - (now - cls._last_api_ts)
            if wait > 0:
                time.sleep(wait)
            cls._last_api_ts = time.monotonic()


class BaseProvider(ABC):
    platform: str
    account_types: list[str] = ["profile", "live"]
    _config_dir: Path | None = None

    @staticmethod
    def load_site_config(platform: str) -> dict[str, Any]:
        """Read site config for the given platform.

        Returns empty dict on any error (file missing, parse failure).
        """
        try:
            from packages.core.config.jsonc import load_jsonc
            config_dir = Path(__file__).resolve().parents[3] / "config"
            return load_jsonc(config_dir / "sites" / f"{platform}.jsonc")
        except Exception:
            return {}

    @abstractmethod
    def healthcheck(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def fetch_content_items(self, task_params: dict[str, Any], account_url: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def detect_live_status(self, task_params: dict[str, Any], account_url: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def build_live_session_payload(self, task_params: dict[str, Any], account_url: str) -> dict[str, Any]:
        raise NotImplementedError

    def build_account_dir(self, account: Any) -> str:
        platform_id = _safe(str(getattr(account, "platform_account_id", "") or ""), 50)
        alias = _safe(str(getattr(account, "account_alias", "") or ""), 50)
        account_url = _safe(str(getattr(account, "account_url", "") or ""), 80)
        return platform_id or alias or account_url or "account"

    def extract_account_key(self, account_url: str, account_type: str) -> str:
        """Extract a stable short key from an account URL for path disambiguation.

        Default: last non-empty path segment.  Platforms with non-standard URL
        formats (e.g. Douyin profile sec_uid) should override this.
        """
        return (account_url or "").rstrip("/").rsplit("/", 1)[-1]

    def build_content_file_path(
        self,
        item: dict[str, Any],
        *,
        creator_dir: str,
        account_dir: str,
        account_url: str,
    ) -> str:
        try:
            dt = datetime.fromtimestamp(float(item.get("create_time") or 0))
            date_str = dt.strftime("%Y-%m-%d %H.%M.%S")
        except (ValueError, OSError):
            date_str = "0000-00-00 00.00.00"

        title = _compact_title(str(item.get("title") or ""))
        prefix = f"{date_str}_{title}" if title else date_str
        media_kind = str(item.get("media_kind") or "video")
        seq = int(item.get("sequence", 0))
        ext = "mp4" if media_kind == "video" else "jpeg"
        return f"{creator_dir}/{self.platform}/{account_dir}/{prefix}_{media_kind}_{seq}.{ext}"

    def build_download_request(self, account_url: str) -> dict[str, Any]:
        return {"headers": {}, "cookies": {}}

    def refresh_content_item_for_download(
        self,
        *,
        content_id: str,
        media_kind: str,
        account_url: str,
        item: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return None

    def resolve_live_stream(
        self,
        task_params: dict[str, Any],
        account_url: str,
    ) -> dict[str, Any] | None:
        return None

    def build_live_download_request(self, account_url: str) -> dict[str, Any]:
        return {"headers": {}, "cookies": {}}

    def normalize_stream_url(self, url: str) -> str:
        """Normalize a live stream URL before download.

        Platforms can override to fix CDN scheme or path issues.
        """
        return url
