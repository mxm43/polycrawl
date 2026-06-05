"""Twitter (X.com) content provider.

Uses X.com GraphQL API (not REST v1.1/v2) to fetch user media.
Cookie-based auth with ``auth_token`` + ``ct0``.

Endpoints:
  - UserByScreenName ➜ resolve user ID from @screen_name
  - UserMedia        ➜ fetch media tweets (500/request, cursor-paginated)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from packages.core.providers.base import BaseProvider, SyncRateLimiter

logger = logging.getLogger(__name__)

# ── GraphQL endpoint constants ──────────────────────────────
_USER_BY_SCREEN_NAME_OP = "xc8f1g7BYqr6VTzTbvNlGw"
_USER_MEDIA_OP = "Le6KlbilFmSu-5VltFND-Q"

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

_USER_MEDIA_FEATURES = {
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

        # Tweet time from edit_control
        edit_control = tweet.get("edit_control", {})
        edit_msecs = edit_control.get("editable_until_msecs", 0)
        create_time = None
        if edit_msecs:
            create_time = (int(edit_msecs) - 3_600_000) / 1000  # minus ~1h offset

        # Title from full_text
        full_text = str(legacy.get("full_text", ""))[:200]

        # Media extraction
        entities = legacy.get("extended_entities", {}) or {}
        media_list = entities.get("media", []) or []

        if not media_list:
            # Text-only tweet — skip (no media to download)
            continue

        for media in media_list:
            media_type = media.get("type", "")
            media_url_https = media.get("media_url_https", "")

            if media_type == "photo":
                results.append({
                    "content_id": tweet_id,
                    "media_kind": "image",
                    "sequence": 0,
                    "title": full_text,
                    "author": author_name,
                    "file_size": 0,
                    "file_path": "",
                    "download_url": f"{media_url_https}?format=png&name=4096x4096",
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
                        "sequence": 0,
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

    # ── Content fetch ──────────────────────────────────────

    def fetch_content_items(
        self,
        task_params: dict[str, Any],
        account_url: str,
    ) -> list[dict[str, Any]]:
        screen_name = _extract_screen_name(account_url)
        site_cfg = self.load_site_config("twitter")
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

        # Fetch media timeline
        return self._fetch_user_media(user_id, screen_name, cookies, cursor, tick)

    def fetch_content_item_by_id(
        self,
        content_id: str,
        account_url: str,
    ) -> dict[str, Any] | None:
        """Twitter doesn't have a simple item-level refresh endpoint.

        Returns None to trigger the standard retry mechanism.
        """
        return None

    # ── Live (not applicable) ──────────────────────────────

    def detect_live_status(self, task_params: dict[str, Any], account_url: str) -> bool:
        return False

    # ── Pagination state ───────────────────────────────────

    @property
    def has_more(self) -> bool:
        return self._has_more

    @property
    def next_cursor(self) -> int:
        return 0

    # ── Internal helpers ───────────────────────────────────

    def _resolve_user_id(
        self, screen_name: str, cookies: dict[str, str], tick: str
    ) -> str | None:
        """Resolve @screen_name to rest_id via UserByScreenName query."""
        self.rate_limit_with_jitter(tick)
        headers = _build_headers(cookies)
        variables = json.dumps({
            "screen_name": screen_name,
            "withSafetyModeUserFields": False,
        }, separators=(",", ":"))
        features = json.dumps(_USER_BY_SCREEN_NAME_FEATURES, separators=(",", ":"))
        field_toggles = json.dumps({"withAuxiliaryUserLabels": False}, separators=(",", ":"))

        url = (
            f"{_API_BASE}/{_USER_BY_SCREEN_NAME_OP}/UserByScreenName"
            f"?variables={variables}&features={features}&fieldToggles={field_toggles}"
        )

        try:
            resp = httpx.get(url, headers=headers, timeout=15)
            data = resp.json()
            return data["data"]["user"]["result"]["rest_id"]
        except Exception as exc:
            logger.warning("[twitter] UserByScreenName failed for %s: %s", screen_name, exc)
            return None

    def _fetch_user_media(
        self,
        user_id: str,
        screen_name: str,
        cookies: dict[str, str],
        cursor: str,
        tick: str,
    ) -> list[dict[str, Any]]:
        """Fetch UserMedia timeline, normalized to content items."""
        self.rate_limit_with_jitter(tick)
        headers = _build_headers(cookies)

        variables = {
            "userId": user_id,
            "count": 500,
            "includePromotedContent": False,
            "withClientEventToken": False,
            "withBirdwatchNotes": False,
            "withVoice": True,
            "withV2Timeline": True,
        }
        if cursor:
            variables["cursor"] = cursor

        vars_json = json.dumps(variables, separators=(",", ":"))
        features_json = json.dumps(_USER_MEDIA_FEATURES, separators=(",", ":"))

        url = (
            f"{_API_BASE}/{_USER_MEDIA_OP}/UserMedia"
            f"?variables={vars_json}&features={features_json}"
        )

        try:
            resp = httpx.get(url, headers=headers, timeout=30)
            data = resp.json()
        except Exception as exc:
            logger.warning("[twitter] UserMedia request failed for %s: %s", screen_name, exc)
            self._has_more = False
            return []

        # Extract timeline entries
        try:
            timeline = data["data"]["user"]["result"]["timeline_response"]["timeline"]
            instructions = timeline.get("instructions", [])
        except (KeyError, TypeError):
            logger.warning("[twitter] unexpected response structure for %s", screen_name)
            self._has_more = False
            return []

        entries: list[dict] = []
        next_cursor = ""

        for instruction in instructions:
            if instruction.get("type") == "TimelineAddEntries":
                entries = instruction.get("entries", [])
            elif instruction.get("type") == "TimelinePinEntry":
                # Pinned tweet, skip
                pass

        # Find cursor for pagination
        for entry in entries:
            entry_id = entry.get("entryId", "")
            if "cursor-bottom" in entry_id:
                content = entry.get("content", {})
                value = content.get("value", "")
                if value:
                    next_cursor = value
                    break

        self._has_more = bool(next_cursor) and next_cursor != cursor
        self._next_cursor = next_cursor

        # Resolve author display name from first entry
        author_name = screen_name
        try:
            legacy = entries[0].get("content", {}).get("itemContent", {}).get("tweet_results", {}).get("result", {}).get("legacy", {})
            core = legacy.get("user", {}).get("legacy", {})
            if core.get("name"):
                author_name = core.get("name", screen_name)
        except (IndexError, AttributeError, KeyError):
            pass

        return _normalise_items(entries, author_name, screen_name)


def build_provider() -> TwitterProvider:
    return TwitterProvider()
