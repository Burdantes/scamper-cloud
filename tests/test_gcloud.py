from pathlib import Path
from typing import Sequence

import pytest

from scamperctl.gcloud import GCloudClient
from scamperctl.models import GCPProfile, Instance
from scamperctl.runner import CommandFailed, CommandResult


class FakeRunner:
    def __init__(self, results: list[CommandResult] | None = None) -> None:
        self.results = list(results or [])
        self.commands: list[list[str]] = []

    def run(self, args: Sequence[str], *, check: bool = True) -> CommandResult:
        self.commands.append(list(args))
        return self.results.pop(0) if self.results else CommandResult()


def profile() -> GCPProfile:
    return GCPProfile(
        name="lab",
        project="example-project",
        configuration="research",
    )


def test_every_gcloud_command_pins_configuration_and_project() -> None:
    client = GCloudClient(profile(), FakeRunner())

    command = client.create_instance_args(
        name="scamper-baseline-us-central1-a-1",
        zone="us-central1-a",
        machine_type="e2-standard-2",
        disk_size_gb=20,
        image_family="ubuntu-2204-lts",
        image_project="ubuntu-os-cloud",
        network="default",
        run_id="baseline",
        startup_script=Path("startup.sh"),
        service_account="measurement-vm@example-project.iam.gserviceaccount.com",
        max_run_duration_seconds=3600,
    )

    assert "--configuration=research" in command
    assert "--project=example-project" in command
    assert "--machine-type=e2-standard-2" in command
    assert (
        "--service-account=measurement-vm@example-project.iam.gserviceaccount.com"
        in command
    )
    assert "--scopes=https://www.googleapis.com/auth/devstorage.read_only" in command
    assert "--max-run-duration=3600s" in command
    assert "--instance-termination-action=DELETE" in command


def test_machine_type_zone_listing_parses_zone_urls() -> None:
    response = CommandResult(
        stdout='[{"name":"e2-small","zone":"projects/p/zones/us-central1-a"}]'
    )
    runner = FakeRunner([response])
    client = GCloudClient(profile(), runner)

    assert client.list_machine_type_zones("e2-small") == ["us-central1-a"]
    assert "--filter=name=e2-small" in runner.commands[0]


def test_create_instance_parses_external_ip() -> None:
    response = CommandResult(
        stdout="""[
          {
            "name": "scamper-baseline-us-central1-a-1",
            "zone": "projects/p/zones/us-central1-a",
            "machineType": "projects/p/zones/us-central1-a/machineTypes/e2-small",
            "status": "RUNNING",
            "networkInterfaces": [{"accessConfigs": [{"natIP": "192.0.2.2"}]}]
          }
        ]"""
    )
    client = GCloudClient(profile(), FakeRunner([response]))

    instance = client.create_instance(
        name="scamper-baseline-us-central1-a-1",
        zone="us-central1-a",
        machine_type="e2-small",
        disk_size_gb=20,
        image_family="ubuntu-2204-lts",
        image_project="ubuntu-os-cloud",
        network="default",
        run_id="baseline",
        startup_script=Path("startup.sh"),
    )

    assert instance.external_ip == "192.0.2.2"
    assert instance.machine_type == "e2-small"
    assert instance.zone == "us-central1-a"


def test_delete_instance_treats_not_found_as_success() -> None:
    runner = FakeRunner(
        [CommandResult(stderr="The resource was not found", returncode=1)]
    )
    client = GCloudClient(profile(), runner)

    client.delete_instance(
        Instance("scamper-test-us-central1-a-1", "us-central1-a", "e2-small")
    )


def test_delete_instance_preserves_other_failures() -> None:
    runner = FakeRunner([CommandResult(stderr="permission denied", returncode=1)])
    client = GCloudClient(profile(), runner)

    with pytest.raises(CommandFailed, match="permission denied"):
        client.delete_instance(
            Instance("scamper-test-us-central1-a-1", "us-central1-a", "e2-small")
        )
