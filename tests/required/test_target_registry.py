from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from controller.target_registry import (
    load_registered_target,
    register_local_target,
    remote_target_path,
    target_id,
)


def test_register_target_normalizes_tsv_and_records_provenance(tmp_path: Path) -> None:
    source = tmp_path / "responsive.tsv"
    source.write_text(
        "192.0.2.1\tresponsive\n198.51.100.2\tresponsive\n",
        encoding="utf-8",
    )

    registered = register_local_target(source, tmp_path / "registry")

    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    normalized = b"192.0.2.1\n198.51.100.2\n"
    normalized_digest = hashlib.sha256(normalized).hexdigest()
    assert registered.target_id == f"sha256:{source_digest}"
    assert registered.targets_path.read_bytes() == normalized
    assert registered.normalized_sha256 == normalized_digest
    assert registered.target_count == 2
    manifest = json.loads(registered.manifest_path.read_text(encoding="utf-8"))
    assert manifest["source_version"] == f"responsive.tsv@sha256:{source_digest}"
    assert manifest["format"] == "canonical-ipv4-one-per-line"
    assert load_registered_target(registered.targets_path) == registered


def test_registry_preserves_distinct_source_versions_with_same_targets(
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain.txt"
    plain.write_text("192.0.2.1\n", encoding="utf-8")
    annotated = tmp_path / "annotated.tsv"
    annotated.write_text("192.0.2.1\tresponsive\n", encoding="utf-8")

    first = register_local_target(plain, tmp_path / "registry")
    second = register_local_target(annotated, tmp_path / "registry")

    assert first.normalized_sha256 == second.normalized_sha256
    assert first.target_id != second.target_id
    assert first.directory != second.directory


def test_register_target_rejects_duplicate_or_invalid_destinations(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.txt"
    duplicate.write_text("192.0.2.1\n192.0.2.1\n", encoding="utf-8")
    invalid = tmp_path / "invalid.txt"
    invalid.write_text("192.0.2.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate target"):
        register_local_target(duplicate, tmp_path / "duplicate-registry")
    with pytest.raises(ValueError, match="invalid target"):
        register_local_target(invalid, tmp_path / "invalid-registry")


def test_register_ipv6_target_records_family_and_rejects_mixed_input(
    tmp_path: Path,
) -> None:
    source = tmp_path / "responsive-v6.txt"
    source.write_text("2606:4700:4700::1111\n2001:4860:4860::8888\n", encoding="utf-8")

    registered = register_local_target(source, tmp_path / "registry", address_family=6)

    assert registered.address_family == 6
    manifest = json.loads(registered.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["format"] == "canonical-ipv6-one-per-line"
    assert manifest["address_family"] == 6

    mixed = tmp_path / "mixed.txt"
    mixed.write_text("2606:4700:4700::1111\n192.0.2.1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mixed or unexpected address family"):
        register_local_target(mixed, tmp_path / "mixed-registry")


def test_target_id_validation_and_remote_path() -> None:
    digest = "a" * 64
    registered_id = target_id(f"sha256:{digest}")

    assert str(remote_target_path(registered_id)).endswith(
        f"/target-registry/sha256/{digest}/{digest}.targets.txt"
    )
    with pytest.raises(Exception, match="target ID"):
        target_id("latest")
