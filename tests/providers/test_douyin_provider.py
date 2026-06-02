from __future__ import annotations

from unittest.mock import AsyncMock, patch

from packages.provider_impls.douyin import DouyinProvider, build_provider
from packages.provider_impls.douyin.api_client import _extract_sec_uid, _normalise_items


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------

def test_extract_sec_uid() -> None:
    url = "https://www.douyin.com/user/MS4wLjABAAAAtest"
    assert _extract_sec_uid(url) == "MS4wLjABAAAAtest"


def test_normalise_item_minimal() -> None:
    raw = {
        "aweme_id": "7001234567890",
        "desc": "hello",
        "author": {"nickname": "Bob"},
        "video": {
            "play_addr": {"url_list": ["https://cdn.example.com/v.mp4"], "data_size": 1024},
        },
        "music": {"play_url": {"url_list": ["https://cdn.example.com/m.mp3"]}},
        "create_time": 1700000000,
    }
    items = _normalise_items(raw)
    assert len(items) == 1
    item = items[0]
    assert item["content_id"] == "7001234567890"
    assert item["media_kind"] == "video"
    assert item["title"] == "hello"
    assert item["author"] == "Bob"
    assert item["file_size"] == 1024
    assert item["download_url"] == "https://cdn.example.com/v.mp4"


def test_douyin_provider_provider_hooks() -> None:
    provider = build_provider()

    class _Account:
        platform_account_id = ""
        account_alias = ""
        account_url = "https://www.douyin.com/user/MS4wLjABAAAAtest"

    account_dir = provider.build_account_dir(_Account())
    assert account_dir == "MS4wLjABAAAAtest"

    path = provider.build_content_file_path(
        {
            "content_id": "7001234567890",
            "media_kind": "video",
            "title": "hello world",
            "create_time": 1700000000,
        },
        creator_dir="creator_a",
        account_dir=account_dir,
        account_url=_Account.account_url,
    )
    assert path.startswith("creator_a/douyin/MS4wLjABAAAAtest/")
    assert path.endswith(".mp4")


def test_douyin_provider_first_embedded_video_name_has_video_0_suffix() -> None:
    provider = build_provider()

    path = provider.build_content_file_path(
        {
            "content_id": "7001234567890_vid1",
            "media_kind": "video",
            "title": "鍜屾垜璋堟亱鐖卞惂鍒汉缁夸綘鎴戜笉鏀惧績鎺ㄨ崘浣犵殑濂冲弸",
            "create_time": 1612543282,
        },
        creator_dir="creator_a",
        account_dir="acc",
        account_url="https://www.douyin.com/user/MS4wLjABAAAAtest",
    )
    assert path.endswith(".mp4")
    assert "_video_0" in path


# ---------------------------------------------------------------------------
# Provider-level tests (no cookies 鈫?stub path)
# ---------------------------------------------------------------------------

def test_douyin_provider_content_items_stub_when_no_cookies() -> None:
    """Without cookies the provider returns a predictable stub item."""
    provider = build_provider()
    # Patch _load_site_config to return no cookies
    with patch("packages.provider_impls.douyin._load_site_config", return_value={}):
        items = provider.fetch_content_items({}, "https://www.douyin.com/user/test_uid")
    assert isinstance(items, list)
    assert len(items) == 1
    assert items[0]["media_kind"] == "video"
    assert "stub-" in items[0]["content_id"]


def test_douyin_provider_content_items_non_profile_url() -> None:
    """A non-profile URL (cannot extract sec_uid) returns empty list with cookies."""
    provider = build_provider()
    with patch(
        "packages.provider_impls.douyin._load_site_config",
        return_value={"platform": {"cookies": {"msToken": "abc"}}},
    ), patch(
        "packages.provider_impls.douyin._load_base_config",
        return_value={"strategy": {"incremental": {"tick": "10s"}}},
    ):
        items = provider.fetch_content_items({}, "https://live.douyin.com/12345")
    assert items == []


def test_douyin_provider_content_items_real_path() -> None:
    """With cookies and a valid profile URL, fetch_all_posts is called."""
    provider = build_provider()
    fake_items = [
        {
            "content_id": "777",
            "media_kind": "video",
            "title": "t",
            "author": "a",
            "file_size": 0,
            "file_path": "",
            "download_url": "https://example.com/v.mp4",
            "music_url": "",
            "cover_url": "",
            "create_time": 0,
        }
    ]
    with patch("packages.provider_impls.douyin._load_site_config", return_value={
        "platform": {"cookies": {"msToken": "tok"}},
    }), patch(
        "packages.provider_impls.douyin._load_base_config",
        return_value={"strategy": {"incremental": {"tick": "10s"}}},
    ), patch.object(
        DouyinProvider, "_fetch_page",
        new=AsyncMock(return_value=fake_items),
    ):
        items = provider.fetch_content_items({"cursor": 0, "tick": "10s"}, "https://www.douyin.com/user/ABC")
    assert items == fake_items


def test_douyin_provider_live_helpers() -> None:
    provider = build_provider()
    # No cookies 鈫?falls back to task_params["is_live"]
    with patch("packages.provider_impls.douyin._load_site_config", return_value={}):
        assert provider.detect_live_status({"is_live": True}, "https://live.douyin.com/1") is True
    payload = provider.build_live_session_payload(
        {
            "duration_seconds": 11,
            "total_bytes": 22,
            "segment_count": 3,
            "output_file_path": "x.ts",
            "stop_requested": True,
            "simulate_disconnect": True,
            "recover_window_seconds": 12,
            "fast_reconnect_seconds": [1, 2, 3],
        },
        "https://live.douyin.com/1",
    )
    assert payload["duration_seconds"] == 11
    assert payload["total_bytes"] == 22
    assert payload["segment_count"] == 3
    assert payload["output_file_path"] == "x.ts"
    assert payload["stop_requested"] is True
    assert payload["simulate_disconnect"] is True
    assert payload["recover_window_seconds"] == 12
    assert payload["fast_reconnect_seconds"] == [1, 2, 3]
