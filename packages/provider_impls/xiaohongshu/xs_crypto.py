"""CryptoProcessor — the core 144-byte payload builder.

This is a pure-Python reimplementation of the browser's ``window.mnsv2()``
internal payload construction.  It does **not** eval any JavaScript; every
constant and algorithm was reverse-engineered from the obfuscated vendor JS.

Key method: ``build_payload_array()`` which builds the 144-byte byte array
that is XOR-transformed → custom-base64-encoded → wrapped into the ``XYS_``
signature.
"""

from __future__ import annotations

import struct
import time
from typing import TYPE_CHECKING

from .xs_config import CryptoConfig
from .xs_encoder import Base64Encoder, BitOperations

if TYPE_CHECKING:
    from .xs_signer import SignState


class CryptoProcessor:
    """Reimplements the browser's payload-array construction logic."""

    def __init__(self, config: CryptoConfig | None = None):
        self.config = config or CryptoConfig()
        self.bit_ops = BitOperations(self.config)
        self.b64encoder = Base64Encoder(self.config)

    # ── helpers ────────────────────────────────────────────────

    @staticmethod
    def _int_to_le_bytes(val: int, length: int = 4) -> list[int]:
        """Convert *val* to a little-endian byte list of *length* bytes."""
        return [(val >> (i * 8)) & 0xFF for i in range(length)]

    @staticmethod
    def _rotate_left(val: int, n: int) -> int:
        """32-bit left rotation."""
        return ((val << n) | (val >> (32 - n))) & 0xFFFFFFFF

    # ── custom_hash_v2 (used for the a3 field of the payload) ──

    def _custom_hash_v2(self, input_bytes: list[int]) -> list[int]:
        """Custom hash used for the a3 field.

        Input: byte list (length must be a multiple of 8).
        Output: 16-byte list.
        """
        s0, s1, s2, s3 = self.config.HASH_IV
        length = len(input_bytes)

        s0 ^= length
        s1 ^= length << 8
        s2 ^= length << 16
        s3 ^= length << 24

        for i in range(length // 8):
            v0, v1 = struct.unpack(
                "<II", bytes(input_bytes[i * 8: (i + 1) * 8])
            )

            s0 = self._rotate_left(((s0 + v0) & 0xFFFFFFFF) ^ s2, 7)
            s1 = self._rotate_left(((v0 ^ s1) + s3) & 0xFFFFFFFF, 11)
            s2 = self._rotate_left(((s2 + v1) & 0xFFFFFFFF) ^ s0, 13)
            s3 = self._rotate_left(((s3 ^ v1) + s1) & 0xFFFFFFFF, 17)

        t0 = s0 ^ length
        t1 = s1 ^ t0
        t2 = (s2 + t1) & 0xFFFFFFFF
        t3 = s3 ^ t2

        rot_t0 = self._rotate_left(t0, 9)
        rot_t1 = self._rotate_left(t1, 13)
        rot_t2 = self._rotate_left(t2, 17)
        rot_t3 = self._rotate_left(t3, 19)

        s0 = (rot_t0 + rot_t2) & 0xFFFFFFFF
        s1 = rot_t1 ^ rot_t3
        s2 = (rot_t2 + s0) & 0xFFFFFFFF
        s3 = rot_t3 ^ s1

        result: list[int] = []
        for s in [s0, s1, s2, s3]:
            result.extend(self._int_to_le_bytes(s, 4))
        return result

    # ── build_payload_array ────────────────────────────────────

    def build_payload_array(
        self,
        hex_parameter: str,       # d_value: MD5(content_string)
        hex_md5_path: str,        # m_value: MD5(uri) or d_value
        a1_value: str,
        app_identifier: str = "xhs-pc-web",
        string_param: str = "",   # content string (for uri_length)
        timestamp: float | None = None,
        sign_state: "SignState | None" = None,
    ) -> list[int]:
        """Build the 144-byte payload array (mns0301 version).

        Layout
        ------
        Offset  Size  Field
        ------  ----  -----
            0     4   VERSION_BYTES
            4     4   seed (random, little-endian)
            8     8   timestamp (ms, little-endian)
           16     8   page_load_timestamp (or timestamp - random_offset)
           24     4   sequence_value (or random)
           28     4   window_props_length (or random)
           32     4   uri_length
           36     8   MD5[0:8] XOR seed_byte
           44     1   a1 length byte
           45    52   a1 value (padded / truncated)
           97     1   app_id length byte
           98    10   app_identifier (padded / truncated)
          108    15   part11 environment checks
          123     ?   A3_PREFIX + custom_hash XOR seed_byte
        """
        timestamp = time.time() if timestamp is None else timestamp
        seed = self.config.MAX_32BIT + 1  # force non-deterministic
        import random as _random
        seed = _random.randint(0, self.config.MAX_32BIT)
        seed_byte = seed & 0xFF

        payload: list[int] = list(self.config.VERSION_BYTES)
        payload.extend(self._int_to_le_bytes(seed, 4))

        ts_ms = int(timestamp * 1000)
        payload.extend(self._int_to_le_bytes(ts_ms, self.config.TIMESTAMP_LE_LENGTH))

        if sign_state is not None:
            payload.extend(
                self._int_to_le_bytes(
                    sign_state.page_load_timestamp, self.config.TIMESTAMP_LE_LENGTH
                )
            )
            payload.extend(self._int_to_le_bytes(sign_state.sequence_value, 4))
            payload.extend(self._int_to_le_bytes(sign_state.window_props_length, 4))
            payload.extend(self._int_to_le_bytes(sign_state.uri_length, 4))
        else:
            time_offset = _random.randint(
                self.config.ENV_FINGERPRINT_TIME_OFFSET_MIN,
                self.config.ENV_FINGERPRINT_TIME_OFFSET_MAX,
            )
            effective_ts_ms = int((timestamp - time_offset) * 1000)
            payload.extend(
                self._int_to_le_bytes(
                    effective_ts_ms, self.config.TIMESTAMP_LE_LENGTH
                )
            )

            seq = _random.randint(
                self.config.SEQUENCE_VALUE_MIN, self.config.SEQUENCE_VALUE_MAX
            )
            payload.extend(self._int_to_le_bytes(seq, 4))

            wpl = _random.randint(
                self.config.WINDOW_PROPS_LENGTH_MIN,
                self.config.WINDOW_PROPS_LENGTH_MAX,
            )
            payload.extend(self._int_to_le_bytes(wpl, 4))

            uri_len = len(string_param.encode("utf-8"))
            payload.extend(self._int_to_le_bytes(uri_len, 4))

        # MD5 XORed with seed_byte (first 8 bytes)
        md5_bytes = bytes.fromhex(hex_parameter)
        payload.extend(
            [md5_bytes[i] ^ seed_byte for i in range(self.config.MD5_XOR_LENGTH)]
        )

        # a1 value
        a1_raw = a1_value.encode("utf-8")[:self.config.A1_LENGTH]
        a1_raw = a1_raw.ljust(self.config.A1_LENGTH, b"\x00")
        payload.append(len(a1_raw))
        payload.extend(a1_raw)

        # app_identifier
        app_raw = app_identifier.encode("utf-8")[:self.config.APP_ID_LENGTH]
        app_raw = app_raw.ljust(self.config.APP_ID_LENGTH, b"\x00")
        payload.append(len(app_raw))
        payload.extend(app_raw)

        # part11 — environment checks
        part11 = [1, seed_byte ^ self.config.ENV_TABLE[0]]
        part11 += [
            self.config.ENV_TABLE[i] ^ self.config.ENV_CHECKS_DEFAULT[i]
            for i in range(1, 15)
        ]
        payload.extend(part11)

        # a3 — custom hash of (ts_bytes + md5_path_bytes)
        ts_bytes = self._int_to_le_bytes(ts_ms, self.config.TIMESTAMP_LE_LENGTH)
        md5_path_bytes = [
            int(hex_md5_path[i:i + 2], 16) for i in range(0, 32, 2)
        ]
        a3_hash = self._custom_hash_v2(ts_bytes + md5_path_bytes)
        payload.extend(
            self.config.A3_PREFIX
            + [b ^ seed_byte for b in a3_hash]
        )

        return payload
