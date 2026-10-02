"""Content-addressed target sets shared by controller campaign runs."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 2
TARGET_ID_PATTERN = re.compile(r"sha256:([0-9a-f]{64})")
REMOTE_REGISTRY_ROOT = Path("/var/lib/scamper-controller/target-registry/sha256")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def target_id(value: str) -> str:
    if TARGET_ID_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "target ID must have the form sha256:<64 lowercase hexadecimal digits>"
        )
    return value


def target_digest(value: str) -> str:
    match = TARGET_ID_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid target ID: {value!r}")
    return match.group(1)


def remote_target_dir(value: str) -> Path:
    return REMOTE_REGISTRY_ROOT / target_digest(value)


def remote_target_path(value: str) -> Path:
    digest = target_digest(value)
    return remote_target_dir(value) / f"{digest}.targets.txt"


@dataclass(frozen=True)
class RegisteredTarget:
    target_id: str
    source_name: str
    source_sha256: str
    normalized_sha256: str
    target_count: int
    address_family: int
    directory: Path

    @property
    def targets_path(self) -> Path:
        staging_path = self.directory / "targets.txt"
        if staging_path.exists():
            return staging_path
        return self.directory / f"{self.source_sha256}.targets.txt"

    @property
    def manifest_path(self) -> Path:
        return self.directory / "manifest.json"

    @property
    def source_version(self) -> str:
        return f"{self.source_name}@sha256:{self.source_sha256}"

    def manifest(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "target_id": self.target_id,
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "source_version": self.source_version,
            "normalized_sha256": self.normalized_sha256,
            "target_count": self.target_count,
            "address_family": self.address_family,
            "format": f"canonical-ipv{self.address_family}-one-per-line",
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }


def register_local_target(
    source: Path,
    staging_root: Path,
    *,
    address_family: int | None = None,
) -> RegisteredTarget:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"target source does not exist: {source}")

    staging_root.mkdir(parents=True, exist_ok=True)
    source_sha256 = sha256_file(source)
    temporary_path = staging_root / f".{source.name}.normalized.tmp"
    normalized_digest = hashlib.sha256()
    if address_family not in {None, 4, 6}:
        raise ValueError("address_family must be 4, 6, or None")
    detected_family = address_family
    target_count = 0
    try:
        with tempfile.TemporaryDirectory(dir=staging_root) as database_dir:
            database_path = Path(database_dir) / "seen.sqlite3"
            with sqlite3.connect(database_path) as database:
                database.execute("PRAGMA journal_mode=OFF")
                database.execute("PRAGMA synchronous=OFF")
                database.execute("CREATE TABLE seen (address TEXT PRIMARY KEY)")
                input_file = source.open("r", encoding="utf-8")
                output_file = temporary_path.open("wb")
                with input_file, output_file:
                    for line_number, raw_line in enumerate(input_file, start=1):
                        value = raw_line.rstrip("\r\n").split("\t", 1)[0].strip()
                        if not value:
                            continue
                        try:
                            address = ipaddress.ip_address(value)
                        except ValueError as error:
                            raise ValueError(
                                f"invalid target on source line {line_number}: {value!r}"
                            ) from error
                        if detected_family is None:
                            detected_family = address.version
                        if address.version != detected_family:
                            raise ValueError(
                                "mixed or unexpected address family on source line "
                                f"{line_number}: expected IPv{detected_family}, got {value!r}"
                            )
                        canonical = str(address)
                        try:
                            database.execute(
                                "INSERT INTO seen(address) VALUES (?)", (canonical,)
                            )
                        except sqlite3.IntegrityError as error:
                            raise ValueError(
                                f"duplicate target on source line {line_number}: {address}"
                            ) from error
                        encoded_line = f"{canonical}\n".encode("ascii")
                        output_file.write(encoded_line)
                        normalized_digest.update(encoded_line)
                        target_count += 1

        if target_count == 0:
            raise ValueError(f"target source contained no IP destinations: {source}")
        assert detected_family in {4, 6}

        normalized_sha256 = normalized_digest.hexdigest()
        directory = staging_root / source_sha256
        directory.mkdir(exist_ok=True)
        targets_path = directory / "targets.txt"
        if targets_path.exists():
            temporary_path.unlink()
        else:
            temporary_path.replace(targets_path)

        registration = RegisteredTarget(
            target_id=f"sha256:{source_sha256}",
            source_name=source.name,
            source_sha256=source_sha256,
            normalized_sha256=normalized_sha256,
            target_count=target_count,
            address_family=detected_family,
            directory=directory,
        )
        registration.manifest_path.write_text(
            json.dumps(registration.manifest(), indent=2) + "\n",
            encoding="utf-8",
        )
        return registration
    finally:
        temporary_path.unlink(missing_ok=True)


def load_registered_target(path: Path) -> RegisteredTarget | None:
    manifest_path = path.parent / "manifest.json"
    if (
        path.name != "targets.txt" and not path.name.endswith(".targets.txt")
    ) or not manifest_path.is_file():
        return None
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema_version = data.get("schema_version")
    if schema_version not in {1, SCHEMA_VERSION}:
        raise ValueError(f"unsupported target registry manifest: {manifest_path}")
    address_family = int(data.get("address_family", 4))
    if address_family not in {4, 6}:
        raise ValueError(f"invalid address family in registry manifest: {manifest_path}")
    registered = RegisteredTarget(
        target_id=target_id(str(data["target_id"])),
        source_name=str(data["source_name"]),
        source_sha256=str(data["source_sha256"]),
        normalized_sha256=str(data["normalized_sha256"]),
        target_count=int(data["target_count"]),
        address_family=address_family,
        directory=path.parent,
    )
    if target_digest(registered.target_id) != registered.source_sha256:
        raise ValueError(f"target registry source digest mismatch: {manifest_path}")
    if registered.target_count <= 0:
        raise ValueError(f"invalid target count in registry manifest: {manifest_path}")
    return registered
