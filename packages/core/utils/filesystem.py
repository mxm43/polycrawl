"""Filesystem-safe path and filename utilities."""

from __future__ import annotations

import re
from pathlib import Path

_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def sanitize_filename(value: str, max_len: int = 50) -> str:
    """Replace filesystem-unsafe characters and truncate."""
    text = _UNSAFE.sub("_", value).strip(" _")
    return text[:max_len] if text else "unnamed"


def build_creator_dir(display_name: str | None, creator_key: str | None) -> str:
    """Build a filesystem-safe directory name for a creator.

    Returns ``{sanitized_name}_{sanitized_key}``.
    """
    name = sanitize_filename(str(display_name or "unknown"), 40)
    key = sanitize_filename(str(creator_key or "unknown"), 40)
    return f"{name}_{key}"
