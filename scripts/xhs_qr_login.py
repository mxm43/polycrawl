#!/usr/bin/env python3
"""Xiaohongshu QR code login tool.

Usage:
    python scripts/xhs_qr_login.py

The script will:
  1. Generate a fresh a1 cookie locally
  2. Create a QR code for scanning
  3. Display the QR URL in the terminal
  4. Poll for scan status
  5. Complete login and save cookies to config/sites/xiaohongshu.jsonc

Requirements:
  pip install qrcode  (optional 鈥?for terminal QR rendering)
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

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

from packages.provider_impls.xiaohongshu.xs_config import CryptoConfig
from packages.provider_impls.xiaohongshu.xs_signer import XHSignatureSigner, SessionManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("xhs_qr_login")

EDITH_HOST = "https://edith.xiaohongshu.com"
HOME_URL = "https://www.xiaohongshu.com"
SITE_CONFIG_PATH = _PROJECT_ROOT / "config" / "sites" / "xiaohongshu.jsonc"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0"
)

POLL_INTERVAL = 2.0
POLL_TIMEOUT = 240


def _generate_a1() -> str:
    prefix = "".join(random.choices("0123456789abcdef", k=24))
    ts = str(int(time.time() * 1000))
    suffix = "".join(random.choices("0123456789abcdef", k=15))
    return prefix + ts + suffix


def _cookie_string(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def _save_cookies(cookies: dict[str, str]) -> None:
    if SITE_CONFIG_PATH.exists():
        config = json.loads(SITE_CONFIG_PATH.read_text(encoding="utf-8"))
    else:
        config = {"platform": {"cookies": {}}}
    platform = config.setdefault("platform", {})
    platform.setdefault("cookies", {}).update(cookies)
    platform["cookies"]["saved_at"] = str(int(time.time()))
    SITE_CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Cookies saved to %s", SITE_CONFIG_PATH)


def _display_qr(url: str) -> None:
    """Try to render QR in terminal, fall back to printing URL."""
    try:
        import qrcode
        qr = qrcode.QRCode(border=2)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        return
    except ImportError:
        pass
    print(f"QR URL (open in browser or scan with phone):\n{url}")


def main() -> int:
    print("=" * 60)
    print("  XiaoHongShu QR Code Login")
    print("=" * 60)

    # 1. Generate a1 + activate
    a1 = _generate_a1()
    web_id = "".join(random.choices("0123456789abcdef", k=32))
    cookies: dict[str, str] = {"a1": a1, "webId": web_id}

    config = CryptoConfig().with_overrides(PUBLIC_USERAGENT=USER_AGENT)
    signer = XHSignatureSigner(config)
    session_mgr = SessionManager(config)

    def _api(
        method: str, uri: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        if method == "POST":
            h = signer.sign_headers_post(uri, cookies, payload=payload, session=session_mgr)
        else:
            h = signer.sign_headers_get(uri, cookies, params=params, session=session_mgr)

        headers = {
            "user-agent": USER_AGENT,
            "cookie": _cookie_string(cookies),
            "origin": HOME_URL,
            "referer": f"{HOME_URL}/",
            **(extra_headers or {}),
            **h,
        }
        url = EDITH_HOST + uri
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            if method == "POST":
                headers["content-type"] = "application/json;charset=UTF-8"
                return client.post(url, json=payload, headers=headers)
            return client.get(url, params=params, headers=headers)

    # 鈹€鈹€ Step 1: Activate guest session 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    print("\n[1/5] Activating guest session...")
    r = _api("POST", "/api/sns/web/v1/login/activate", payload={})
    d = r.json()
    if not d.get("success"):
        logger.error("Activate failed: %s", d.get("msg", ""))
        return 1
    data = d.get("data", {})
    if isinstance(data, dict):
        for k in ("session", "secure_session"):
            if data.get(k):
                cookies[k] = str(data[k])
    logger.info("  Session obtained")

    # 鈹€鈹€ Step 2: Create QR code 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    print("\n[2/5] Creating QR code...")
    r = _api("POST", "/api/sns/web/v1/login/qrcode/create", payload={"qr_type": 1})
    d = r.json()
    if not d.get("success"):
        logger.error("QR create failed: %s", d.get("msg", ""))
        return 1
    qr_data = d.get("data", d)
    if isinstance(qr_data, dict):
        qr_id = qr_data.get("qr_id", "")
        code = qr_data.get("code", "")
        qr_url = qr_data.get("url", "")
    else:
        logger.error("Unexpected QR create response: %s", d)
        return 1

    if not qr_url:
        logger.error("No QR URL in response")
        return 1
    logger.info("  QR code created (qr_id=%s)", qr_id[:12])

    # 鈹€鈹€ Step 3: Display QR code 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    print("\n[3/5] Scan the QR code with the Xiaohongshu App:\n")
    _display_qr(qr_url)

    # 鈹€鈹€ Step 4: Poll for scan status 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    print(f"\n[4/5] Waiting for scan (timeout: {POLL_TIMEOUT}s)...")
    start = time.time()
    last_status = -1
    confirmed = False

    while (time.time() - start) < POLL_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        try:
            r = _api("POST", "/api/qrcode/userinfo",
                     payload={"qrId": qr_id, "code": code},
                     extra_headers={"service-tag": "webcn"})
            d = r.json()
        except Exception as e:
            logger.debug("Poll error: %s", e)
            continue

        # The response may be wrapped or direct
        pd = d.get("data", d) if isinstance(d, dict) else {}
        code_status = int(pd.get("codeStatus", -1))

        if code_status != last_status:
            last_status = code_status
            if code_status == 0:
                logger.info("  QR displayed, waiting for scan...")
            elif code_status == 1:
                logger.info("  Scanned! Waiting for confirmation...")
            elif code_status == 2:
                logger.info("  Confirmed!")
                confirmed = True
                break

    if not confirmed:
        logger.error("Timed out waiting for QR scan")
        return 1

    # 鈹€鈹€ Step 5: Complete login 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    print("\n[5/5] Completing login...")
    r = _api("GET", "/api/sns/web/v1/login/qrcode/status",
             params={"qr_id": qr_id, "code": code})
    d = r.json()

    if d.get("success"):
        logger.info("  Login successful!")
        # Extract session cookies from the response data and headers
        ld = d.get("data", d)
        if isinstance(ld, dict):
            session = ld.get("session") or ld.get("web_session", "")
            secure_session = ld.get("secure_session") or ld.get("web_session_sec", "")
            user_id = ld.get("user_id") or ld.get("userId", "")
            if session:
                cookies["web_session"] = str(session)
            if secure_session:
                cookies["web_session_sec"] = str(secure_session)
            if user_id:
                logger.info("  User ID: %s", user_id)

        # Also try to extract set-cookie from response headers
        for name, value in r.cookies.items():
            if value and name not in cookies:
                cookies[name] = value
    else:
        logger.error("QR login completion failed: %s", d.get("msg", ""))
        return 1

    # 鈹€鈹€ Save cookies 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    print("\n" + "=" * 60)
    logger.info("Saving cookies...")
    _save_cookies(cookies)
    print(f"\n  a1: {cookies.get('a1', '')[:30]}...")
    print(f"  web_session: {cookies.get('web_session', '(not set)')[:20]}...")
    print(f"  webId: {cookies.get('webId', '')[:20]}...")
    print(f"\nConfig: {SITE_CONFIG_PATH}")

    # 鈹€鈹€ Verify 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    print("\nVerifying login...")
    try:
        r = _api("GET", "/api/sns/web/v2/user/me")
        me = r.json()
        if me.get("success"):
            info = me.get("data", {})
            logger.info("  Nickname: %s", info.get("nickname", "N/A"))
        else:
            logger.warning("  Verify failed: %s", me.get("msg", ""))
    except Exception as e:
        logger.warning("  Verify error: %s", e)

    print("\nDone! Cookies saved and ready to use.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
