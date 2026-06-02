"""x-rap-param generator 鈥?wraps Spider_XHS's ``xhs_rap.js`` JSVMP via execjs.

Usage
-----
    >>> from packages.provider_impls.xiaohongshu.xhs_rap import generate_rap_param
    >>> rap = generate_rap_param("/api/sns/web/v1/feed", {...})
    >>> headers = {"x-rap-param": rap, "xy-direction": "13"}
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import execjs

logger = logging.getLogger(__name__)

_JS_DIR = os.path.abspath(os.path.dirname(__file__))
_JS_FILE = os.path.join(_JS_DIR, "xhs_rap.js")

# Compiled JS runtime 鈥?cached after first call
_RUNTIME: execjs.ExternalRuntime | None = None
_RAP_FN: Any = None


def _ensure_runtime() -> Any:
    """Lazy-load and compile ``xhs_rap.js``, cache the result."""
    global _RUNTIME, _RAP_FN
    if _RAP_FN is not None:
        return _RAP_FN

    try:
        with open(_JS_FILE, "r", encoding="utf-8") as f:
            source = f.read()
        ctx = execjs.compile(source)
        _RAP_FN = ctx
        logger.debug("[xhs-rap] JS runtime compiled (%d bytes)", len(source))
    except Exception as exc:
        logger.error("[xhs-rap] Failed to compile xhs_rap.js: %s", exc)
        raise
    return _RAP_FN


def generate_rap_param(
    api: str,
    data: dict[str, Any] | str = "",
    app_id: str | None = None,
) -> str:
    """Generate ``x-rap-param`` header value for a Xiaohongshu API call.

    Parameters
    ----------
    api : str
        API path, e.g. ``"/api/sns/web/v1/feed"``.
        Can also be a full URL 鈥?the function auto-detects.
    data : dict or str
        Request payload (JSON body).  Will be JSON-serialised if a dict.
    app_id : str or None
        App identifier (``"xhs-pc-web"``, ``"creator-platform"``).
        Auto-detected when ``None``.

    Returns
    -------
    str
        The ``x-rap-param`` value (opaque base64-like string).
    """
    ctx = _ensure_runtime()
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False) if isinstance(data, dict) else (data or "")
    try:
        rap = ctx.call("generate_x_rap_param", api, body, app_id or "")
        return str(rap)
    except Exception as exc:
        logger.warning("[xhs-rap] generate_x_rap_param failed: %s", exc)
        return ""


def is_rap_needed(api: str) -> bool:
    """Check whether an API endpoint typically requires ``x-rap-param``.

    Based on Spider_XHS's ``anti_hp_sign_config.signIncludesUrl`` list.
    """
    rap_apis = [
        "api/sns/web/v1/homefeed",
        "api/sns/web/v1/search/notes",
        "api/sns/web/v1/user_posted",
        "api/sns/web/v1/feed",
        "api/sns/web/v1/comment/post",
        "web_api/sns/v5/creator/topic/template/list",
        "web_api/sns/v2/note",
    ]
    for pattern in rap_apis:
        if pattern in api:
            return True
    return False
