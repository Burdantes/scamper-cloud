from pathlib import Path
from typing import Sequence

import pytest

from scamperctl.gcloud import GCloudClient
from scamperctl.models import CostGuard, GCPProfile, Instance, RunInventory, SSHAccess
from scamperctl.runner import CommandResult
from scamperctl.store import Store
from scamperctl.workflow import (
    ProvisionOptions,
    build_deployment_plan,
    build_provision_plan,
    deployment_commands,
    instance_name,
    one_zone_per_region,
    prepare_ssh_metadata,
    provision,
    resolved_registry_auth,
)


class FakeRunner:
    def __init__(self, results: list[CommandResult] | None = None) -> None:
        self.results = list(results or [])

    def run(self, args: Sequence[str], *, check: bool = True) -> CommandResult:
        return self.results.pop(0) if self.results else CommandResult()


def client() -> GCloudClient:
    return GCloudClient(
        GCPProfile(name="lab", project="example-project"),
        FakeRunner(),
    )


def test_provision_plan_enforces_vm_limit(tmp_path: Path) -> None:
    options = ProvisionOptions(
        run_id="baseline",
        zones=("us-central1-a", "us-east1-b"),
        machine_type="e2-small",
        count_per_zone=2,
        max_vms=3,
    )

    with pytest.raises(ValueError, match="exceeding --max-vms"):
        build_provision_plan(client(), options, tmp_path / "startup.sh")


def test_one_zone_per_region_is_deterministic() -> None:
    assert one_zone_per_region(
        ("us-central1-c", "europe-west1-b", "us-central1-a", "europe-west1-c")
    ) == ("europe-west1-b", "us-central1-a")


def test_one_per_region_apply_requires_cost_guard(tmp_path: Path) -> None:
    options = ProvisionOptions(
        run_id="global",
        zones=("one-per-region",),
        machine_type="e2-small",
        max_vms=50,
    )

    with pytest.raises(ValueError, match="requires an explicit cost guard"):
        provision(client(), Store(tmp_path / ".scamper"), options)


def test_one_per_region_filters_for_machine_type_availability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gcloud_client = client()
    monkeypatch.setattr(
        gcloud_client,
        "list_zones",
        lambda: ["europe-west1-b", "europe-west1-c", "us-central1-a"],
    )
    monkeypatch.setattr(
        gcloud_client,
        "list_machine_type_zones",
        lambda machine_type: ["europe-west1-c", "us-central1-a"],
    )
    options = ProvisionOptions(
        run_id="global",
        zones=("one-per-region",),
        machine_type="e2-small",
        max_vms=2,
        cost_guard=CostGuard(
            estimated_vm_hourly_usd=0.05,
            estimated_disk_gb_monthly_usd=0.05,
            max_runtime_hours=1,
            max_estimated_cost_usd=1,
        ),
    )

    plan = build_provision_plan(gcloud_client, options, tmp_path / "startup.sh")

    assert plan["location_selection"] == "one-per-region"
    assert plan["region_count"] == 2
    assert [item["zone"] for item in plan["instances"]] == [
        "europe-west1-c",
        "us-central1-a",
    ]


def test_provision_plan_enforces_estimated_cost_bound(tmp_path: Path) -> None:
    options = ProvisionOptions(
        run_id="global",
        zones=("us-central1-a", "us-east1-b"),
        machine_type="e2-small",
        max_vms=2,
        cost_guard=CostGuard(
            estimated_vm_hourly_usd=1,
            estimated_disk_gb_monthly_usd=1,
            max_runtime_hours=2,
            max_estimated_cost_usd=3,
        ),
    )

    with pytest.raises(ValueError, match="estimated maximum cost"):
        build_provision_plan(client(), options, tmp_path / "startup.sh")


def test_cost_guard_adds_server_side_auto_delete(tmp_path: Path) -> None:
    options = ProvisionOptions(
        run_id="bounded",
        zones=("us-central1-a",),
        machine_type="e2-small",
        max_vms=1,
        cost_guard=CostGuard(
            estimated_vm_hourly_usd=0.05,
            estimated_disk_gb_monthly_usd=0.05,
            max_runtime_hours=1.5,
            max_estimated_cost_usd=1,
        ),
    )

    plan = build_provision_plan(client(), options, tmp_path / "startup.sh")
    command = plan["instances"][0]["command"]

    assert "--max-run-duration=5400s" in command
    assert "--instance-termination-action=DELETE" in command


def test_provision_plan_attaches_collaborator_public_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gcloud_client = client()
    monkeypatch.setattr(gcloud_client, "project_os_login_enabled", lambda: False)
    access = SSHAccess(
        username="collaborator",
        public_key="ssh-ed25519 cmVwcmVzZW50YXRpdmUta2V5 collaborator@test",
    )
    options = ProvisionOptions(
        run_id="shared",
        zones=("us-central1-a",),
        machine_type="e2-small",
        max_vms=1,
        ssh_access=access,
    )

    plan = build_provision_plan(gcloud_client, options, tmp_path / "startup.sh")
    command = plan["instances"][0]["command"]
    metadata_path = tmp_path / "ssh-keys"

    assert metadata_path.read_text(encoding="utf-8") == access.metadata_line + "\n"
    assert any(f"ssh-keys={metadata_path}" in arg for arg in command)
    assert plan["ssh_access"]["username"] == "collaborator"
    assert access.public_key not in str(plan)


def test_metadata_ssh_access_rejects_os_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gcloud_client = client()
    monkeypatch.setattr(gcloud_client, "project_os_login_enabled", lambda: True)
    access = SSHAccess(
        username="collaborator",
        public_key="ssh-ed25519 cmVwcmVzZW50YXRpdmUta2V5 collaborator@test",
    )

    with pytest.raises(ValueError, match="OS Login is enabled"):
        prepare_ssh_metadata(gcloud_client, access, tmp_path)


def test_long_instance_names_remain_valid() -> None:
    name = instance_name("a" * 55, "us-central1-a", 1)

    assert len(name) <= 63
    assert name[0].isalpha()


def test_deployment_uses_capabilities_without_privileged_mode(tmp_path: Path) -> None:
    targets = tmp_path / "targets.txt"
    targets.write_text("8.8.8.8\n", encoding="utf-8")
    instance = Instance(
        name="scamper-baseline-us-central1-a-1",
        zone="us-central1-a",
        machine_type="e2-small",
        external_ip="192.0.2.2",
    )

    _, ssh_command = deployment_commands(
        client(),
        instance,
        run_id="baseline",
        experiment="icmp",
        image="ghcr.io/example/scamper:latest",
        registry_auth="none",
        targets=targets,
        scamper_args='-c "trace -P ICMP" -p 1000',
    )
    remote_command = next(arg for arg in ssh_command if arg.startswith("--command="))

    assert "--cap-add=NET_RAW" in remote_command
    assert "--cap-add=NET_ADMIN" in remote_command
    assert "--env=PROBE_IP=192.0.2.2" in remote_command
    assert "--privileged" not in remote_command


def test_deployment_plan_requires_existing_targets(tmp_path: Path) -> None:
    inventory = RunInventory(
        run_id="baseline",
        profile="lab",
        project="example-project",
        machine_type="e2-small",
        instances=(
            Instance(
                name="scamper-baseline-us-central1-a-1",
                zone="us-central1-a",
                machine_type="e2-small",
            ),
        ),
    )

    with pytest.raises(FileNotFoundError):
        build_deployment_plan(
            client(),
            inventory,
            experiment="icmp",
            image="ghcr.io/example/scamper:latest",
            registry_auth="none",
            targets=tmp_path / "missing.txt",
            scamper_args="-p 1000",
        )


def test_artifact_registry_pull_uses_ephemeral_vm_identity(tmp_path: Path) -> None:
    targets = tmp_path / "targets.txt"
    targets.write_text("192.0.2.1\n", encoding="utf-8")
    instance = Instance(
        name="scamper-baseline-us-central1-a-1",
        zone="us-central1-a",
        machine_type="e2-small",
        external_ip="192.0.2.2",
    )
    image = (
        "us-central1-docker.pkg.dev/example-project/experiments/"
        "scamper@sha256:0123456789abcdef"
    )

    _, ssh_command = deployment_commands(
        client(),
        instance,
        run_id="baseline",
        experiment="icmp",
        image=image,
        registry_auth="auto",
        targets=targets,
        scamper_args='-c "trace -P ICMP" -p 1000',
    )
    remote_command = next(arg for arg in ssh_command if arg.startswith("--command="))

    assert "metadata.google.internal" in remote_command
    assert "Metadata-Flavor: Google" in remote_command
    assert "--password-stdin" in remote_command
    assert "mktemp -d" in remote_command
    assert 'rm -rf "$docker_config"' in remote_command
    assert image in remote_command


def test_artifact_registry_auth_rejects_other_registry_hosts() -> None:
    with pytest.raises(ValueError, match="LOCATION-docker.pkg.dev"):
        resolved_registry_auth("ghcr.io/example/scamper:v1", "artifact-registry")


def test_registry_auth_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unsupported registry authentication"):
        resolved_registry_auth("registry.example.com/scamper:v1", "unknown")  # type: ignore[arg-type]
