from __future__ import annotations

import argparse
import ast
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.core.config.jsonc import load_jsonc
from packages.provider_impls.xiaohongshu.xhs_signer import _USER_AGENT
from packages.provider_impls.xiaohongshu.xs_config import CryptoConfig
from packages.provider_impls.xiaohongshu.xs_encoder import Base64Encoder
from packages.provider_impls.xiaohongshu.xs_signer import SessionManager, XHSignatureSigner


DEFAULT_URL = "https://edith.xiaohongshu.com/api/redcaptcha/v2/getconfig"
DEFAULT_URI = "/api/redcaptcha/v2/getconfig"
DEFAULT_BROWSER_XSC = (
    "2UQAPsHC+aIjqArjwjHjNsQhPsHCH0rjNsQhPaHCH0c1PUhMHjIj2eHjwjQgynEDJ74A"
    "HjIj2ePjwjQhyoPTqBPT49pjHjIj2ecjwjH9N0rAN0PjNsQh+aHCH0rE8/qA8BzYPePMJ"
    "diEG9EU89bF+08Ty04APobhy0mC+B8DG/+U2gQD+/ZIPeZUw/DUwePjNsQhwaHCN/rhwe"
    "cAP/rA+AqVHdWlPsHCPsIj2erlH0ijJfRUJnbVHjIj2erUH0ijP/qhPeZ9+ePU+/GE+/Vl"
    "+AqF+eH9PArF+eLMHdF="
)
DEFAULT_OBSERVED_DSL = "1774426314455"
DEFAULT_OBSERVED_X9 = -1884311377


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Probe Xiaohongshu x-s-common variants against a single endpoint to "
            "validate whether browser-like x12/no-x8 shape changes the response."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "sites" / "xiaohongshu.jsonc",
        help="Path to the site config containing cookies.",
    )
    parser.add_argument(
        "--method",
        default="POST",
        choices=["GET", "POST"],
        help="HTTP method to probe.",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help="Absolute request URL to probe.",
    )
    parser.add_argument(
        "--uri",
        default=DEFAULT_URI,
        help="URI path used for x-s signing.",
    )
    parser.add_argument(
        "--params-json",
        default="{}",
        help="JSON object used as query params for GET or payload for POST.",
    )
    parser.add_argument(
        "--dsl",
        default=DEFAULT_OBSERVED_DSL,
        help="Observed browser window._dsl value used to build x12.",
    )
    parser.add_argument(
        "--dsllt",
        default="",
        help="Observed browser localStorage.dsllt value. Defaults to current epoch ms.",
    )
    parser.add_argument(
        "--browser-xsc",
        default=DEFAULT_BROWSER_XSC,
        help="Exact browser-captured x-s-common to replay.",
    )
    parser.add_argument(
        "--browser-x9",
        type=int,
        default=DEFAULT_OBSERVED_X9,
        help="Observed browser x9 value for synthesized browser-like x-s-common.",
    )
    return parser.parse_args()


def cookie_string(cookies: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies.items() if value)


def decode_xsc(encoder: Base64Encoder, value: str) -> dict[str, Any]:
    return json.loads(encoder.decode(value))


def summarize_xsc(data: dict[str, Any]) -> dict[str, Any]:
    x8 = data.get("x8")
    return {
        "keys": sorted(data.keys()),
        "x1": data.get("x1"),
        "x4": data.get("x4"),
        "x5_prefix": str(data.get("x5", ""))[:12],
        "x8_len": len(x8) if isinstance(x8, str) else None,
        "x9": data.get("x9"),
        "x10": data.get("x10"),
        "x11": data.get("x11"),
        "x12": data.get("x12"),
    }


def build_browser_like_xsc(
    encoder: Base64Encoder,
    cookies: dict[str, str],
    *,
    dsllt: str,
    dsl: str,
    x9: int,
) -> str:
    struct = {
        "s0": 5,
        "s1": "",
        "x0": "1",
        "x1": "4.3.5",
        "x2": "Windows",
        "x3": "xhs-pc-web",
        "x4": "6.13.3",
        "x5": cookies.get("a1", ""),
        "x9": x9,
        "x10": 0,
        "x11": "normal",
        "x12": f"{dsllt};{dsl}",
    }
    return encoder.encode(json.dumps(struct, separators=(",", ":"), ensure_ascii=False))


def build_headers(
    signer: XHSignatureSigner,
    session: SessionManager,
    cookies: dict[str, str],
    *,
    method: str,
    uri: str,
    params_or_payload: dict[str, Any],
    xsc: str,
) -> dict[str, str]:
    if method == "GET":
        headers = signer.sign_headers_get(
            uri,
            cookies,
            params=params_or_payload,
            session=session,
        )
    else:
        headers = signer.sign_headers_post(
            uri,
            cookies,
            payload=params_or_payload,
            session=session,
        )
    headers["x-s-common"] = xsc
    return {
        "accept": "application/json, text/plain, */*",
        "origin": "https://www.xiaohongshu.com",
        "referer": "https://www.xiaohongshu.com/",
        "user-agent": _USER_AGENT,
        "cookie": cookie_string(cookies),
        **({"content-type": "application/json;charset=UTF-8"} if method == "POST" else {}),
        **headers,
    }


def probe(
    client: httpx.Client,
    signer: XHSignatureSigner,
    session: SessionManager,
    encoder: Base64Encoder,
    cookies: dict[str, str],
    *,
    name: str,
    method: str,
    url: str,
    uri: str,
    params_or_payload: dict[str, Any],
    xsc: str,
) -> dict[str, Any]:
    decoded = decode_xsc(encoder, xsc)
    headers = build_headers(
        signer,
        session,
        cookies,
        method=method,
        uri=uri,
        params_or_payload=params_or_payload,
        xsc=xsc,
    )
    if method == "GET":
        query = urlencode(params_or_payload, doseq=True)
        full_url = f"{url}?{query}" if query else url
        response = client.get(full_url, headers=headers)
    else:
        full_url = url
        response = client.post(full_url, headers=headers, json=params_or_payload)
    body_text = response.text
    try:
        body: Any = response.json()
    except Exception:
        body = body_text

    result = {
        "name": name,
        "method": method,
        "request_url": full_url,
        "status_code": response.status_code,
        "verifytype": response.headers.get("verifytype"),
        "verifyuuid": response.headers.get("verifyuuid"),
        "xsc_summary": summarize_xsc(decoded),
        "body": body,
    }
    return result


def main() -> int:
    args = parse_args()
    config_data = load_jsonc(args.config)
    cookies = dict(config_data.get("platform", {}).get("cookies", {}))
    if not cookies.get("a1"):
        raise SystemExit("Config does not contain platform.cookies.a1")
    try:
        params_or_payload = json.loads(args.params_json)
    except json.JSONDecodeError:
        params_or_payload = ast.literal_eval(args.params_json)
    if not isinstance(params_or_payload, dict):
        raise SystemExit("--params-json must decode to a JSON object")

    dsllt = args.dsllt or str(int(time.time() * 1000))
    config = CryptoConfig().with_overrides(PUBLIC_USERAGENT=_USER_AGENT)
    encoder = Base64Encoder(config)
    signer = XHSignatureSigner(config)

    variants = [
        ("current", signer.sign_xs_common(cookies, xs="", xt=int(dsllt))),
        ("browser_replay", args.browser_xsc),
        (
            "browser_like_minimal",
            build_browser_like_xsc(
                encoder,
                cookies,
                dsllt=dsllt,
                dsl=args.dsl,
                x9=args.browser_x9,
            ),
        ),
    ]

    results: list[dict[str, Any]] = []
    with httpx.Client(timeout=20.0, follow_redirects=False) as client:
        for name, xsc in variants:
            session = SessionManager(config)
            results.append(
                probe(
                    client,
                    signer,
                    session,
                    encoder,
                    cookies,
                    name=name,
                    method=args.method,
                    url=args.url,
                    uri=args.uri,
                    params_or_payload=params_or_payload,
                    xsc=xsc,
                )
            )

    print(json.dumps(
        {
            "url": args.url,
            "uri": args.uri,
            "method": args.method,
            "params_or_payload": params_or_payload,
            "dsllt": dsllt,
            "dsl": args.dsl,
            "results": results,
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
