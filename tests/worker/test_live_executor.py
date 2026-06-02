from __future__ import annotations

import asyncio
import uuid

from services.worker.executors.live_executor import _upsert_live_status


class _DummyStatus:
    def __init__(self, account_id: int, status: str) -> None:
        self.account_id = account_id
        self.status = status
        self.status_since = None
        self.current_recording_session_id = None
        self.recorded_seconds = None
        self.recorded_bytes = None
        self.error_message = None
        self.error_time = None
        self.updated_at = None


class _DummyResult:
    def __init__(self, item):
        self._item = item

    def scalars(self):
        return self

    def first(self):
        return self._item


class _DummySession:
    def __init__(self, existing=None) -> None:
        self.existing = existing
        self.added = None

    async def execute(self, _query):
        return _DummyResult(self.existing)

    def add(self, item):
        self.added = item


def test_upsert_live_status_create_new() -> None:
    session = _DummySession(existing=None)

    async def _run():
        await _upsert_live_status(session, 1, status="probing")

    asyncio.run(_run())

    assert session.added is not None
    assert session.added.account_id == 1
    assert session.added.status == "probing"


def test_upsert_live_status_update_existing() -> None:
    existing = _DummyStatus(account_id=1, status="offline")
    session = _DummySession(existing=existing)

    async def _run():
        await _upsert_live_status(
            session,
            1,
            status="recording",
            current_recording_session_id=uuid.uuid4(),
            recorded_seconds=12,
            recorded_bytes=1024,
        )

    asyncio.run(_run())

    assert session.added is None
    assert existing.status == "recording"
    assert existing.recorded_seconds == 12
    assert existing.recorded_bytes == 1024
