"""Encoding, bit-operations, CRC32, random, and URL utilities.

All pulled from the browser's obfuscated JS and reimplemented in pure Python.
"""

from __future__ import annotations

import base64
import hashlib
import random
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from .xs_config import CryptoConfig

# ═══════════════════════════════════════════════════════════════
# Custom Base64 (3 different alphabets)
# ═══════════════════════════════════════════════════════════════


class Base64Encoder:
    """Custom-base64 encoder/decoder using platform-specific alphabets.

    - ``encode(s)`` / ``decode(s)``:  standard → *custom* alphabet
      (used for ``x-s-common`` and the ``XYS_`` outer wrapper).
    - ``encode_x3(b)`` / ``decode_x3(s)``:  standard → *X3* alphabet
      (used for the inner ``x3`` signature field).
    """

    def __init__(self, config: CryptoConfig):
        self.config = config
        self._ctab = str.maketrans(
            config.STANDARD_BASE64_ALPHABET, config.CUSTOM_BASE64_ALPHABET,
        )
        self._cdtab = str.maketrans(
            config.CUSTOM_BASE64_ALPHABET, config.STANDARD_BASE64_ALPHABET,
        )
        self._x3tab = str.maketrans(
            config.STANDARD_BASE64_ALPHABET, config.X3_BASE64_ALPHABET,
        )
        self._x3dtat = str.maketrans(
            config.X3_BASE64_ALPHABET, config.STANDARD_BASE64_ALPHABET,
        )

    def encode(self, data: bytes | str | Iterable[int]) -> str:
        """Standard Base64 → custom alphabet."""
        if isinstance(data, str):
            data_bytes = data.encode("utf-8")
        elif isinstance(data, (bytes, bytearray)):
            data_bytes = data
        else:
            data_bytes = bytearray(data)
        return base64.b64encode(data_bytes).decode().translate(self._ctab)

    def decode(self, encoded: str) -> str:
        """Custom alphabet → standard Base64 → decoded UTF-8 string."""
        std = encoded.translate(self._cdtab)
        return base64.b64decode(std).decode("utf-8")

    def encode_x3(self, data: bytes | bytearray) -> str:
        """Standard Base64 → X3 alphabet (for x3 field)."""
        return base64.b64encode(data).decode().translate(self._x3tab)

    def decode_x3(self, encoded: str) -> bytes:
        """X3 alphabet → standard Base64 → decoded bytes."""
        std = encoded.translate(self._x3dtat)
        return base64.b64decode(std)


# ═══════════════════════════════════════════════════════════════
# XOR payload transform
# ═══════════════════════════════════════════════════════════════


class BitOperations:
    """XOR transform of the 144-byte payload with HEX_KEY."""

    def __init__(self, config: CryptoConfig):
        self.config = config

    def xor_transform_array(self, source: list[int]) -> bytearray:
        """XOR each byte of *source* with the corresponding HEX_KEY byte."""
        result = bytearray(len(source))
        key_bytes = bytes.fromhex(self.config.HEX_KEY)
        klen = len(key_bytes)
        for i in range(len(source)):
            result[i] = (source[i] ^ key_bytes[i]) & 0xFF if i < klen else source[i] & 0xFF
        return result

    @staticmethod
    def normalize_to_32bit(value: int) -> int:
        return value & 0xFFFFFFFF

    def to_signed_32bit(self, unsigned: int) -> int:
        if unsigned > self.config.MAX_SIGNED_32BIT:
            return unsigned - 0x100000000
        return unsigned


# ═══════════════════════════════════════════════════════════════
# CRC32 (JS-compatible)
# ═══════════════════════════════════════════════════════════════


class CRC32:
    """JS-style CRC32 (matches ``(-1 ^ c ^ 0xEDB88320) >>> 0``)."""

    MASK32 = 0xFFFFFFFF
    POLY = 0xEDB88320
    _TABLE: list[int] | None = None

    @classmethod
    def _ensure_table(cls) -> None:
        if cls._TABLE is not None:
            return
        tbl = [0] * 256
        for d in range(256):
            r = d
            for _ in range(8):
                r = ((r >> 1) ^ cls.POLY) if (r & 1) else (r >> 1)
                r &= cls.MASK32
            tbl[d] = r
        cls._TABLE = tbl

    @classmethod
    def crc32_js_int(
        cls,
        data: str | bytes | Iterable[int],
        string_mode: str = "js",
        signed: bool = True,
    ) -> int:
        """JS-compatible CRC32.

        Args:
            data: Input (str → charCodeAt & 0xFF if string_mode='js').
            string_mode: ``"js"`` (charCodeAt) or ``"utf8"`` (encode first).
            signed: Return signed 32-bit int.
        """
        cls._ensure_table()
        c = cls.MASK32

        if isinstance(data, (bytes, bytearray)):
            it = bytes(data)
        elif isinstance(data, str):
            if string_mode.lower() == "utf8":
                it = data.encode("utf-8")
            else:
                it = (ord(ch) & 0xFF for ch in data)
        else:
            it = ((int(b) & 0xFF) for b in data)

        for b in it:
            c = (cls._TABLE[((c & 0xFF) ^ b) & 0xFF] ^ (c >> 8)) & cls.MASK32

        result = (-1 ^ c ^ cls.POLY) & cls.MASK32
        if signed and (result & 0x80000000):
            result -= 0x100000000
        return result


# ═══════════════════════════════════════════════════════════════
# Random generators
# ═══════════════════════════════════════════════════════════════


class RandomGenerator:
    """Deterministic-looking random values for signature fields."""

    def __init__(self, config: CryptoConfig | None = None):
        self.config = config or CryptoConfig()

    def generate_random_int(self) -> int:
        return random.randint(0, self.config.MAX_32BIT)

    def generate_random_byte_in_range(self, lo: int, hi: int) -> int:
        return random.randint(lo, hi)

    def generate_b3_trace_id(self) -> str:
        return "".join(random.choice(self.config.HEX_CHARS) for _ in range(16))

    def generate_xray_trace_id(
        self, timestamp: int | None = None, seq: int | None = None
    ) -> str:
        ts = timestamp if timestamp is not None else int(time.time() * 1000)
        sq = seq if seq is not None else random.randint(0, 8388607)
        part1 = format(
            ((ts << 23) | sq), f"0{self.config.XRAY_TRACE_ID_PART1_LENGTH}x"
        )
        part2 = "".join(
            random.choice(self.config.HEX_CHARS)
            for _ in range(self.config.XRAY_TRACE_ID_PART2_LENGTH)
        )
        return part1 + part2


# ═══════════════════════════════════════════════════════════════
# URL helpers
# ═══════════════════════════════════════════════════════════════


def extract_uri(url: str) -> str:
    """Extract the URI path from a full URL or partial path."""
    if not url or not isinstance(url, str):
        raise ValueError("URL must be a non-empty string")
    parsed = urlparse(url.strip())
    path = parsed.path
    if not path or path == "/":
        raise ValueError(f"Cannot extract URI from: {url}")
    return path


def build_url(base_url: str, params: dict[str, Any] | None = None) -> str:
    """Build URL with query parameters (XHS-specific encoding rules)."""
    if not params:
        return base_url
    parts = []
    for k, v in params.items():
        if isinstance(v, (list, tuple)):
            vs = ",".join(str(x) for x in v)
        elif v is not None:
            vs = str(v)
        else:
            vs = ""
        # Only '=' is encoded as '%3D'; ',' and other chars are kept as-is
        import urllib.parse
        encoded = urllib.parse.quote(vs, safe=",%")
        parts.append(f"{k}={encoded}")
    return base_url + "?" + "&".join(parts)


# ═══════════════════════════════════════════════════════════════
# MD5 helpers
# ═══════════════════════════════════════════════════════════════


def md5_hex(data: str) -> str:
    """Return the 32-char lowercase MD5 hex digest of *data*."""
    return hashlib.md5(data.encode("utf-8")).hexdigest()
