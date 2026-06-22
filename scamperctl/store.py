from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from scamperctl.models import GCPProfile, RunInventory


def default_home() -> Path:
    return Path(os.environ.get("SCAMPERCTL_HOME", ".scamper"))


class Store:
    def __init__(self, home: Path | None = None) -> None:
        self.home = home or default_home()
        self.config_path = self.home / "config.json"
        self.runs_dir = self.home / "runs"

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as err:
            raise FileNotFoundError(f"configuration file not found: {path}") from err
        except json.JSONDecodeError as err:
            raise ValueError(f"invalid JSON in {path}: {err}") from err

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        temporary_path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        temporary_path.replace(path)

    def save_profile(self, profile: GCPProfile) -> None:
        if self.config_path.exists():
            config = self._read_json(self.config_path)
        else:
            config = {"version": 1, "profiles": {}}
        profiles = config.setdefault("profiles", {})
        profiles[profile.name] = profile.to_dict()
        self._write_json(self.config_path, config)

    def get_profile(self, name: str) -> GCPProfile:
        config = self._read_json(self.config_path)
        try:
            profile_value = config["profiles"][name]
        except KeyError as err:
            raise KeyError(
                f"profile {name!r} is not configured; run 'scamperctl configure' first"
            ) from err
        return GCPProfile.from_dict(name, profile_value)

    def list_profiles(self) -> tuple[GCPProfile, ...]:
        if not self.config_path.exists():
            return ()
        config = self._read_json(self.config_path)
        return tuple(
            GCPProfile.from_dict(name, value)
            for name, value in sorted(config.get("profiles", {}).items())
        )

    def run_path(self, run_id: str) -> Path:
        return self.runs_dir / run_id / "inventory.json"

    def run_directory(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def save_inventory(self, inventory: RunInventory) -> None:
        self._write_json(self.run_path(inventory.run_id), inventory.to_dict())

    def get_inventory(self, run_id: str) -> RunInventory:
        path = self.run_path(run_id)
        try:
            return RunInventory.from_dict(self._read_json(path))
        except FileNotFoundError as err:
            raise FileNotFoundError(
                f"run {run_id!r} was not found under {self.runs_dir}"
            ) from err

    def list_inventories(self) -> tuple[RunInventory, ...]:
        if not self.runs_dir.exists():
            return ()
        paths = sorted(self.runs_dir.glob("*/inventory.json"))
        return tuple(RunInventory.from_dict(self._read_json(path)) for path in paths)
