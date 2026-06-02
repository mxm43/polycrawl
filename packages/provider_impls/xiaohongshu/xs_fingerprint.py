"""Browser fingerprint generation and ``x-s-common`` signature.

The ``x-s-common`` header is a JSON blob (custom-base64-encoded) carrying
a synthetic browser fingerprint (GPU, screen, fonts, …), the ``a1`` cookie,
and a CRC32 checksum.  It is sent alongside the per-request ``x-s`` header.

Unlike ``x-s`` (which is pure computation — see ``xs_crypto.py``),
``x-s-common`` simulates browser-environment values.  Every field
is randomised within realistic ranges so consecutive requests look
like different browser sessions.
"""

from __future__ import annotations

import hashlib
import json
import random
import secrets
import time
import urllib.parse
from typing import Any, Final

from Crypto.Cipher import ARC4

from .xs_config import CryptoConfig
from .xs_encoder import CRC32, Base64Encoder

# ═══════════════════════════════════════════════════════════════
# Fingerprint data constants
# ═══════════════════════════════════════════════════════════════

GPU_VENDORS: Final[list[str]] = [
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) HD Graphics 400 (0x00000166) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) HD Graphics 4400 (0x00001112) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) HD Graphics 4600 (0x00000412) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) HD Graphics 520 (0x1912) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) HD Graphics 530 (0x00001912) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) HD Graphics 550 (0x00001512) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) HD Graphics 6000 (0x1606) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) Iris(TM) Graphics 540 (0x1912) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) Iris(TM) Graphics 550 (0x1913) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) Iris(TM) Plus Graphics 640 (0x161C) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) UHD Graphics 600 (0x3E80) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) UHD Graphics 620 (0x00003EA0) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E9B) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) UHD Graphics 655 (0x00009BC8) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) Iris(R) Xe Graphics (0x000046A8) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) Iris(R) Xe Graphics (0x00009A49) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) Iris(R) Xe MAX Graphics (0x00009BC0) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel Arc A370M (0x0000AF51) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel Arc A380 (0x0000AF41) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel Arc A380M (0x0000AF5E) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel Arc A550 (0x0000AF42) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel Arc A770 (0x0000AF43) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel Arc A770M (0x0000AF50) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Mesa Intel(R) Graphics (RPL‑P GT1) (0x0000A702) OpenGL 4.6)",
    "Google Inc. (Intel)|ANGLE (Intel, Mesa Intel(R) UHD Graphics 770 (0x00004680) OpenGL 4.6)",
    "Google Inc. (Intel)|ANGLE (Intel, Mesa Intel(R) HD Graphics 4400 (0x00001122) OpenGL 4.6)",
    "Google Inc. (Intel)|ANGLE (Intel, Mesa Intel(R) Graphics (ADL‑S GT1) (0x0000A0A1) OpenGL 4.6)",
    "Google Inc. (Intel)|ANGLE (Intel, Mesa Intel(R) UHD Graphics (CML GT2) (0x00009A14) OpenGL 4.6)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) HD Graphics 3000 (0x00001022) Direct3D9Ex vs_3_0 ps_3_0, igdumd64.dll)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) HD Graphics Family (0x00000A16) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) Iris Pro OpenGL Engine, OpenGL 4.1)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) Iris(TM) Plus Graphics 645 (0x1616) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) Iris(TM) Plus Graphics 655 (0x161E) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) UHD Graphics 730 (0x0000A100) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)|ANGLE (Intel, Intel(R) UHD Graphics 805 (0x0000B0A0) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon Vega 3 Graphics (0x000015E0) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon Vega 8 Graphics (0x000015D8) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon Vega 11 Graphics (0x000015DD) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon Graphics (0x00001636) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon RX 5500 XT Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon RX 560 (0x000067EF) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon RX 570 (0x000067DF) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon RX 580 2048SP (0x00006FDF) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon RX 590 (0x000067FF) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon RX 6600 (0x000073FF) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon RX 6600 XT (0x000073FF) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon RX 6650 XT Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon RX 6700 XT (0x000073DF) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon RX 6800 (0x000073BF) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon RX 6900 XT (0x000073C2) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon RX 7700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon Pro 5300M OpenGL Engine, OpenGL 4.1)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon Pro 5500 XT OpenGL Engine, OpenGL 4.1)",
    "Google Inc. (AMD)|ANGLE (AMD, AMD Radeon R7 370 Series (0x00006811) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (AMD)|ANGLE (AMD, ATI Technologies Inc. AMD Radeon RX Vega 64 OpenGL Engine, OpenGL 4.1)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 (0x00001C81) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Ti (0x00001C8C) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 6GB (0x000010DE) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce GTX 1070 (0x00001B81) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce GTX 1080 (0x00001B80) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 (0x00001F06) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 SUPER (0x00001F06) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 2070 (0x00001F10) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 2070 SUPER (0x00001F10) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x0000250F) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Ti (0x00002489) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 (0x00002488) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Ti (0x000028A5) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 (0x00002206) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Ti (0x00002208) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 3090 (0x00002204) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 (0x00002882) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Ti (0x00002803) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 (0x00002786) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Ti (0x00002857) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 4080 (0x00002819) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 4090 (0x00002684) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA Quadro RTX 5000 Ada Generation (0x000026B2) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA Quadro P400 (0x00001CB3) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Google)|ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)",
    "Google Inc. (Google)|ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver)",
    "Google Inc. (Google)|ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device), SwiftShader driver)",
]

FONTS: str = (
    "14px Arial, 14px Arial Black, 14px Arial Narrow, 13px Arial Unicode MS, "
    "14px Calibri, 14px Cambria, 14px Cambria Math, 14px Candara, "
    "15px Comic Sans MS, 14px Consolas, 14px Constantia, 14px Corbel, "
    "14px Courier New, 14px Ebrima, 14px Franklin Gothic Medium, "
    "14px Gabriola, 14px Georgia, 14px HoloLens MDL2 Assets, "
    "14px Impact, 14px Lucida Console, 14px Lucida Sans Unicode, "
    "14px Malgun Gothic, 14px Marlett, 14px Microsoft Himalaya, "
    "14px Microsoft JhengHei, 14px Microsoft New Tai Lue, "
    "14px Microsoft PhagsPa, 14px Microsoft Sans Serif, "
    "14px Microsoft Tai Le, 14px Microsoft YaHei, 14px Microsoft Yi Baiti, "
    "14px MingLiU-ExtB, 14px Mongolian Baiti, 14px MS Gothic, "
    "14px MV Boli, 14px Myanmar Text, 14px Nirmala UI, 14px Palatino Linotype, "
    "14px Segoe MDL2 Assets, 14px Segoe Print, 14px Segoe Script, "
    "14px Segoe UI, 14px Segoe UI Emoji, 14px Segoe UI Historic, "
    "14px Segoe UI Symbol, 14px SimSun-ExtB, 14px Sitka Banner, "
    "14px Sylfaen, 14px Symbol, 14px Tahoma, 14px Times New Roman, "
    "14px Trebuchet MS, 14px Verdana, 14px Webdings, 14px Wingdings, "
    "14px Yu Gothic"
)

BROWSER_PLUGINS: str = (
    "PDF Viewer|Portable Document Format|application/pdf~pdf,text/pdf~pdf|Chrome PDF Viewer"
    "|Chrome PDF Viewer|application/pdf~pdf,text/pdf~pdf|"
    "Chromium PDF Viewer|Chromium PDF Viewer|application/pdf~pdf,text/pdf~pdf|"
    "Microsoft Edge PDF Viewer|Microsoft Edge PDF Viewer|application/pdf~pdf,text/pdf~pdf"
)

CANVAS_HASH: str = "742cc32c"
VOICE_HASH_OPTIONS: str = "10311144241322244122"

# ═══════════════════════════════════════════════════════════════
# Weighted-random helpers
# ═══════════════════════════════════════════════════════════════


def _weighted_choice(options: list, weights: list) -> Any:
    return random.choices(options, weights=weights, k=1)[0]


def _get_renderer_info() -> tuple[str, str]:
    entry = random.choice(GPU_VENDORS)
    vendor = entry.split("|")[0] if "|" in entry else "Google Inc."
    return vendor, entry


def _get_screen_config() -> dict[str, int]:
    resolutions = [
        (1366, 768, 0.25),
        (1600, 900, 0.15),
        (1920, 1080, 0.35),
        (2560, 1440, 0.15),
        (3840, 2160, 0.08),
        (7680, 4320, 0.02),
    ]
    w, h, _ = random.choices(
        resolutions, weights=[r[2] for r in resolutions], k=1
    )[0]
    # simulate taskbar occupation
    taskbar = random.choice([0, 30, 40, 50, 60])
    return {
        "width": w,
        "height": h,
        "availWidth": w,
        "availHeight": h - taskbar,
    }


# ═══════════════════════════════════════════════════════════════
# FingerprintGenerator
# ═══════════════════════════════════════════════════════════════


class FingerprintGenerator:
    """Generates a synthetic browser fingerprint (50+ fields) and the ``b1`` value."""

    def __init__(self, config: CryptoConfig):
        self.config = config
        self._b1_key = config.B1_SECRET_KEY.encode()
        self._encoder = Base64Encoder(config)

    def generate_b1(self, fp: dict) -> str:
        """Generate the ``b1`` field from a fingerprint dict.

        Process: extract 18 fields → JSON → RC4 → URL-encode → custom-base64.
        """
        subset = {k: fp[k] for k in (
            "x33", "x34", "x35", "x36", "x37", "x38", "x39",
            "x42", "x43", "x44", "x45", "x46", "x48", "x49",
            "x50", "x51", "x52", "x82",
        )}
        b1_json = json.dumps(subset, separators=(",", ":"), ensure_ascii=False)
        cipher = ARC4.new(self._b1_key)
        ciphertext = cipher.encrypt(b1_json.encode("utf-8")).decode("latin1")
        encoded_url = urllib.parse.quote(ciphertext, safe="!*'()~_-")
        b = []
        for c in encoded_url.split("%")[1:]:
            chars = list(c)
            b.append(int("".join(chars[:2]), 16))
            b.extend(ord(j) for j in chars[2:])
        return self._encoder.encode(bytearray(b))

    def generate(self, cookies: dict, user_agent: str) -> dict:
        """Generate a complete browser fingerprint dict (50+ fields)."""
        cookie_string = "; ".join(f"{k}={v}" for k, v in cookies.items())
        screen = _get_screen_config()
        is_incognito = _weighted_choice(["true", "false"], [0.95, 0.05])
        vendor, renderer = _get_renderer_info()
        x78_y = random.randint(2350, 2450)

        return {
            "x1": user_agent,
            "x2": "false",
            "x3": "zh-CN",
            "x4": _weighted_choice([16, 24, 30, 32], [0.05, 0.6, 0.05, 0.3]),
            "x5": _weighted_choice([1, 2, 4, 8, 12, 16], [0.10, 0.25, 0.4, 0.2, 0.03, 0.01]),
            "x6": "24",
            "x7": f"{vendor},{renderer}",
            "x8": _weighted_choice([2, 4, 6, 8, 12, 16, 24, 32],
                                   [0.1, 0.4, 0.2, 0.15, 0.08, 0.04, 0.02, 0.01]),
            "x9": f"{screen['width']};{screen['height']}",
            "x10": f"{screen['availWidth']};{screen['availHeight']}",
            "x11": "-480",
            "x12": "Asia/Shanghai",
            "x13": is_incognito,
            "x14": is_incognito,
            "x15": is_incognito,
            "x16": "false",
            "x17": "false",
            "x18": "un",
            "x19": "Win32",
            "x20": "",
            "x21": BROWSER_PLUGINS,
            "x22": hashlib.md5(secrets.token_bytes(32)).hexdigest(),
            "x23": "false",
            "x24": "false",
            "x25": "false",
            "x26": "false",
            "x27": "false",
            "x28": "0,false,false",
            "x29": "4,7,8",
            "x30": "swf object not loaded",
            "x33": "0",
            "x34": "0",
            "x35": "0",
            "x36": f"{random.randint(1, 20)}",
            "x37": "0|0|0|0|0|0|0|0|0|1|0|0|0|0|0|0|0|0|1|0|0|0|0|0",
            "x38": "0|0|1|0|1|0|0|0|0|0|1|0|1|0|1|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0",
            "x39": 0,
            "x40": "0",
            "x41": "0",
            "x42": "3.4.4",
            "x43": CANVAS_HASH,
            "x44": f"{int(time.time() * 1000)}",
            "x45": "__SEC_CAV__1-1-1-1-1|__SEC_WSA__|",
            "x46": "false",
            "x47": "1|0|0|0|0|0",
            "x48": "",
            "x49": "{list:[],type:}",
            "x50": "",
            "x51": "",
            "x52": "",
            "x53": hashlib.md5(secrets.token_bytes(32)).hexdigest(),
            "x54": VOICE_HASH_OPTIONS,
            "x55": "380,380,360,400,380,400,420,380,400,400,360,360,440,420",
            "x56": f"{vendor}|{renderer}|{hashlib.md5(secrets.token_bytes(32)).hexdigest()}|35",
            "x57": cookie_string,
            "x58": "180",
            "x59": "2",
            "x60": "63",
            "x61": "1291",
            "x62": "2047",
            "x63": "0",
            "x64": "0",
            "x65": "0",
            "x66": {
                "referer": "",
                "location": "https://www.xiaohongshu.com/explore",
                "frame": 0,
            },
            "x67": "1|0",
            "x68": "0",
            "x69": "326|1292|30",
            "x70": ["location"],
            "x71": "true",
            "x72": "complete",
            "x73": "1191",
            "x74": "0|0|0",
            "x75": "Google Inc.",
            "x76": "true",
            "x77": "1|1|1|1|1|1|1|1|1|1",
            "x78": {
                "x": 0,
                "y": x78_y,
                "left": 0,
                "right": 290.828125,
                "bottom": x78_y + 18,
                "height": 18,
                "top": x78_y,
                "width": 290.828125,
                "font": FONTS,
            },
            "x80": "1|[object FileSystemDirectoryHandle]",
            "x82": "_0x17a2|_0x1954",
            "x31": "124.04347527516074",
            "x79": "144|599565058866",
        }

    def update(self, fp: dict, cookies: dict, url: str) -> None:
        """Update fingerprint with fresh cookies and URL."""
        fp.update({
            "x39": 0,
            "x44": f"{time.time() * 1000}",
            "x57": "; ".join(f"{k}={v}" for k, v in cookies.items()),
            "x66": {
                "referer": "https://www.xiaohongshu.com/explore",
                "location": url,
                "frame": 0,
            },
        })


# ═══════════════════════════════════════════════════════════════
# XsCommonSigner (x-s-common)
# ═══════════════════════════════════════════════════════════════


class XsCommonSigner:
    """Generates the ``x-s-common`` request header value."""

    def __init__(self, config: CryptoConfig | None = None):
        self.config = config or CryptoConfig()
        self._fp_gen = FingerprintGenerator(self.config)
        self._encoder = Base64Encoder(self.config)

    def sign(self, cookie_dict: dict[str, str]) -> str:
        """Build an ``x-s-common`` value from a cookie dict (must contain ``a1``)."""
        a1_value = cookie_dict["a1"]
        fp = self._fp_gen.generate(
            cookies=cookie_dict, user_agent=self.config.PUBLIC_USERAGENT,
        )
        b1 = self._fp_gen.generate_b1(fp)
        x9 = CRC32.crc32_js_int(b1)

        struct = dict(self.config.SIGNATURE_XSCOMMON_TEMPLATE)
        struct["x5"] = a1_value
        struct["x8"] = b1
        struct["x9"] = x9

        return self._encoder.encode(
            json.dumps(struct, separators=(",", ":"), ensure_ascii=False)
        )
