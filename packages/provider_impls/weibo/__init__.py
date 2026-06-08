"""
Weibo (微博) content provider.

Scrapes weibo.cn HTML to fetch user posts.
Cookies (from config/sites/weibo.jsonc) are required for authentication.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from packages.core.providers.base import BaseProvider, SyncRateLimiter
from packages.core.utils import sanitize_filename

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_DIR = _ROOT / "config"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

# Regex patterns for parsing weibo.cn HTML
_RE_WEIBO_ITEM = re.compile(
    r'<div class="c" id="M_(\w+)".*?(?=<div class="c" id="M_|\Z)',
    re.DOTALL,
)
_RE_WEIBO_TEXT = re.compile(r'<span class="ctt">(.*?)</span>', re.DOTALL)
_RE_WEIBO_TIME = re.compile(r'<span class="ct">(.*?)</span>', re.DOTALL)
_RE_WEIBO_PIC_LINK = re.compile(r'<a[^>]*href="(https?://weibo\.cn/mblog/pic/[^"]+)"[^>]*>')
_RE_WEIBO_IMG = re.compile(r'<img[^>]*src="(https?://[^"]+)"[^>]*>')
_RE_STRIP_TAGS = re.compile(r'<[^>]+>')

_RE_SIZE_VARIANT = re.compile(r'/(mw\d+|wap\d+|bmiddle|thumb\d+|orj\d+|woriginal)/')


def _to_large(url: str) -> str:
    """Convert any Weibo image size variant to /large/ original quality."""
    return _RE_SIZE_VARIANT.sub("/large/", url)


def _load_site_config() -> dict[str, Any]:
    return BaseProvider.load_site_config("weibo")


def _extract_uid(account_url: str) -> str:
    """Parse numeric uid from a Weibo profile URL.

    Supports formats:
      https://weibo.cn/u/3217070567
      https://weibo.com/u/3217070567
      https://m.weibo.cn/profile/3217070567
      3217070567  (bare uid)
    """
    account_url = account_url.strip().rstrip("/")
    # Already a bare numeric uid
    if account_url.isdigit():
        return account_url
    # Extract last path segment that is purely numeric
    parts = account_url.split("/")
    for part in reversed(parts):
        if part.isdigit():
            return part
    raise ValueError(f"Cannot extract uid from Weibo URL: {account_url}")


def _extract_uid_or_none(account_url: str) -> str | None:
    try:
        return _extract_uid(account_url)
    except ValueError:
        return None


# ── helpers ──────────────────────────────────────────────────────

def _parse_weibo_time(s: str) -> int:
    """Convert Weibo datetime string to Unix timestamp."""
    if not s:
        return 0
    # Formats: "Tue May 28 22:30:01 +0800 2024" or just a timestamp
    try:
        dt = datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
        return int(dt.timestamp())
    except ValueError:
        pass
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _extract_images(mblog: dict[str, Any]) -> list[str]:
    """Extract image URLs from a mblog dict."""
    pics = mblog.get("pics") or []
    return [pic.get("large", {}).get("url", "") or pic.get("url", "")
            for pic in pics if isinstance(pic, dict)]


def _extract_video_url(mblog: dict[str, Any]) -> str:
    """Extract video URL from mblog's page_info."""
    page_info = mblog.get("page_info") or {}
    if page_info.get("type") != "video":
        return ""
    media_info = page_info.get("media_info") or {}
    # Prefer h264 mp4, fall back to stream_url
    return (media_info.get("mp4_hd_url") or
            media_info.get("mp4_url") or
            media_info.get("stream_url") or "")


def _normalise_items(raw_cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Weibo API cards to canonical content items."""
    results: list[dict[str, Any]] = []
    for card in raw_cards:
        if card.get("card_type") != 9:
            continue
        mblog = card.get("mblog") or {}
        content_id = str(mblog.get("id") or "")
        if not content_id:
            continue

        # Text content
        text = str(mblog.get("text") or "")
        # Strip HTML tags for title
        title = re.sub(r"<[^>]+>", "", text)[:200]

        create_time = _parse_weibo_time(str(mblog.get("created_at") or ""))

        images = _extract_images(mblog)
        video_url = _extract_video_url(mblog)

        if images:
            # Image post — one item per image
            for seq, img_url in enumerate(images):
                results.append({
                    "content_id": content_id,
                    "media_kind": "image",
                    "sequence": seq,
                    "title": title,
                    "author": str((mblog.get("user") or {}).get("screen_name", "")),
                    "file_size": 0,
                    "file_path": "",
                    "download_url": img_url,
                    "create_time": create_time,
                })
        elif video_url:
            # Video post
            results.append({
                "content_id": content_id,
                "media_kind": "video",
                "sequence": 0,
                "title": title,
                "author": str((mblog.get("user") or {}).get("screen_name", "")),
                "file_size": 0,
                "file_path": "",
                "download_url": video_url,
                "create_time": create_time,
            })
        else:
            # Text-only post — no media to download
            pass

    return results


class WeiboProvider(BaseProvider, SyncRateLimiter):
    platform = "weibo"
    account_types: list[str] = ["profile"]  # No live for Weibo

    def __init__(self) -> None:
        self._next_cursor: int = 0
        self._has_more: bool = False
        self._json_api_working: bool = True  # tracked across pages; cleared on failure

    def healthcheck(self) -> dict[str, str]:
        return {"platform": self.platform, "status": "ok"}

    # ── Content fetch ───────────────────────────────────────────

    def fetch_content_items(
        self,
        task_params: dict[str, Any],
        account_url: str,
    ) -> list[dict[str, Any]]:
        uid = _extract_uid(account_url)
        page: int = int(task_params.get("cursor") or 1)
        site_cfg = _load_site_config()
        cookies: dict[str, str] = {
            k: str(v) for k, v in (site_cfg.get("cookies") or {}).items() if v
        }
        tick = str(task_params.get("tick") or "10s")

        # Try m.weibo.cn JSON API first (cleaner data, live photo/video support)
        if self._json_api_working:
            self.rate_limit(tick)
            items, has_more = self._fetch_page_json(uid, page, cookies, task_params)
            if items is None:
                self._json_api_working = False
                logger.info("[weibo] JSON API permanently disabled for uid=%s, falling back to HTML", uid)
                self.rate_limit(tick)
                items, has_more = self._fetch_page(uid, page, cookies)
        else:
            self.rate_limit(tick)
            items, has_more = self._fetch_page(uid, page, cookies)

        self._next_cursor = page + 1 if has_more else 0
        self._has_more = has_more
        return items

    def _fetch_page_json(
        self,
        uid: str,
        page: int,
        cookies: dict[str, str],
        task_params: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]] | None, bool]:
        """Fetch one page via m.weibo.cn JSON API.

        Adds a random delay (from task_params jitter config) between calls
        to avoid rate limiting. Returns (items, has_more) on success,
        or (None, False) to trigger fallback.
        """
        # Rate limit by tick config with optional jitter (page-to-page jitter)
        tick = str(task_params.get("tick") or "10s") if task_params else "10s"
        jitter = task_params.get("jitter") if task_params else None
        self.rate_limit_with_jitter(tick, jitter, label=f"page {page}")

        url = f"https://m.weibo.cn/api/container/getIndex?containerid=230413{uid}&page={page}&count=20"
        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": "https://m.weibo.cn/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            resp = httpx.get(url, headers=headers, cookies=cookies,
                             timeout=15.0, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("[weibo] JSON API error for uid=%s page=%d: %s", uid, page, exc)
            return None, False

        if data.get("ok") != 1:
            # On page 1, fall back to HTML. On later pages, also fall back
            # so pagination can continue via HTML.
            logger.info("[weibo] JSON API ok=%s for uid=%s page=%d, falling back", data.get("ok"), uid, page)
            # Detect auth failures: ok < 0 or msg contains login keywords
            msg = str(data.get("msg") or "")
            if data.get("ok", 1) < 0 or any(kw in msg.lower() for kw in ("login", "auth", "cookie", "token")):
                raise RuntimeError(f"Weibo API auth failure: ok={data.get('ok')}, msg={msg}")
            return None, False

        cards = (data.get("data") or {}).get("cards") or []
        items = self._parse_page_json(cards)
        mblog_count = sum(1 for c in cards if c.get("card_type") == 9)
        has_more = mblog_count > 0
        return items, has_more

    def _parse_page_json(self, cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Parse m.weibo.cn JSON cards into content items.

        Handles:
          - Regular images (type=image or no type)
          - Live photos (type=livephoto) → video with videoSrc
          - Videos (page_info.type=video) → video with media_info URLs
        """
        results: list[dict[str, Any]] = []
        for card in cards:
            if card.get("card_type") != 9:
                continue
            mblog = card.get("mblog") or {}
            content_id = str(mblog.get("id") or "")
            if not content_id:
                continue
            text = str(mblog.get("text") or "")
            title = _RE_STRIP_TAGS.sub("", text)[:200]
            create_time = _parse_weibo_time(str(mblog.get("created_at") or ""))

            # Check pics array for images and live photos
            pics = mblog.get("pics") or []
            image_urls: list[str] = []
            livephoto_urls: list[str] = []
            for pic in pics:
                if not isinstance(pic, dict):
                    continue
                ptype = pic.get("type", "image")
                url = pic.get("large", {}).get("url", "") or pic.get("url", "")
                if ptype == "livephoto":
                    vsrc = pic.get("videoSrc", "")
                    if vsrc:
                        livephoto_urls.append(vsrc)
                elif ptype == "video":
                    vsrc = pic.get("videoSrc", "")
                    if vsrc:
                        livephoto_urls.append(vsrc)
                elif url:
                    image_urls.append(url)

            # Check page_info for video
            video_url = _extract_video_url(mblog)

            # Emit items: live photos first, then images, then page video
            for seq, url in enumerate(livephoto_urls):
                results.append({
                    "content_id": content_id, "media_kind": "video",
                    "sequence": seq, "title": title, "author": "",
                    "file_size": 0, "file_path": "",
                    "download_url": url, "create_time": create_time,
                })

            if image_urls:
                for seq, img_url in enumerate(image_urls):
                    results.append({
                        "content_id": content_id, "media_kind": "image",
                        "sequence": seq, "title": title, "author": "",
                        "file_size": 0, "file_path": "",
                        "download_url": img_url, "create_time": create_time,
                    })
            elif video_url and not livephoto_urls:
                results.append({
                    "content_id": content_id, "media_kind": "video",
                    "sequence": 0, "title": title, "author": "",
                    "file_size": 0, "file_path": "",
                    "download_url": video_url, "create_time": create_time,
                })
        return results

    def _fetch_page(
        self,
        uid: str,
        page: int,
        cookies: dict[str, str],
    ) -> tuple[list[dict[str, Any]], bool]:
        """Fetch one page of Weibo posts via weibo.cn HTML."""
        url = f"https://weibo.cn/u/{uid}?page={page}"
        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": "https://weibo.cn/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        try:
            resp = httpx.get(url, headers=headers, cookies=cookies, timeout=15.0, follow_redirects=True)
            resp.raise_for_status()
            html = resp.text
        except Exception as exc:
            logger.warning("[weibo] fetch page %d failed for uid=%s: %s", page, uid, exc)
            return [], False

        items = self._parse_page(html, uid)

        # Check if more pages: weibo.cn shows "下页" link when more pages exist
        has_more = '>下页<' in html or '>下页</a>' in html

        return items, has_more

    def _parse_page(self, html: str, uid: str) -> list[dict[str, Any]]:
        """Parse weibo.cn HTML page into content items."""
        results: list[dict[str, Any]] = []

        for match in _RE_WEIBO_ITEM.finditer(html):
            block = match.group(0)
            content_id = match.group(1)
            if not content_id:
                continue

            # Extract text
            text_match = _RE_WEIBO_TEXT.search(block)
            text = _RE_STRIP_TAGS.sub("", text_match.group(1) if text_match else "").strip()[:200]

            # Extract time — the ct span often has trailing "&nbsp;来自XXX" 
            time_match = _RE_WEIBO_TIME.search(block)
            time_str = time_match.group(1).strip() if time_match else ""
            # Strip HTML entities and trailing source text
            time_str = time_str.replace("&nbsp;", " ").replace("\xa0", " ").strip()
            # Remove trailing "来自..." text
            time_str = re.sub(r"\s*来自.*$", "", time_str).strip()
            create_time = self._parse_weibo_time_html(time_str)

            # Extract pictures via weibo.cn photo pages (same approach as
            # the reference weiboSpider project).
            pic_urls = self._extract_picture_urls(block, content_id)
            # Extract video
            video_url = self._extract_video_url(block)

            if pic_urls:
                for seq, img_url in enumerate(pic_urls):
                    results.append({
                        "content_id": content_id,
                        "media_kind": "image",
                        "sequence": seq,
                        "title": text,
                        "author": "",
                        "file_size": 0,
                        "file_path": "",
                        "download_url": img_url,
                        "create_time": create_time,
                    })
            elif video_url:
                results.append({
                    "content_id": content_id,
                    "media_kind": "video",
                    "sequence": 0,
                    "title": text,
                    "author": "",
                    "file_size": 0,
                    "file_path": "",
                    "download_url": video_url,
                    "create_time": create_time,
                })

        return results

    def _extract_picture_urls(self, block: str, weibo_id: str) -> list[str]:
        """Extract full-size picture URLs from a weibo block.

        Matches the approach from the reference weiboSpider project:
          - Single picture: extract thumbnail img src, replace size variant → /large/
          - Multiple pictures: use picAll page to get full list
          - Multiple pic links without picAll: return all first images
        """
        first_pic = f"https://weibo.cn/mblog/pic/{weibo_id}"
        all_pic = f"https://weibo.cn/mblog/picAll/{weibo_id}"

        # Check if this weibo has pictures at all
        pic_links = _RE_WEIBO_PIC_LINK.findall(block)
        if not pic_links:
            return []

        # Use startswith because URLs may have query params like ?rl=0
        has_first = any(p.startswith(first_pic) for p in pic_links)
        has_all = any(p.startswith(all_pic) for p in pic_links) if has_first else False

        if has_all:
            # Multiple pictures — fetch picAll page
            return self._fetch_pic_all(weibo_id)

        if has_first:
            # Get all unique pic links (for posts with multiple images but no picAll)
            # Each /pic/{id} link points to a different image
            extra_pics = [p for p in pic_links
                          if p.startswith(first_pic) and not p.startswith(all_pic)]
            
            img_matches = _RE_WEIBO_IMG.findall(block)
            wap180_urls = [src for src in img_matches if _RE_SIZE_VARIANT.search(src)]
            
            if len(extra_pics) > 1 and len(wap180_urls) >= len(extra_pics):
                # Multiple pics, one thumbnail each: return all
                return [_to_large(src) for src in wap180_urls[:len(extra_pics)]]
            
            # Single picture — get thumbnail and upscale
            for src in img_matches:
                if _RE_SIZE_VARIANT.search(src):
                    return [_to_large(src)]
            # Fallback: return first non-icon/avatar image
            for src in img_matches:
                if "icon" not in src and "avatar" not in src:
                    return [src]

        return []

    def _fetch_pic_all(self, weibo_id: str) -> list[str]:
        """Fetch picAll page to get all full-size picture URLs."""
        url = f"https://weibo.cn/mblog/picAll/{weibo_id}"
        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": "https://weibo.cn/",
            "Accept": "text/html",
        }
        site_cfg = _load_site_config()
        cookies: dict[str, str] = {
            k: str(v) for k, v in (site_cfg.get("cookies") or {}).items() if v
            if k in ("_T_WM", "SUB", "SUBP", "SCF")
        }
        try:
            resp = httpx.get(url, headers=headers, cookies=cookies, timeout=10, follow_redirects=True)
            resp.raise_for_status()
            html = resp.text
        except Exception:
            return []

        # Extract all image URLs and convert to full size
        urls = []
        for src in _RE_WEIBO_IMG.findall(html):
            if _RE_SIZE_VARIANT.search(src):
                urls.append(_to_large(src))
        return urls

    def _extract_video_url(self, block: str) -> str:
        """Extract video download URL from a weibo block.

        Handles:
          - m.weibo.cn/s/video/show links (standard video)
          - <video> tags with src attribute (live photos, embedded video)
        """
        # Look for video links
        vid_match = re.search(r'href="(https?://m\.weibo\.cn/s/video/show\?[^"]+)"', block)
        if vid_match:
            video_page_url = vid_match.group(1)
            # Fetch video page to get actual download URL
            return self._resolve_video_url(video_page_url)

        # Look for embedded <video> tags (live photos, etc.)
        video_tag = re.search(r'<video[^>]+src="(https?://[^"]+)"', block)
        if video_tag:
            return video_tag.group(1)
        return ""

    def _resolve_video_url(self, video_page_url: str) -> str:
        """Resolve the actual video download URL from a video page."""
        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": "https://weibo.cn/",
            "Accept": "text/html",
        }
        site_cfg = _load_site_config()
        cookies: dict[str, str] = {
            k: str(v) for k, v in (site_cfg.get("cookies") or {}).items() if v
            if k in ("_T_WM", "SUB", "SUBP", "SCF")
        }
        try:
            resp = httpx.get(video_page_url, headers=headers, cookies=cookies, timeout=10, follow_redirects=True)
            resp.raise_for_status()
            html = resp.text
            # Look for the video URL in the page
            # Format: <video src="https://..." or similar
            vid_src = re.search(r'<video[^>]*src="(https?://[^"]+)"', html)
            if vid_src:
                return vid_src.group(1)
            # Also try: "video_url": "..."
            vid_json = re.search(r'"video_url"\s*:\s*"([^"]+)"', html)
            if vid_json:
                return vid_json.group(1).replace("\\/", "/")
        except Exception:
            pass
        return ""

    @staticmethod
    def _parse_weibo_time_html(time_str: str) -> int:
        """Parse weibo.cn time string to unix timestamp.

        Matches reference project's get_publish_time logic.
        Formats handled:
          "刚刚"              → now
          "N分钟前"           → N minutes ago
          "今天 HH:MM"        → today at HH:MM
          "MM月DD日 HH:MM"   → current year
          "YYYY年MM月DD日 HH:MM"
          "YYYY-MM-DD HH:MM"
        """
        from datetime import datetime, timedelta
        now = datetime.now()
        time_str = time_str.strip()
        try:
            if "刚刚" in time_str:
                return int(now.timestamp())
            if "分钟" in time_str:
                m = re.search(r"(\d+)", time_str)
                mins = int(m.group(1)) if m else 0
                return int((now - timedelta(minutes=mins)).timestamp())
            if "今天" in time_str:
                t = time_str.replace("今天", "").strip()
                dt = datetime(now.year, now.month, now.day,
                              *[int(x) for x in t.split(":")])
                return int(dt.timestamp())
            if "月" in time_str and "年" not in time_str:
                # "05月28日 22:30"
                parts = time_str.replace("月", "-").replace("日", "").split()
                month_day = parts[0].split("-")
                hm = parts[1].split(":") if len(parts) > 1 else ["00", "00"]
                dt = datetime(now.year, int(month_day[0]), int(month_day[1]),
                              int(hm[0]), int(hm[1]))
                return int(dt.timestamp())
            if "年" in time_str:
                # "2024年05月28日 22:30"
                time_str = time_str.replace("年", "-").replace("月", "-").replace("日", "")
            # Try standard formats
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return int(datetime.strptime(time_str[:16], fmt).timestamp())
                except ValueError:
                    continue
        except (ValueError, IndexError):
            pass
        return 0

    # ── Pagination state ────────────────────────────────────────

    @property
    def next_cursor(self) -> int:
        return self._next_cursor

    @property
    def has_more(self) -> bool:
        return self._has_more

    # ── Live (not applicable for Weibo) ─────────────────────────

    def detect_live_status(self, task_params: dict[str, Any], account_url: str) -> bool:
        return False

    def build_live_session_payload(self, task_params: dict[str, Any], account_url: str) -> dict[str, Any]:
        return {}

    # ── Download headers ────────────────────────────────────────

    def build_account_dir(self, account: Any) -> str:
        """Use uid as account directory name."""
        uid = _extract_uid_or_none(str(getattr(account, "account_url", "") or ""))
        if uid:
            return uid
        return super().build_account_dir(account)

    def build_download_request(self, account_url: str) -> dict[str, Any]:
        """Return download headers with Referer set to weibo.cn."""
        return {
            "headers": {
                "User-Agent": _USER_AGENT,
                "Referer": "https://weibo.cn/",
                "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            "cookies": {},
        }

    # ── URL parsing ─────────────────────────────────────────────

    def extract_account_key(self, account_url: str, account_type: str) -> str:
        return _extract_uid(account_url)


def build_provider() -> WeiboProvider:
    return WeiboProvider()
