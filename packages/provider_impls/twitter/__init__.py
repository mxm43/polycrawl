"""Twitter (X.com) content provider.

Uses X.com GraphQL API (not REST v1.1/v2) to fetch user media.
Cookie-based auth with ``auth_token`` + ``ct0``.

Endpoints:
  - UserByScreenName ➜ resolve user ID from @screen_name
  - UserTweets      ➜ fetch ALL tweets with pagination, filter for media
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from packages.core.providers.base import BaseProvider, SyncRateLimiter, _safe

logger = logging.getLogger(__name__)

# ── GraphQL endpoint constants ──────────────────────────────
_USER_BY_SCREEN_NAME_OP = "xc8f1g7BYqr6VTzTbvNlGw"
_USER_MEDIA_OP = "9zyyd1hebl7oNWIPdA8HRw"

_API_BASE = "https://x.com/i/api/graphql"

_USER_BY_SCREEN_NAME_FEATURES = {
    "hidden_profile_likes_enabled": False,
    "hidden_profile_subscriptions_enabled": False,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}

# UserTweets features (used for full historical pagination)
_USER_TWEETS_FEATURES = {
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": False,
    "tweet_awards_web_tipping_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_media_download_video_enabled": False,
    "responsive_web_enhance_cards_enabled": False,
}

# Static bearer token (X.com public client token)
_BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)


# ── helpers ────────────────────────────────────────────────

def _extract_screen_name(account_url: str) -> str:
    """Parse @screen_name from a Twitter/X profile URL.

    Supports:
      https://x.com/username
      https://twitter.com/username
      https://x.com/username/with_replies
      username  (bare)
    """
    account_url = account_url.strip().rstrip("/")
    # Remove URL prefix
    for prefix in ("https://x.com/", "https://twitter.com/", "http://x.com/", "http://twitter.com/"):
        if account_url.startswith(prefix):
            account_url = account_url[len(prefix):]
            break
    # Remove trailing path segments
    account_url = account_url.split("/")[0]
    # Strip @ if present
    account_url = account_url.lstrip("@")
    if not account_url or not account_url.replace("_", "").isalnum():
        raise ValueError(f"Cannot extract screen_name from Twitter URL: {account_url!r}")
    return account_url


# ── headers builder ────────────────────────────────────────

# Lazy cache for X.com ClientTransaction (anti-bot x-client-transaction-id)
_tid_cache: tuple | None = None
_TID_LOCK = threading.Lock()


def _ensure_tid_cache() -> None:
    """Initialize the ClientTransaction cache once (thread-safe)."""
    global _tid_cache
    if _tid_cache is not None:
        return
    with _TID_LOCK:
        if _tid_cache is not None:
            return
        try:
            import bs4
            from x_client_transaction import ClientTransaction
            from x_client_transaction.utils import generate_headers, get_ondemand_file_url

            session = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(15))
            home_resp = session.get("https://x.com")
            home_soup = bs4.BeautifulSoup(home_resp.content, "html.parser")
            ondemand_url = get_ondemand_file_url(response=home_soup)
            ondemand_resp = session.get(ondemand_url)
            ct = ClientTransaction(home_soup, ondemand_resp.text)
            _tid_cache = (ct, home_soup)
        except Exception:
            logger.warning("[twitter] failed to init ClientTransaction, continuing without TID header")
            _tid_cache = (None, None)


def _add_tid_header(headers: dict[str, str], url: str) -> None:
    """Add ``x-client-transaction-id`` header if the cache is available."""
    _ensure_tid_cache()
    ct, home_soup = _tid_cache if _tid_cache else (None, None)
    if ct is None:
        return
    try:
        path = urlparse(url).path
        tid = ct.generate_transaction_id(method="GET", path=path, home_page_response=home_soup)
        headers["x-client-transaction-id"] = tid
    except Exception:
        pass


def _build_headers(cookies: dict[str, str]) -> dict[str, str]:
    """Build HTTP headers for X.com GraphQL requests."""
    ct0 = cookies.get("ct0", "")
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items() if k != "__example__")
    return {
        "Authorization": f"Bearer {_BEARER_TOKEN}",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/134.0.0.0 Safari/537.36"
        ),
        "X-Csrf-Token": ct0,
        "Cookie": cookie_str,
        "Referer": "https://x.com/",
        "Origin": "https://x.com",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def _get_tweet_metadata(tweet: dict[str, Any]) -> dict[str, Any]:
    """Extract user info from a tweet result, handling nested structures."""
    result = tweet
    # Unwrap 'tweet' nesting (for some restricted/reply accounts)
    if "tweet" in result:
        result = result["tweet"]
    return result


def _normalise_items(
    entries: list[dict[str, Any]],
    author_name: str,
    author_screen_name: str,
) -> list[dict[str, Any]]:
    """Convert UserMedia timeline entries to canonical content items."""
    results: list[dict[str, Any]] = []

    for entry in entries:
        entry_id = entry.get("entryId", "")
        # Skip promoted (ads), cursor markers
        if "promoted" in entry_id or "cursor" in entry_id:
            continue
        if "tweet" not in entry_id:
            continue

        # Navigate the nested result structure
        try:
            content = entry.get("content", {})
            item_content = content.get("itemContent", {})
            tweet_result = item_content.get("tweet_results", {}).get("result", {})
        except AttributeError:
            continue

        if not tweet_result:
            continue

        tweet = _get_tweet_metadata(tweet_result)
        legacy = tweet.get("legacy", {})
        if not legacy:
            continue

        tweet_id = str(legacy.get("id_str", ""))
        if not tweet_id:
            continue

        # Tweet time: prefer legacy.created_at (always present), fall back to edit_control
        create_time = None
        created_at_str = legacy.get("created_at", "")
        if created_at_str:
            # Format: "Sun Mar 09 04:53:51 +0000 2025"
            try:
                from datetime import datetime
                dt = datetime.strptime(created_at_str, "%a %b %d %H:%M:%S %z %Y")
                create_time = dt.timestamp()
            except (ValueError, TypeError):
                pass
        if not create_time:
            # Fallback: edit_control.editable_until_msecs
            edit_control = tweet.get("edit_control", {})
            edit_msecs = edit_control.get("editable_until_msecs", 0)
            if edit_msecs:
                create_time = (int(edit_msecs) - 3_600_000) / 1000

        # Title from full_text (strip URLs)
        import re as _re
        full_text = str(legacy.get("full_text", ""))[:200]
        full_text = _re.sub(r"https?://\S+", "", full_text).strip()

        # Media extraction
        entities = legacy.get("extended_entities", {}) or {}
        media_list = entities.get("media", []) or []

        if not media_list:
            # Text-only tweet — skip (no media to download)
            continue

        for seq, media in enumerate(media_list):
            media_type = media.get("type", "")
            media_url_https = media.get("media_url_https", "")

            if media_type == "photo":
                results.append({
                    "content_id": tweet_id,
                    "media_kind": "image",
                    "sequence": seq,
                    "title": full_text,
                    "author": author_name,
                    "file_size": 0,
                    "file_path": "",
                    "download_url": f"{media_url_https}?name=orig",
                    "create_time": create_time,
                })
            elif media_type in ("video", "animated_gif"):
                video_info = media.get("video_info", {}) or {}
                variants = video_info.get("variants", []) or []
                # Pick highest bitrate variant
                best_url = None
                best_bitrate = -1
                for v in variants:
                    bitrate = v.get("bitrate", -1)
                    url = v.get("url", "")
                    if url and (bitrate > best_bitrate or (media_type == "animated_gif" and best_bitrate == -1)):
                        best_bitrate = bitrate
                        best_url = url
                if best_url:
                    results.append({
                        "content_id": tweet_id,
                        "media_kind": "video" if media_type == "video" else "gif",
                        "sequence": seq,
                        "title": full_text,
                        "author": author_name,
                        "file_size": 0,
                        "file_path": "",
                        "download_url": best_url,
                        "create_time": create_time,
                    })

    return results


# ── Provider class ──────────────────────────────────────────

class TwitterProvider(BaseProvider, SyncRateLimiter):
    platform = "twitter"
    account_types: list[str] = ["profile"]

    def __init__(self) -> None:
        self._next_cursor: str = ""
        self._has_more: bool = False

    def healthcheck(self) -> dict[str, str]:
        return {"platform": self.platform, "status": "ok"}

    def build_live_session_payload(
        self, task_params: dict[str, Any], account_url: str
    ) -> dict[str, Any]:
        """Twitter does not support live recording."""
        return {}

    # ── Content fetch ──────────────────────────────────────

    def fetch_content_items(
        self,
        task_params: dict[str, Any],
        account_url: str,
    ) -> list[dict[str, Any]]:
        screen_name = _extract_screen_name(account_url)
        site_cfg = self._get_site_config()
        platform_cfg = site_cfg.get("platform") or {}
        cookies: dict[str, str] = {
            k: str(v) for k, v in (platform_cfg.get("cookies") or {}).items()
            if v and k != "__example__"
        }

        tick = str(task_params.get("tick") or "3s")
        cursor: str = str(task_params.get("cursor") or "")

        # Resolve screen_name → user rest_id
        user_id = self._resolve_user_id(screen_name, cookies, tick)
        if not user_id:
            logger.warning("[twitter] failed to resolve user_id for %s", screen_name)
            return []

        # Fetch media timeline (UserTweets for full pagination)
        return self._fetch_user_tweets(user_id, screen_name, cookies, cursor, tick)

    def fetch_content_item_by_id(
        self,
        content_id: str,
        account_url: str,
    ) -> dict[str, Any] | None:
        """Twitter doesn't have a simple item-level refresh endpoint.

        Returns None to trigger the standard retry mechanism.
        """
        return None

    def refresh_content_item_for_download(
        self,
        *,
        content_id: str,
        media_kind: str,
        account_url: str,
        item: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Fallback: if ``name=orig`` 404'd, retry with ``name=4096x4096``."""
        if not item or media_kind != "image":
            return None
        old_url = str(item.get("download_url") or "")
        if "name=orig" not in old_url:
            return None
        # Replace name=orig with name=4096x4096
        new_url = old_url.replace("name=orig", "name=4096x4096")
        if new_url == old_url:
            return None
        return {"download_url": new_url, "create_time": item.get("create_time", 0)}

    def build_download_request(self, account_url: str) -> dict[str, Any]:
        """Return cookies for Twitter media download requests."""
        site_cfg = self._get_site_config()
        platform_cfg = site_cfg.get("platform") or {}
        cookies: dict[str, str] = {
            k: str(v) for k, v in (platform_cfg.get("cookies") or {}).items()
            if v and k != "__example__"
        }
        return {"headers": {}, "cookies": cookies}

    # ── Live (not applicable) ──────────────────────────────

    def detect_live_status(self, task_params: dict[str, Any], account_url: str) -> bool:
        return False

    # ── Pagination state ───────────────────────────────────

    @property
    def has_more(self) -> bool:
        return self._has_more

    @property
    def next_cursor(self) -> str:
        return self._next_cursor

    def extract_account_key(self, account_url: str, account_type: str) -> str:
        """Extract @screen_name from Twitter/X profile URL for directory naming."""
        try:
            return _extract_screen_name(account_url)
        except ValueError:
            return super().extract_account_key(account_url, account_type)

    def build_account_dir(self, account: Any) -> str:
        """Use screen_name as the account directory name for Twitter."""
        try:
            screen_name = _extract_screen_name(getattr(account, "account_url", "") or "")
            return _safe(screen_name, 50)
        except ValueError:
            return super().build_account_dir(account)

    # ── Internal helpers ───────────────────────────────────

    def _resolve_user_id(
        self, screen_name: str, cookies: dict[str, str], tick: str,
    ) -> str | None:
        """Resolve @screen_name to rest_id via UserByScreenName query."""
        self.rate_limit_with_jitter(tick)
        url_base = f"{_API_BASE}/{_USER_BY_SCREEN_NAME_OP}/UserByScreenName"
        headers = _build_headers(cookies)
        _add_tid_header(headers, url_base)
        variables = json.dumps({
            "screen_name": screen_name,
            "withSafetyModeUserFields": False,
        }, separators=(",", ":"))
        features = json.dumps(_USER_BY_SCREEN_NAME_FEATURES, separators=(",", ":"))
        field_toggles = json.dumps({"withAuxiliaryUserLabels": False}, separators=(",", ":"))

        client_kwargs = {"headers": headers, "timeout": httpx.Timeout(15)}
        if self.proxy:
            client_kwargs["proxy"] = self.proxy

        try:
            with httpx.Client(**client_kwargs) as client:
                resp = client.get(url_base, params={
                    "variables": variables,
                    "features": features,
                    "fieldToggles": field_toggles,
                })
                data = resp.json()
                return data["data"]["user"]["result"]["rest_id"]
        except Exception as exc:
            logger.warning("[twitter] UserByScreenName failed for %s: %s", screen_name, exc)
            return None

    def _fetch_user_tweets(
        self,
        user_id: str,
        screen_name: str,
        cookies: dict[str, str],
        cursor: str,
        tick: str,
    ) -> list[dict[str, Any]]:
        """Fetch ALL tweets via UserTweets (supports full pagination), then filter for media.

        Handles 429 rate limits with automatic backoff and retry.
        """
        headers = _build_headers(cookies)
        url_base = f"{_API_BASE}/{_USER_MEDIA_OP}/UserTweets"
        _add_tid_header(headers, url_base)

        variables = {
            "userId": user_id,
            "count": 20,
            "includePromotedContent": False,
            "withVoice": True,
            "withV2Timeline": True,
        }
        if cursor:
            variables["cursor"] = cursor

        vars_json = json.dumps(variables, separators=(",", ":"))
        features_json = json.dumps(_USER_TWEETS_FEATURES, separators=(",", ":"))

        client_kwargs = {"headers": headers, "timeout": httpx.Timeout(30)}
        if self.proxy:
            client_kwargs["proxy"] = self.proxy

        # Retry loop with backoff for rate limits
        max_retries = 3
        for attempt in range(max_retries + 1):
            self.rate_limit_with_jitter(tick)
            try:
                with httpx.Client(**client_kwargs) as client:
                    url_base = f"{_API_BASE}/{_USER_MEDIA_OP}/UserTweets"
                    resp = client.get(url_base, params={
                        "variables": vars_json,
                        "features": features_json,
                    })
                    if resp.status_code == 429:
                        wait = 60 * (attempt + 1)
                        logger.warning("[twitter] 429 rate limited for %s, waiting %ds (attempt %d/%d)",
                                       screen_name, wait, attempt + 1, max_retries)
                        time.sleep(wait)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    break
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 and attempt < max_retries:
                    wait = 60 * (attempt + 1)
                    logger.warning("[twitter] 429 rate limited for %s, waiting %ds (attempt %d/%d)",
                                   screen_name, wait, attempt + 1, max_retries)
                    time.sleep(wait)
                    continue
                logger.warning("[twitter] UserTweets HTTP error for %s: %s", screen_name, exc)
                self._has_more = False
                return []
            except Exception as exc:
                logger.warning("[twitter] UserTweets request failed for %s: %s", screen_name, exc)
                self._has_more = False
                return []

        # Extract timeline entries
        try:
            timeline = data["data"]["user"]["result"]["timeline_v2"]["timeline"]
            instructions = timeline.get("instructions", [])
        except (KeyError, TypeError):
            logger.warning("[twitter] unexpected response structure for %s", screen_name)
            self._has_more = False
            return []

        raw_entries: list[dict] = []
        next_cursor = ""
        author_name = screen_name

        for instruction in instructions:
            if instruction.get("type") != "TimelineAddEntries":
                continue
            for entry in instruction.get("entries", []):
                entry_id = entry.get("entryId", "")
                content = entry.get("content", {})
                content_type = content.get("__typename", "")

                if content_type == "TimelineTimelineModule":
                    # New format: tweets grouped in a profile-grid module
                    for item in content.get("items", []):
                        item_content = item.get("item", {}).get("itemContent", {})
                        if item_content.get("__typename") == "TimelineTweet":
                            tweet_result = item_content.get("tweet_results", {}).get("result", {})
                            if tweet_result:
                                raw_entries.append({
                                    "entryId": item.get("entryId", ""),
                                    "content": {"itemContent": item_content},
                                })
                                # Extract author from first tweet
                                if author_name == screen_name:
                                    try:
                                        legacy = tweet_result.get("legacy", {})
                                        core = legacy.get("user", {}).get("legacy", {})
                                        if core.get("name"):
                                            author_name = core.get("name", screen_name)
                                    except (AttributeError, KeyError):
                                        pass
                elif "tweet" in entry_id and "promoted" not in entry_id:
                    # Old format: direct tweet entries
                    raw_entries.append(entry)
                    # Extract author from first tweet
                    if author_name == screen_name:
                        try:
                            legacy = content.get("itemContent", {}).get("tweet_results", {}).get("result", {}).get("legacy", {})
                            core = legacy.get("user", {}).get("legacy", {})
                            if core.get("name"):
                                author_name = core.get("name", screen_name)
                        except (AttributeError, KeyError):
                            pass
                elif "cursor-bottom" in entry_id:
                    value = content.get("value", "")
                    if value:
                        next_cursor = value

        self._has_more = bool(next_cursor) and next_cursor != cursor
        self._next_cursor = next_cursor

        return _normalise_items(raw_entries, author_name, screen_name)


def build_provider() -> TwitterProvider:
    return TwitterProvider()
