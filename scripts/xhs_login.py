#!/usr/bin/env python3
"""Xiaohongshu phone number + SMS verification code login tool.

Usage:
    python scripts/xhs_login.py

You'll be prompted for:
  1. Your phone number
  2. The SMS verification code sent to your phone

The resulting cookies will be saved to ``config/sites/xiaohongshu.jsonc``.
"""

from __future__ import annotations

import json
import logging
import random
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# Add project root to sys.path so we can import our signer
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

from packages.provider_impls.xiaohongshu.xs_config import CryptoConfig
from packages.provider_impls.xiaohongshu.xs_signer import XHSignatureSigner, SessionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("xhs_login")

# 鈹€鈹€ Constants 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

EDITH_HOST = "https://edith.xiaohongshu.com"
HOME_URL = "https://www.xiaohongshu.com"
SITE_CONFIG_PATH = _PROJECT_ROOT / "config" / "sites" / "xiaohongshu.jsonc"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0"
)

# 鈹€鈹€ Helpers 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def _generate_a1() -> str:
    """Generate a fresh a1 cookie value (52 hex chars with embedded timestamp)."""
    prefix = "".join(random.choices("0123456789abcdef", k=24))
    ts = str(int(time.time() * 1000))
    suffix = "".join(random.choices("0123456789abcdef", k=15))
    return prefix + ts + suffix


def _cookie_string(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def _save_cookies(cookies: dict[str, str]) -> None:
    """Save cookies to the xiaohongshu site config file."""
    # Read existing config
    if SITE_CONFIG_PATH.exists():
        config = json.loads(SITE_CONFIG_PATH.read_text(encoding="utf-8"))
    else:
        config = {"platform": {"cookies": {}}}

    # Update cookies
    platform = config.setdefault("platform", {})
    platform.setdefault("cookies", {}).update(cookies)
    # Also save a timestamp
    platform["cookies"]["saved_at"] = str(int(time.time()))

    SITE_CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Cookies saved to %s", SITE_CONFIG_PATH)


# 鈹€鈹€ API call wrapper 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def _api_call(
    signer: XHSignatureSigner,
    session_mgr: SessionManager,
    cookies: dict[str, str],
    method: str,
    uri: str,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make a signed API call to edith.xiaohongshu.com and return JSON data."""
    if method == "POST":
        headers = signer.sign_headers_post(
            uri, cookies, payload=payload, timestamp=None, session=session_mgr,
        )
    else:
        headers = signer.sign_headers_get(
            uri, cookies, params=params, timestamp=None, session=session_mgr,
        )

    url = EDITH_HOST + uri
    req_headers = {
        "user-agent": USER_AGENT,
        "cookie": _cookie_string(cookies),
        "origin": HOME_URL,
        "referer": f"{HOME_URL}/",
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        **headers,
    }

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        if method == "POST":
            req_headers["content-type"] = "application/json;charset=UTF-8"
            resp = client.post(url, json=payload, headers=req_headers)
        else:
            resp = client.get(url, params=params, headers=req_headers)

    data = resp.json()
    logger.debug("[%s] %s 鈫?%s", method, uri, resp.status_code)

    if not data.get("success"):
        msg = data.get("msg", "") or data.get("message", "unknown error")
        logger.warning("API warning: %s", msg)

    return data


# 鈹€鈹€ Login steps 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def step_activate(signer: XHSignatureSigner, sm: SessionManager, cookies: dict[str, str]) -> dict[str, Any]:
    """Step 1: Activate guest session."""
    logger.info("Step 1/4: Activating guest session...")
    data = _api_call(signer, sm, cookies, "POST", "/api/sns/web/v1/login/activate", payload={})
    # The server may set cookies in the response
    logger.info("  鈫?activate response keys: %s", list(data.keys()))
    return data


def step_send_code(
    signer: XHSignatureSigner, sm: SessionManager, cookies: dict[str, str], phone: str
) -> dict[str, Any]:
    """Step 2: Send SMS verification code (GET request with query params)."""
    logger.info("Step 2/4: Sending verification code to %s...", phone)
    params = {"phone": phone, "region_code": "86", "zone": "86"}
    data = _api_call(signer, sm, cookies, "GET", "/api/sns/web/v2/login/send_code", params=params)
    if data.get("success"):
        logger.info("  鉁?Code sent! Check your phone for SMS.")
    else:
        logger.warning("  鈿狅笍  %s", data.get("msg", ""))
    return data


def step_check_code(
    signer: XHSignatureSigner, sm: SessionManager, cookies: dict[str, str], phone: str, code: str
) -> dict[str, Any]:
    """Step 3: Verify the SMS code (GET request with query params)."""
    logger.info("Step 3/4: Verifying code...")
    params = {"phone": phone, "region_code": "86", "code": code, "zone": "86"}
    data = _api_call(signer, sm, cookies, "GET", "/api/sns/web/v1/login/check_code", params=params)
    if data.get("success"):
        logger.info("  鉁?Code verified!")
    else:
        logger.warning("  鈿狅笍  %s", data.get("msg", ""))
    return data


def step_login_code(
    signer: XHSignatureSigner, sm: SessionManager, cookies: dict[str, str], phone: str, code: str
) -> dict[str, Any]:
    """Step 4: Complete login and obtain session cookies (POST with JSON body)."""
    logger.info("Step 4/4: Completing login...")
    payload = {"phone": phone, "region_code": "86", "code": code}
    data = _api_call(signer, sm, cookies, "POST", "/api/sns/web/v2/login/code", payload=payload)
    if data.get("success"):
        logger.info("  鉁?Login successful!")
        d = data.get("data", data)
        if isinstance(d, dict):
            session = d.get("session") or d.get("web_session", "")
            secure_session = d.get("secure_session") or d.get("web_session_sec", "")
            user_id = d.get("user_id") or d.get("userId", "")
            if session:
                cookies["web_session"] = str(session)
                logger.info("  web_session obtained")
            if secure_session:
                cookies["web_session_sec"] = str(secure_session)
            if user_id:
                logger.info("  User ID: %s", user_id)
    else:
        logger.warning("  msg: %s", data.get("msg", ""))
        # If it needs mobile_token, try check_code first to get it
        if "mobile_token" in str(data):
            logger.info("  Need mobile_token - trying check_code first...")
            check = step_check_code(signer, sm, cookies, phone, code)
            if check.get("success"):
                token = check.get("data", {}).get("mobile_token") or check.get("mobile_token", "")
                if token:
                    payload["mobile_token"] = token
                    data = _api_call(signer, sm, cookies, "POST", "/api/sns/web/v2/login/code", payload=payload)
    return data


# 鈹€鈹€ Main 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def main() -> int:
    print("=" * 60)
    print("  XiaoHongShu Phone + SMS Login Tool")
    print("=" * 60)

    # 1. Get phone number
    phone = input("\n Phone number: ").strip()
    if not phone:
        logger.error("Phone number required")
        return 1

    # 鈹€鈹€ 2. Generate a1 and set up signer 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    a1 = _generate_a1()
    web_id = "".join(random.choices("0123456789abcdef", k=32))
    cookies: dict[str, str] = {"a1": a1, "webId": web_id}

    config = CryptoConfig().with_overrides(PUBLIC_USERAGENT=USER_AGENT)
    signer = XHSignatureSigner(config)
    session_mgr = SessionManager(config)

    # 鈹€鈹€ 3. Step 1: Activate 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    activate_data = step_activate(signer, session_mgr, cookies)
    # The activate endpoint might return session cookies
    act_data = activate_data.get("data", activate_data)
    if isinstance(act_data, dict):
        for key in ("session", "web_session"):
            if act_data.get(key):
                cookies["web_session"] = str(act_data[key])
                break

    # 鈹€鈹€ 4. Step 2: Send code 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    send_data = step_send_code(signer, session_mgr, cookies, phone)
    if not send_data.get("success"):
        msg = send_data.get("msg", "")
        if "楠岃瘉鐮? in msg:
            logger.info("  (楠岃瘉鐮佸彲鑳藉凡鍙戦€侊紝缁х画绛夊緟杈撳叆...)")
        else:
            logger.error("鍙戦€侀獙璇佺爜澶辫触: %s", msg)
            retry = input("\n馃攧 閲嶈瘯锛?y/n): ").strip().lower()
            if retry == "y":
                send_data = step_send_code(signer, session_mgr, cookies, phone)
                if not send_data.get("success"):
                    logger.error("鍐嶆澶辫触: %s", send_data.get("msg", ""))
                    return 1
            else:
                return 1

    # 鈹€鈹€ 5. Get code from user 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    code = input("\n馃摬 杈撳叆鐭俊楠岃瘉鐮? ").strip()
    if not code:
        logger.error("楠岃瘉鐮佷笉鑳戒负绌?)
        return 1

    # 鈹€鈹€ 6. Step 3: Check code 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    check_data = step_check_code(signer, session_mgr, cookies, phone, code)
    if not check_data.get("success"):
        logger.error("楠岃瘉鐮佹牎楠屽け璐? %s", check_data.get("msg", ""))
        return 1

    # 鈹€鈹€ 7. Step 4: Complete login 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    login_data = step_login_code(signer, session_mgr, cookies, phone, code)
    if not login_data.get("success"):
        logger.error("Login failed: %s", login_data.get("msg", ""))
        retry = input("\nRetry with /api/sns/web/v1/login/code? (y/n): ").strip().lower()
        if retry == "y":
            logger.info("Trying /api/sns/web/v1/login/code...")
            payload = {"phone": phone, "region_code": "86", "code": code}
            login_data = _api_call(signer, session_mgr, cookies, "POST",
                                   "/api/sns/web/v1/login/code", payload=payload)
            if not login_data.get("success"):
                logger.error("Still failed: %s", login_data.get("msg", ""))
                return 1

    # 鈹€鈹€ 8. Save cookies 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    print("\n" + "=" * 60)
    print("  鉁?鐧诲綍鎴愬姛锛佷繚瀛?cookies...")
    print("=" * 60)
    _save_cookies(cookies)

    print(f"\n  a1: {cookies.get('a1', '')[:30]}...")
    print(f"  web_session: {cookies.get('web_session', '(not set)')[:20]}...")
    print(f"  webId: {cookies.get('webId', '')[:20]}...")
    print(f"\n馃搧 閰嶇疆鏂囦欢: {SITE_CONFIG_PATH}")

    # 鈹€鈹€ 9. Verify by fetching self info 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    print("\n馃攳 楠岃瘉鐧诲綍鐘舵€?..")
    try:
        me_data = _api_call(signer, session_mgr, cookies, "GET", "/api/sns/web/v2/user/me")
        if me_data.get("success"):
            user_info = me_data.get("data", me_data)
            if isinstance(user_info, dict):
                nickname = user_info.get("nickname") or user_info.get("user_name", "")
                print(f"  馃懁 鐧诲綍鐢ㄦ埛: {nickname}")
        else:
            print(f"  鈿狅笍  楠岃瘉鎺ュ彛杩斿洖: {me_data.get('msg', '')}")
    except Exception as e:
        print(f"  鈿狅笍  楠岃瘉璇锋眰澶辫触: {e}")

    print("\n馃帀 瀹屾垚锛佺幇鍦ㄥ彲浠ヤ娇鐢ㄦ柊鐨?cookies 浜嗐€?)
    return 0


if __name__ == "__main__":
    sys.exit(main())
