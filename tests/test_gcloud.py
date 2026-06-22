from pathlib import Path
from typing import Sequence

from scamperctl.gcloud import GCloudClient
from scamperctl.models import GCPProfile
from scamperctl.runner import CommandResult


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
    )

    assert "--configuration=research" in command
    assert "--project=example-project" in command
    assert "--machine-type=e2-standard-2" in command
    assert (
        "--service-account=measurement-vm@example-project.iam.gserviceaccount.com"
        in command
    )
    assert "--scopes=https://www.googleapis.com/auth/cloud-platform" in command


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
