"""Date/time parsing and formatting utilities."""

from __future__ import annotations

from datetime import UTC, datetime


def now_utc_naive() -> datetime:
    """Return the current UTC time as a naive datetime."""
    return datetime.now(UTC).replace(tzinfo=None)


def parse_publish_date(raw: object) -> datetime | None:
    """Parse a publish-date value from API responses into naive UTC datetime.

    Supports:
    - Unix timestamp (int / float)
    - ISO-8601 string with or without timezone
    - ``None`` / empty string
    """
    if raw is None:
        return None

    if isinstance(raw, (int, float)):
        if raw <= 0:
            return None
        return datetime.fromtimestamp(float(raw), tz=UTC).replace(tzinfo=None)

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        with_timezone = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(with_timezone)
            return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            return None

    return None
