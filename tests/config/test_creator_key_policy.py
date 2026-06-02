from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.core.config.creator_keys import DuplicateCreatorKeyError
from packages.core.config.loader import ConfigLoader


def _write(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def test_generate_creator_key_only_when_missing(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    sites_dir = config_dir / "sites"
    sites_dir.mkdir(parents=True)

    _write(
        config_dir / "base.jsonc",
        {
            "config_version": 1,
            "storage": {
                "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
                "redis_url": "redis://localhost:6379/0",
                "media_base_path": "./downloads",
            },
            "global": {},
        },
    )
    _write(
        config_dir / "creators.jsonc",
        {
            "creators": [
                {
                    "creator_key": "creator_fixedabcd",
                    "display_name": "A",
                    "accounts": [],
                },
                {
                    "display_name": "B",
                    "accounts": [],
                },
            ]
        },
    )

    state = ConfigLoader(config_dir).load_all()

    keys = [c.creator_key for c in state.creators.creators]
    assert keys[0] == "creator_fixedabcd"
    assert keys[1] is not None
    assert keys[1].startswith("creator_")
    assert len(keys[1]) == len("creator_") + 8


def test_duplicate_creator_key_fail_fast(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    sites_dir = config_dir / "sites"
    sites_dir.mkdir(parents=True)

    _write(
        config_dir / "base.jsonc",
        {
            "config_version": 1,
            "storage": {
                "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
                "redis_url": "redis://localhost:6379/0",
                "media_base_path": "./downloads",
            },
            "global": {},
        },
    )
    _write(
        config_dir / "creators.jsonc",
        {
            "creators": [
                {
                    "creator_key": "creator_dup1234",
                    "display_name": "A",
                    "accounts": [],
                },
                {
                    "creator_key": "creator_dup1234",
                    "display_name": "B",
                    "accounts": [],
                },
            ]
        },
    )

    with pytest.raises(DuplicateCreatorKeyError):
        ConfigLoader(config_dir).load_all()
