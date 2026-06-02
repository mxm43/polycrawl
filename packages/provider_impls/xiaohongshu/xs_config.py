"""CryptoConfig — all magic constants for Xiaohongshu's mnsv2 signing.

These constants are extracted from the browser's obfuscated vendor JS
(``window.mnsv2`` internals) and the ``x-s-common`` fingerprint pipeline.
They change infrequently but when the API starts returning 401/406 errors
check for updated ``DATA_SDK_VERSION`` / ``DATA_webBuild`` values first.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class CryptoConfig:
    """Frozen collection of all crypto/encoding/fingerprint constants."""

    # ── SDK / platform identity ────────────────────────────────
    DES_KEY: str = "zbp30y86"
    GID_URL: str = "https://as.xiaohongshu.com/api/sec/v1/shield/webprofile"
    DATA_PALTFORM: str = "Windows"
    DATA_SVN: str = "2"
    DATA_SDK_VERSION: str = "4.3.2"
    DATA_webBuild: str = "6.13.7"

    # ── Bitwise operation constants ────────────────────────────
    MAX_32BIT: int = 0xFFFFFFFF
    MAX_SIGNED_32BIT: int = 0x7FFFFFFF
    MAX_BYTE: int = 255

    # ── Base64 alphabets ───────────────────────────────────────
    STANDARD_BASE64_ALPHABET: str = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    )
    CUSTOM_BASE64_ALPHABET: str = (
        "ZmserbBoHQtNP+wOcza/LpngG8yJq42KWYj0DSfdikx3VT16IlUAFM97hECvuRX5"
    )
    X3_BASE64_ALPHABET: str = (
        "MfgqrsbcyzPQRStuvC7mn501HIJBo2DEFTKdeNOwxWXYZap89+/A4UVLhijkl63G"
    )

    # ── 144-byte XOR transform key (hex) ───────────────────────
    HEX_KEY: str = (
        "71a302257793271ddd273bcee3e4b98d9d7935e1da33f5765e2ea8afb6dc77a5"
        "1a499d23b67c20660025860cbf13d4540d92497f58686c574e508f46e1956344"
        "f39139bf4faf22a3eef120b79258145b2feb5193b6478669961298e79bedca64"
        "6e1a693a926154a5a7a1bd1cf0dedb742f917a747a1e388b234f2277516db711"
        "6035439730fa61e9822a0eca7bff72d8"
    )

    # ── Payload layout ─────────────────────────────────────────
    VERSION_BYTES: list[int] = field(default_factory=lambda: [121, 104, 96, 41])
    PAYLOAD_LENGTH: int = 144
    A1_LENGTH: int = 52
    APP_ID_LENGTH: int = 10
    MD5_XOR_LENGTH: int = 8
    A3_PREFIX: list[int] = field(default_factory=lambda: [2, 97, 51, 16])
    TIMESTAMP_LE_LENGTH: int = 8

    # ── Random ranges (no session) ─────────────────────────────
    SEQUENCE_VALUE_MIN: int = 15
    SEQUENCE_VALUE_MAX: int = 50
    WINDOW_PROPS_LENGTH_MIN: int = 1000
    WINDOW_PROPS_LENGTH_MAX: int = 1200

    # ── Session state ranges ───────────────────────────────────
    SESSION_WINDOW_PROPS_INIT_MIN: int = 1000
    SESSION_WINDOW_PROPS_INIT_MAX: int = 2000
    SESSION_SEQUENCE_INIT_MIN: int = 15
    SESSION_SEQUENCE_INIT_MAX: int = 17
    SESSION_SEQUENCE_STEP_MIN: int = 0
    SESSION_SEQUENCE_STEP_MAX: int = 1
    SESSION_WINDOW_PROPS_STEP_MIN: int = 1
    SESSION_WINDOW_PROPS_STEP_MAX: int = 10

    # ── Checksum (16 bytes, used in x-s-common) ────────────────
    CHECKSUM_VERSION: int = 1
    CHECKSUM_XOR_KEY: int = 115
    CHECKSUM_FIXED_TAIL: list[int] = field(
        default_factory=lambda: [
            249, 65, 103, 103, 201, 181, 131, 99, 94, 7, 68, 250, 132, 21,
        ]
    )

    # ── Environment-detection table (part11 of payload) ────────
    ENV_TABLE: list[int] = field(
        default_factory=lambda: [
            115, 248, 83, 102, 103, 201, 181, 131, 99, 94, 4, 68, 250, 132, 21,
        ]
    )
    ENV_CHECKS_DEFAULT: list[int] = field(
        default_factory=lambda: [0, 1, 18, 1, 0, 0, 0, 0, 0, 0, 3, 0, 0, 0, 0]
    )

    # ── custom_hash_v2 initial vector ──────────────────────────
    HASH_IV: tuple[int, int, int, int] = (
        1831565813,
        461845907,
        2246822507,
        3266489909,
    )

    # ── Fingerprint (b1) ───────────────────────────────────────
    ENV_FINGERPRINT_XOR_KEY: int = 41
    ENV_FINGERPRINT_TIME_OFFSET_MIN: int = 10
    ENV_FINGERPRINT_TIME_OFFSET_MAX: int = 50
    B1_SECRET_KEY: str = "xhswebmplfbt"

    # ── Signature templates ────────────────────────────────────
    SIGNATURE_DATA_TEMPLATE: dict[str, str] = field(
        default_factory=lambda: {
            "x0": "4.3.2",
            "x1": "xhs-pc-web",
            "x2": "Windows",
            "x3": "",
            "x4": "",
        }
    )
    SIGNATURE_XSCOMMON_TEMPLATE: dict[str, Any] = field(
        default_factory=lambda: {
            "s0": 5,
            "s1": "",
            "x0": "1",
            "x1": "4.3.2",
            "x2": "Windows",
            "x3": "xhs-pc-web",
            "x4": "4.84.1",
            "x5": "",
            "x6": "",
            "x7": "",
            "x8": "",
            "x9": -596800761,
            "x10": 0,
            "x11": "normal",
        }
    )

    # ── Prefixes ───────────────────────────────────────────────
    X3_PREFIX: str = "mns0301_"
    XYS_PREFIX: str = "XYS_"

    # ── Trace-id helpers ───────────────────────────────────────
    HEX_CHARS: str = "abcdef0123456789"
    XRAY_TRACE_ID_SEQ_MAX: int = 8388607  # 2**23 - 1
    XRAY_TRACE_ID_TIMESTAMP_SHIFT: int = 23
    XRAY_TRACE_ID_PART1_LENGTH: int = 16
    XRAY_TRACE_ID_PART2_LENGTH: int = 16
    B3_TRACE_ID_LENGTH: int = 16

    # ── User-Agent ─────────────────────────────────────────────
    PUBLIC_USERAGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0"
    )

    def with_overrides(self, **kwargs: Any) -> "CryptoConfig":
        """Return a new instance with selected fields replaced."""
        return replace(self, **kwargs)
