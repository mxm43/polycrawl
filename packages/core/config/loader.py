from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .creator_keys import DuplicateCreatorKeyError, ensure_no_duplicates, generate_creator_key
from .jsonc import dump_json, load_jsonc
from .models import BaseConfig, ConfigState, CreatorConfig, CreatorsFile


# ── Site config files we care about ──────────────────────────
SITE_CONFIG_FILES: list[str] = [
    "douyin.jsonc",
    "twitter.jsonc",
    "weibo.jsonc",
    "xiaohongshu.jsonc",
]


class ConfigLoader:
    """Loads and validates file-based Spider configuration.

    Creator config is read from ``config/creators/`` — every ``*.jsonc``
    file in that directory is loaded and its ``creators`` arrays are merged.
    """

    def __init__(self, config_dir: Path | str = "config") -> None:
        self.config_dir = Path(config_dir)
        self.base_path = self.config_dir / "base.jsonc"
        self.creators_dir = self.config_dir / "creators"
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
        return BaseConfig.model_validate(data)

    def _load_creators_with_key_policy(self) -> CreatorsFile:
        """Load all ``*.jsonc`` files from the creators directory and merge them.

        Falls back to the legacy ``creators.jsonc`` file if the directory
        does not exist.
        """
        # ── Backward compat: single creators.jsonc ─────────────────
        legacy_path = self.config_dir / "creators.jsonc"
        if not self.creators_dir.exists():
            if legacy_path.exists():
                return self._load_single_creators_file(legacy_path)
            raise FileNotFoundError(
                f"Neither directory ({self.creators_dir}) nor legacy file "
                f"({legacy_path}) found"
            )

        files: list[Path] = sorted(self.creators_dir.glob("*.jsonc"))
        if not files:
            return CreatorsFile(creators=[])

        # ── Load & merge all creators ───────────────────────────────
        all_creators: list[CreatorConfig] = []
        provided_keys: list[str] = []

        for fpath in files:
            raw = load_jsonc(fpath)
            cf = CreatorsFile.model_validate(raw)
            for c in cf.creators:
                all_creators.append(c)
                if c.creator_key:
                    provided_keys.append(c.creator_key)

        ensure_no_duplicates(provided_keys)

        # ── Generate missing keys ───────────────────────────────────
        existing: set[str] = set(provided_keys)
        generated_map: dict[str, str] = {}  # display_name → generated key

        for c in all_creators:
            if c.creator_key:
                continue
            new_key = generate_creator_key(existing)
            c.creator_key = new_key
            existing.add(new_key)
            generated_map[c.display_name] = new_key

        try:
            ensure_no_duplicates([c.creator_key for c in all_creators if c.creator_key])
        except DuplicateCreatorKeyError:
            raise

        # ── Write back files that had keys generated ────────────────
        if generated_map:
            for fpath in files:
                raw = load_jsonc(fpath)
                cf = CreatorsFile.model_validate(raw)
                changed = False
                for c in cf.creators:
                    if not c.creator_key and c.display_name in generated_map:
                        c.creator_key = generated_map[c.display_name]
                        changed = True
                if changed:
                    payload = cf.model_dump(exclude_none=True)
                    text = dump_json(payload)
                    tmp_path = fpath.with_suffix(".jsonc.tmp")
                    tmp_path.write_text(text, encoding="utf-8")
                    tmp_path.replace(fpath)

        return CreatorsFile(creators=all_creators)

    def _load_single_creators_file(self, path: Path) -> CreatorsFile:
        """Legacy: load creators from a single JSONC file (backward compat)."""
        raw = load_jsonc(path)
        creators_file = CreatorsFile.model_validate(raw)

        existing: set[str] = set()
        provided_keys: list[str] = []
        generated = False

        for creator in creators_file.creators:
            if creator.creator_key:
                provided_keys.append(creator.creator_key)

        ensure_no_duplicates(provided_keys)

        for creator in creators_file.creators:
            if creator.creator_key:
                existing.add(creator.creator_key)
                continue
            creator.creator_key = generate_creator_key(existing)
            existing.add(creator.creator_key)
            generated = True

        try:
            ensure_no_duplicates([c.creator_key for c in creators_file.creators if c.creator_key])
        except DuplicateCreatorKeyError:
            raise

        if generated:
            payload = creators_file.model_dump(exclude_none=True)
            text = dump_json(payload)
            tmp_path = path.with_suffix(".jsonc.tmp")
            tmp_path.write_text(text, encoding="utf-8")
            tmp_path.replace(path)

        return creators_file

    def _load_sites(self) -> dict[str, dict[str, Any]]:
        sites: dict[str, dict[str, Any]] = {}
        if not self.sites_dir.exists():
            return sites

        for filename in SITE_CONFIG_FILES:
            path = self.sites_dir / filename
            if not path.exists():
                continue
            try:
                sites[path.stem] = load_jsonc(path)
            except (ValueError, ValidationError) as exc:
                raise ValueError(f"Invalid site config: {path}: {exc}") from exc
        return sites
