"""
Douyin X-Bogus request signing.

Pure-Python port of the algorithm from the douyin-downloader reference project.
No I/O, no side effects 鈥?only deterministic computation.

Usage:
    from packages.provider_impls.douyin.signing import sign_query

    query = sign_query("sec_user_id=ABC&count=18&max_cursor=0&device_platform=webapp&aid=6383")
    # returns "sec_user_id=ABC&...&X-Bogus=<value>"
"""
from __future__ import annotations

import base64
import hashlib
import time

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/109.0.0.0 Safari/537.36"
)

_B64_CHARS = "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe="


# ---------------------------------------------------------------------------
# RC4-like stream cipher (鍐呴儴浣跨敤)
# ---------------------------------------------------------------------------

def _rc4_like(key: list[str], data: str) -> bytearray:
    """RC4-style KSA + PRGA over a string."""
    d = list(range(256))
    c = 0
    for i in range(256):
        c = (c + d[i] + ord(key[i % len(key)])) % 256
        d[i], d[c] = d[c], d[i]

    t = 0
    c = 0
    result = bytearray(len(data))
    for i in range(len(data)):
        t = (t + 1) % 256
        c = (c + d[t]) % 256
        d[t], d[c] = d[c], d[t]
        result[i] = ord(data[i]) ^ d[(d[t] + d[c]) % 256]
    return result


# ---------------------------------------------------------------------------
# Core signing steps
# ---------------------------------------------------------------------------

def _double_md5(s: str | bytes) -> bytes:
    """MD5(MD5(s)) 鈥?used for payload/form salt."""
    if isinstance(s, str):
        s = s.encode()
    return hashlib.md5(hashlib.md5(s).digest()).digest()


def _ua_salt(ua: str) -> bytes:
    """Compute UA salt bytes from the User-Agent string."""
    ua_key = ["\x00", "\x01", "\x0e"]
    encoded = base64.b64encode(_rc4_like(ua_key, ua))
    return hashlib.md5(encoded).digest()


def _build_arr1(payload: str, ua: str, form: str = "") -> list[int]:
    sp = list(_double_md5(payload))
    sf = list(_double_md5(form))
    su = list(_ua_salt(ua))

    ts = int(time.time())
    canvas = 1489154074

    arr1 = [
        64,                         # 0 鈥?fixed
        0,                          # 1 鈥?fixed
        1,                          # 2 鈥?fixed
        14,                         # 3 鈥?fixed
        sp[14], sp[15],             # 4-5  payload bytes
        sf[14], sf[15],             # 6-7  form bytes
        su[14], su[15],             # 8-9  ua bytes
        (ts >> 24) & 0xFF,          # 10
        (ts >> 16) & 0xFF,          # 11
        (ts >>  8) & 0xFF,          # 12
        (ts >>  0) & 0xFF,          # 13
        (canvas >> 24) & 0xFF,      # 14
        (canvas >> 16) & 0xFF,      # 15
        (canvas >>  8) & 0xFF,      # 16
        (canvas >>  0) & 0xFF,      # 17
        64,                         # 18 鈥?checksum (XOR accumulator)
    ]

    for i in range(1, 18):          # XOR positions 1鈥?7 into [18]
        arr1[18] ^= arr1[i]

    return arr1


def _arr1_to_arr2(arr1: list[int]) -> list[int]:
    """Interleave even/odd indices."""
    return [
        arr1[0], arr1[2], arr1[4], arr1[6], arr1[8],
        arr1[10], arr1[12], arr1[14], arr1[16], arr1[18],
        arr1[1], arr1[3], arr1[5], arr1[7], arr1[9],
        arr1[11], arr1[13], arr1[15], arr1[17],
    ]


def _arr2_to_garbled(arr2: list[int]) -> list[int]:
    """Produce the 23-byte garbled sequence used for base64-like encoding."""
    p = [
        arr2[0],  arr2[10], arr2[1],  arr2[11], arr2[2],  arr2[12],
        arr2[3],  arr2[13], arr2[4],  arr2[14], arr2[5],  arr2[15],
        arr2[6],  arr2[16], arr2[7],  arr2[17], arr2[8],  arr2[18],
        arr2[9],
    ]
    char_array = [chr(b) for b in p]
    cipher_key = ["\x02", "\xff"]
    cipher_key_str = [chr(0xFF)]
    encrypted = _rc4_like(cipher_key_str, "".join(char_array))
    return [2, 0xFF, *encrypted]


def _garbled_to_xbogus(garbled: list[int]) -> str:
    """Encode garbled bytes with the custom 65-char alphabet."""
    result = []
    for i in range(0, 21, 3):
        n = garbled[i + 2] | garbled[i + 1] << 8 | garbled[i] << 16
        result.append(_B64_CHARS[(n & 0xFC0000) >> 18])
        result.append(_B64_CHARS[(n & 0x03F000) >> 12])
        result.append(_B64_CHARS[(n & 0x000FC0) >> 6])
        result.append(_B64_CHARS[(n & 0x00003F)])
    return "".join(result)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_xbogus(payload: str, ua: str = _UA, form: str = "") -> str:
    """
    Compute the X-Bogus value for a given query-string payload.

    Args:
        payload: URL-encoded query string (without leading '?')
        ua:      User-Agent header value (must match the one sent in the request)
        form:    POST body string; empty for GET requests

    Returns:
        The X-Bogus value (28-char string).
    """
    arr1 = _build_arr1(payload, ua, form)
    arr2 = _arr1_to_arr2(arr1)
    garbled = _arr2_to_garbled(arr2)
    return _garbled_to_xbogus(garbled)


def sign_query(payload: str, ua: str = _UA) -> str:
    """
    Append '&X-Bogus=<value>' to the payload query string.

    Args:
        payload: URL-encoded query string (without leading '?')
        ua:      User-Agent header value

    Returns:
        payload + "&X-Bogus=<value>"
    """
    return payload + "&X-Bogus=" + compute_xbogus(payload, ua)


DEFAULT_UA = _UA
