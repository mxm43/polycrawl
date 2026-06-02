from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_SEC_UID_PATTERN = re.compile(r"MS4wLjAB[0-9A-Za-z_-]+")
_LEGACY_SAFE_CHARS = re.compile(r"[0-9A-Za-z\u4e00-\u9fa5]+")


def extract_sec_uid(value: str) -> str | None:
    match = _SEC_UID_PATTERN.search(value)
    if match is None:
        return None
    return match.group(0)


def sanitize_legacy_desc(desc: str) -> str:
    compact = "".join(_LEGACY_SAFE_CHARS.findall(desc or "")).strip()
    return compact[:20]


def build_legacy_file_prefix(create_time: int, desc: str) -> str:
    # Legacy downloader used localtime for filename prefix generation.
    dt = datetime.fromtimestamp(create_time)
    ts = dt.strftime("%Y-%m-%d %H.%M.%S")
    return f"{ts}_{sanitize_legacy_desc(desc)}"


def infer_media_kind(raw: dict[str, Any]) -> str:
    images = raw.get("images")
    if isinstance(images, list) and images:
        return "image"

    image_list = raw.get("image_list")
    if isinstance(image_list, list) and image_list:
        return "image"

    return "video"
