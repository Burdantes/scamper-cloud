"""Content-addressed target sets shared by controller campaign runs."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
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
            "format": "canonical-ipv4-one-per-line",
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }


def register_local_target(source: Path, staging_root: Path) -> RegisteredTarget:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"target source does not exist: {source}")

    staging_root.mkdir(parents=True, exist_ok=True)
    source_sha256 = sha256_file(source)
    temporary_path = staging_root / f".{source.name}.normalized.tmp"
    normalized_digest = hashlib.sha256()
    seen: set[int] = set()
    target_count = 0
    try:
        with source.open("r", encoding="utf-8") as input_file:
            with temporary_path.open("wb") as output_file:
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
                    if address.version != 4:
                        raise ValueError(
                            f"non-IPv4 target on source line {line_number}: {value!r}"
                        )
                    encoded_address = int(address)
                    if encoded_address in seen:
                        raise ValueError(
                            f"duplicate target on source line {line_number}: {address}"
                        )
                    seen.add(encoded_address)
                    encoded_line = f"{address}\n".encode("ascii")
                    output_file.write(encoded_line)
                    normalized_digest.update(encoded_line)
                    target_count += 1

        if target_count == 0:
            raise ValueError(f"target source contained no IPv4 destinations: {source}")

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
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported target registry manifest: {manifest_path}")
    registered = RegisteredTarget(
        target_id=target_id(str(data["target_id"])),
        source_name=str(data["source_name"]),
        source_sha256=str(data["source_sha256"]),
        normalized_sha256=str(data["normalized_sha256"]),
        target_count=int(data["target_count"]),
        directory=path.parent,
    )
    if target_digest(registered.target_id) != registered.source_sha256:
        raise ValueError(f"target registry source digest mismatch: {manifest_path}")
    if registered.target_count <= 0:
        raise ValueError(f"invalid target count in registry manifest: {manifest_path}")
    return registered
