from argparse import Namespace
from pathlib import Path

import pytest

from scamperctl.cli import build_parser, cost_guard_from_args, ssh_access_from_args
from scamperctl.models import SSHAccess


PUBLIC_KEY = "ssh-ed25519 cmVwcmVzZW50YXRpdmUta2V5 collaborator@test"


def test_ssh_access_builds_metadata_line_and_fingerprint() -> None:
    access = SSHAccess(username="collaborator", public_key=PUBLIC_KEY)

    assert access.metadata_line == f"collaborator:{PUBLIC_KEY}"
    assert access.fingerprint.startswith("SHA256:")


@pytest.mark.parametrize("username", ["root", "Uppercase", "bad.name"])
def test_ssh_access_rejects_unsafe_usernames(username: str) -> None:
    with pytest.raises(ValueError, match="SSH username"):
        SSHAccess(username=username, public_key=PUBLIC_KEY)


def test_ssh_access_rejects_private_key() -> None:
    with pytest.raises(ValueError, match="public key"):
        SSHAccess(
            username="collaborator",
            public_key="-----BEGIN OPENSSH PRIVATE KEY-----",
        )


def test_ssh_cli_reads_public_key_file(tmp_path: Path) -> None:
    key_path = tmp_path / "collaborator.pub"
    key_path.write_text(PUBLIC_KEY + "\n", encoding="utf-8")
    args = Namespace(ssh_user="collaborator", ssh_public_key=key_path)

    access = ssh_access_from_args(args)

    assert access is not None
    assert access.public_key == PUBLIC_KEY


def test_ssh_cli_requires_user_and_key_together(tmp_path: Path) -> None:
    args = Namespace(ssh_user="collaborator", ssh_public_key=None)

    with pytest.raises(ValueError, match="must be provided together"):
        ssh_access_from_args(args)


def test_provision_parser_preserves_cost_and_ssh_options(tmp_path: Path) -> None:
    key_path = tmp_path / "collaborator.pub"
    key_path.write_text(PUBLIC_KEY + "\n", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "provision",
            "--profile",
            "lab",
            "--run",
            "shared",
            "--zones",
            "us-central1-a",
            "--estimated-vm-hourly-usd",
            "0.05",
            "--estimated-disk-gb-monthly-usd",
            "0.05",
            "--max-runtime-hours",
            "1",
            "--max-estimated-cost-usd",
            "1",
            "--ssh-user",
            "collaborator",
            "--ssh-public-key",
            str(key_path),
        ]
    )

    guard = cost_guard_from_args(args)
    access = ssh_access_from_args(args)

    assert guard is not None
    assert guard.max_runtime_hours == 1
    assert access is not None
    assert access.username == "collaborator"
