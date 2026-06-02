from __future__ import annotations

import random
import string
from collections.abc import Iterable

PREFIX = "creator_"
_ALPHABET = string.ascii_lowercase + string.digits


class DuplicateCreatorKeyError(ValueError):
    """Raised when duplicated creator_key values are detected."""


def generate_creator_key(existing: set[str], length: int = 8) -> str:
    """Generate a creator key with collision checks against existing keys."""
    for _ in range(50):
        suffix = "".join(random.choices(_ALPHABET, k=length))
        key = f"{PREFIX}{suffix}"
        if key not in existing:
            return key
    raise RuntimeError("Failed to generate unique creator_key after retries")


def ensure_no_duplicates(keys: Iterable[str]) -> None:
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            msg = f"Duplicate creator_key detected: {key}"
            raise DuplicateCreatorKeyError(msg)
        seen.add(key)
