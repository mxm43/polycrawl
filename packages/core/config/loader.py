from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .creator_keys import DuplicateCreatorKeyError, ensure_no_duplicates, generate_creator_key
from .jsonc import dump_json, load_jsonc
from .models import BaseConfig, ConfigState, CreatorsFile


class ConfigLoader:
    """Loads and validates file-based Spider configuration."""

    def __init__(self, config_dir: Path | str = "config") -> None:
        self.config_dir = Path(config_dir)
        self.base_path = self.config_dir / "base.jsonc"
        self.creators_path = self.config_dir / "creators.jsonc"
        self.sites_dir = self.config_dir / "sites"
        self._lock = threading.Lock()

    def load_all(self) -> ConfigState:
        with self._lock:
            base = self._load_base()
            creators = self._load_creators_with_key_policy()
            sites = self._load_sites()
            return ConfigState(base=base, creators=creators, sites=sites)

    def _load_base(self) -> BaseConfig:
        if not self.base_path.exists():
            raise FileNotFoundError(f"Missing config file: {self.base_path}")
        data = load_jsonc(self.base_path)
        base = BaseConfig.model_validate(data)

        # Environment variable overrides (for Docker deployment).
        # These take precedence over the config file values.
        env_db = os.environ.get("POLYCRAWL_DATABASE_URL")
        if env_db:
            base.storage.database_url = env_db
        env_redis = os.environ.get("POLYCRAWL_REDIS_URL")
        if env_redis:
            base.storage.redis_url = env_redis

        return base

    def _load_creators_with_key_policy(self) -> CreatorsFile:
        if not self.creators_path.exists():
            raise FileNotFoundError(f"Missing config file: {self.creators_path}")

        raw = load_jsonc(self.creators_path)
        creators_file = CreatorsFile.model_validate(raw)

        existing: set[str] = set()
        provided_keys: list[str] = []
        generated = False

        for creator in creators_file.creators:
            if creator.creator_key:
                provided_keys.append(creator.creator_key)

        # Immediate fail when duplicates already exist in file.
        ensure_no_duplicates(provided_keys)

        for creator in creators_file.creators:
            if creator.creator_key:
                existing.add(creator.creator_key)
                continue
            creator.creator_key = generate_creator_key(existing)
            existing.add(creator.creator_key)
            generated = True

        # Defensive check for final key set.
        try:
            ensure_no_duplicates([c.creator_key for c in creators_file.creators if c.creator_key])
        except DuplicateCreatorKeyError:
            raise

        if generated:
            self._write_creators_file(creators_file)

        return creators_file

    def _write_creators_file(self, creators_file: CreatorsFile) -> None:
        payload = creators_file.model_dump(exclude_none=True)
        text = dump_json(payload)

        tmp_path = self.creators_path.with_suffix(".jsonc.tmp")
        tmp_path.write_text(text, encoding="utf-8")
        tmp_path.replace(self.creators_path)

    def _load_sites(self) -> dict[str, dict[str, Any]]:
        sites: dict[str, dict[str, Any]] = {}
        if not self.sites_dir.exists():
            return sites

        for path in sorted(self.sites_dir.glob("*.jsonc")):
            try:
                sites[path.stem] = load_jsonc(path)
            except (ValueError, ValidationError) as exc:
                raise ValueError(f"Invalid site config: {path}: {exc}") from exc
        return sites
