"""Xiaohongshu API request signer.

This is the main entry-point for signing Xiaohongshu API requests.
It provides:

- ``XHSignatureSigner`` — generates ``x-s``, ``x-s-common``, ``x-t``,
  ``x-b3-traceid``, ``x-xray-traceid`` headers for GET/POST requests.
- ``SessionManager`` / ``SignState`` — stateful session simulation for
  more authentic-looking consecutive signatures.
"""

from __future__ import annotations

import hashlib
import json
import time
import random
from typing import Any, Literal, NamedTuple

from .xs_config import CryptoConfig
from .xs_crypto import CryptoProcessor
from .xs_encoder import Base64Encoder, CRC32, RandomGenerator, extract_uri, md5_hex
from .xs_fingerprint import FingerprintGenerator


# ═══════════════════════════════════════════════════════════════
# Session state
# ═══════════════════════════════════════════════════════════════


class SignState(NamedTuple):
    """Immutable snapshot of session state for a single signing operation."""

    page_load_timestamp: int  # ms
    sequence_value: int
    window_props_length: int
    uri_length: int


class SessionManager:
    """Simulates a browser session so consecutive signatures look realistic.

    The counters (sequence, window_props_length) evolve gradually between
    requests, mimicking real user behaviour.
    """

    def __init__(self, config: CryptoConfig | None = None):
        self._config = config or CryptoConfig()
        self.page_load_timestamp: int = int(time.time() * 1000)
        self.sequence_value: int = random.randint(
            self._config.SESSION_SEQUENCE_INIT_MIN,
            self._config.SESSION_SEQUENCE_INIT_MAX,
        )
        self.window_props_length: int = random.randint(
            self._config.SESSION_WINDOW_PROPS_INIT_MIN,
            self._config.SESSION_WINDOW_PROPS_INIT_MAX,
        )

    def _update_state(self) -> None:
        self.sequence_value += random.randint(
            self._config.SESSION_SEQUENCE_STEP_MIN,
            self._config.SESSION_SEQUENCE_STEP_MAX,
        )
        self.window_props_length += random.randint(
            self._config.SESSION_WINDOW_PROPS_STEP_MIN,
            self._config.SESSION_WINDOW_PROPS_STEP_MAX,
        )

    def get_current_state(self, content_string: str) -> SignState:
        self._update_state()
        return SignState(
            page_load_timestamp=self.page_load_timestamp,
            sequence_value=self.sequence_value,
            window_props_length=self.window_props_length,
            uri_length=len(content_string),
        )


# ═══════════════════════════════════════════════════════════════
# Main signer
# ═══════════════════════════════════════════════════════════════


class XHSignatureSigner:
    """Generate signed headers for Xiaohongshu API requests.

    Usage
    -----
    >>> signer = XHSignatureSigner()
    >>> headers = signer.sign_headers_get(
    ...     "/api/sns/web/v1/user_posted",
    ...     {"a1": "..."},
    ...     params={"num": 30, "user_id": "..."},
    ... )
    """

    def __init__(self, config: CryptoConfig | None = None):
        self.config = config or CryptoConfig()
        self.crypto = CryptoProcessor(self.config)
        self.encoder = Base64Encoder(self.config)
        self.random_gen = RandomGenerator(self.config)
        # Persistent fingerprint generator — caches fingerprint after first call
        # so consecutive requests look like the same browser session.
        self._fp_gen = FingerprintGenerator(self.config)
        self._b1_cache: str | None = None

    # ── content string ─────────────────────────────────────────

    def _build_content_string(
        self, method: str, uri: str, payload: dict[str, Any] | None = None
    ) -> str:
        """Build the canonical content string used for MD5 hashing.

        **Critical:** This must match the browser's ``seccore_signv2`` behaviour.

        Both GET and POST use ``uri + JSON.stringify(payload)`` format.
        """
        payload = payload or {}
        if not payload:
            return uri
        # Both GET and POST use JSON.stringify format — matches browser seccore_signv2
        return uri + json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    # ── single signature (x-s) ─────────────────────────────────

    def sign_xs(
        self,
        method: Literal["GET", "POST"],
        uri: str,
        a1_value: str,
        xsec_appid: str = "xhs-pc-web",
        payload: dict[str, Any] | None = None,
        timestamp: float | None = None,
        session: SessionManager | None = None,
    ) -> str:
        """Generate the ``x-s`` request header value (``XYS_`` prefixed).

        Steps mirror the browser's ``seccore_signv2``:

        1. Extract URI path.
        2. Build content string (uri + params / uri + JSON body).
        3. Compute ``d_value = MD5(content_string)``.
        4. Compute ``m_value = d_value`` for GET, ``MD5(uri)`` for POST.
        5. Build 144-byte payload array via ``CryptoProcessor``.
        6. XOR-transform with ``HEX_KEY``.
        7. Custom-base64 (X3 alphabet) → wrap in JSON → custom-base64 → ``XYS_``.
        """
        uri = extract_uri(uri)
        content_string = self._build_content_string(method, uri, payload)

        # d_value = MD5(content_string) — same for GET/POST
        d_value = md5_hex(content_string)

        # m_value: both GET and POST use MD5(uri) — matches browser seccore_signv2
        m_value = md5_hex(uri)

        sign_state = session.get_current_state(content_string) if session else None

        payload_array = self.crypto.build_payload_array(
            hex_parameter=d_value,
            hex_md5_path=m_value,
            a1_value=a1_value,
            app_identifier=xsec_appid,
            string_param=content_string,
            timestamp=timestamp,
            sign_state=sign_state,
        )

        xor_result = self.crypto.bit_ops.xor_transform_array(payload_array)
        x3 = self.encoder.encode_x3(xor_result[: self.config.PAYLOAD_LENGTH])

        wrapper = dict(self.config.SIGNATURE_DATA_TEMPLATE)
        wrapper["x3"] = self.config.X3_PREFIX + x3

        return self.config.XYS_PREFIX + self.encoder.encode(
            json.dumps(wrapper, separators=(",", ":"), ensure_ascii=False)
        )

    # ── x-s-common ─────────────────────────────────────────────

    def sign_xs_common(self, cookies: dict[str, str], xs: str, xt: int) -> str:
        """Generate the ``x-s-common`` header value.

        Matches Spider_XHS implementation (xhs_main_260411.js ``XsCommon``):

        - ``x6``: current timestamp (``xt``, int — serialised as number in JSON)
        - ``x7``: the ``x-s`` value for this request
        - ``x8``: fixed ``b1`` blob (generated once, cached forever)
        - ``x9``: ``CRC32(MD5(str(xt) + xs + x8))``
        """
        if self._b1_cache is None:
            # First call: generate a fresh fingerprint and b1, then cache b1 forever
            fp = self._fp_gen.generate(cookies=cookies, user_agent=self.config.PUBLIC_USERAGENT)
            self._b1_cache = self._fp_gen.generate_b1(fp)

        # x9 = CRC32(MD5(str(xt) + xs + x8)) — xt is int, same as JS Date.now()
        md5_input = str(xt) + xs + self._b1_cache
        md5_hash = hashlib.md5(md5_input.encode()).hexdigest()
        md5_bytes = bytes.fromhex(md5_hash)
        x9 = CRC32.crc32_js_int(md5_bytes)

        struct = dict(self.config.SIGNATURE_XSCOMMON_TEMPLATE)
        struct["x5"] = cookies.get("a1", "")
        struct["x6"] = xt  # int — JSON will serialise as number (no quotes)
        struct["x7"] = xs
        struct["x8"] = self._b1_cache
        struct["x9"] = x9

        return self.encoder.encode(
            json.dumps(struct, separators=(",", ":"), ensure_ascii=False)
        )

    # ── convenience wrappers ───────────────────────────────────

    def sign_xs_get(
        self,
        uri: str,
        a1_value: str,
        xsec_appid: str = "xhs-pc-web",
        params: dict[str, Any] | None = None,
        timestamp: float | None = None,
        session: SessionManager | None = None,
    ) -> str:
        """Shortcut: ``sign_xs("GET", …)``."""
        return self.sign_xs("GET", uri, a1_value, xsec_appid, params, timestamp, session)

    def sign_xs_post(
        self,
        uri: str,
        a1_value: str,
        xsec_appid: str = "xhs-pc-web",
        payload: dict[str, Any] | None = None,
        timestamp: float | None = None,
        session: SessionManager | None = None,
    ) -> str:
        """Shortcut: ``sign_xs("POST", …)``."""
        return self.sign_xs("POST", uri, a1_value, xsec_appid, payload, timestamp, session)

    # ── header-bundle helpers ──────────────────────────────────

    @staticmethod
    def get_x_t(timestamp: float | None = None) -> str:
        """Millisecond-epoch timestamp string."""
        return str(int((timestamp if timestamp is not None else time.time()) * 1000))

    def get_b3_trace_id(self) -> str:
        return self.random_gen.generate_b3_trace_id()

    def get_xray_trace_id(
        self, timestamp: int | None = None, seq: int | None = None
    ) -> str:
        return self.random_gen.generate_xray_trace_id(timestamp, seq)

    # ── one-shot header generation ─────────────────────────────

    def sign_headers(
        self,
        method: Literal["GET", "POST"],
        uri: str,
        cookies: dict[str, str],
        xsec_appid: str = "xhs-pc-web",
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        timestamp: float | None = None,
        session: SessionManager | None = None,
    ) -> dict[str, str]:
        """Generate all required headers for a Xiaohongshu API call.

        Returns
        -------
        dict with keys: ``x-s``, ``x-s-common``, ``x-t``,
        ``x-b3-traceid``, ``x-xray-traceid``.
        """
        a1 = cookies.get("a1", "")
        actual_payload = payload if method == "POST" else params
        ts = timestamp if timestamp is not None else time.time()
        ts_ms = int(ts * 1000)

        xs = self.sign_xs(method, uri, a1, xsec_appid, actual_payload, ts, session)
        xsc = self.sign_xs_common(cookies, xs, ts_ms)  # ts_ms is int, matches JS Date.now()

        return {
            "x-s": xs,
            "x-s-common": xsc,
            "x-t": str(ts_ms),
            "x-b3-traceid": self.random_gen.generate_b3_trace_id(),
            "x-xray-traceid": self.random_gen.generate_xray_trace_id(ts_ms),
        }

    def sign_headers_get(
        self,
        uri: str,
        cookies: dict[str, str],
        xsec_appid: str = "xhs-pc-web",
        params: dict[str, Any] | None = None,
        timestamp: float | None = None,
        session: SessionManager | None = None,
    ) -> dict[str, str]:
        """Shortcut: ``sign_headers("GET", …)``."""
        return self.sign_headers(
            "GET", uri, cookies, xsec_appid, params=params,
            timestamp=timestamp, session=session,
        )

    def sign_headers_post(
        self,
        uri: str,
        cookies: dict[str, str],
        xsec_appid: str = "xhs-pc-web",
        payload: dict[str, Any] | None = None,
        timestamp: float | None = None,
        session: SessionManager | None = None,
    ) -> dict[str, str]:
        """Shortcut: ``sign_headers("POST", …)``."""
        return self.sign_headers(
            "POST", uri, cookies, xsec_appid, payload=payload,
            timestamp=timestamp, session=session,
        )
