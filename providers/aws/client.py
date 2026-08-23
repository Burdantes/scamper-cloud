"""AWS EC2 client satisfying the scamperctl CloudClient contract.

Shaped deliberately like ``scamperctl.gcloud.GCloudClient``: every operation has
an ``*_args`` builder returning the argv, and a method that runs it through a
Runner and parses the result. Keeping argv separable is what makes the workflow
loggable and dry-runnable.

Two behaviours are load-bearing and differ from the legacy AWS driver:

* ``instance_type`` is a constructor argument, not a module-level list. The
  legacy driver hardcoded ``['t3.micro', 't2.micro']`` - cheap, so harmless -
  but the Azure campaign showed what an unconfigurable size costs when the
  default is wrong.
* ``delete_instance`` calls ``terminate-instances``, never ``stop-instances``.
  A stopped EC2 instance releases compute but keeps its EBS volumes, and the
  equivalent Azure mistake billed compute for eight idle days. Teardown here
  means gone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scamperctl.models import Instance
from scamperctl.runner import Runner, SubprocessRunner

DEFAULT_INSTANCE_TYPE = "t3.micro"


def _parse_json(payload: str, context: str) -> Any:
    try:
        return json.loads(payload or "{}")
    except json.JSONDecodeError as err:
        raise ValueError(f"invalid JSON while {context}: {err}") from err


class AWSClient:
    """Drive EC2 through the ``aws`` CLI."""

    def __init__(
        self,
        *,
        region: str,
        runner: Runner | None = None,
        executable: str = "aws",
        profile: str | None = None,
        instance_type: str = DEFAULT_INSTANCE_TYPE,
        ssh_user: str = "ubuntu",
        ssh_key: Path | None = None,
    ) -> None:
        if not region.strip():
            raise ValueError("region cannot be empty")
        if not instance_type.strip():
            raise ValueError("instance type cannot be empty")
        self.region = region
        self.runner = runner or SubprocessRunner()
        self.executable = executable
        self.profile = profile
        self.instance_type = instance_type
        self.ssh_user = ssh_user
        self.ssh_key = ssh_key

    # ------------------------------------------------------------------ argv
    def _command(self, *args: str) -> list[str]:
        command = [self.executable, "--region", self.region]
        if self.profile:
            command += ["--profile", self.profile]
        command += list(args)
        command += ["--output", "json"]
        return command

    def zone_list_args(self) -> list[str]:
        return self._command(
            "ec2",
            "describe-availability-zones",
            "--query",
            "AvailabilityZones[?State=='available'].ZoneName",
        )

    def machine_type_zone_list_args(self, machine_type: str) -> list[str]:
        return self._command(
            "ec2",
            "describe-instance-type-offerings",
            "--location-type",
            "availability-zone",
            "--filters",
            f"Name=instance-type,Values={machine_type}",
            "--query",
            "InstanceTypeOfferings[].Location",
        )

    def create_instance_args(
        self,
        *,
        name: str,
        zone: str,
        image_id: str,
        machine_type: str | None = None,
        security_group_id: str | None = None,
        disk_size_gb: int | None = None,
    ) -> list[str]:
        args = [
            "ec2",
            "run-instances",
            "--image-id",
            image_id,
            "--instance-type",
            machine_type or self.instance_type,
            "--count",
            "1",
            "--placement",
            f"AvailabilityZone={zone}",
            "--tag-specifications",
            f"ResourceType=instance,Tags=[{{Key=Name,Value={name}}}]",
            # If the guest halts, the instance goes away rather than lingering
            # as a stopped resource that still holds volumes.
            "--instance-initiated-shutdown-behavior",
            "terminate",
        ]
        if security_group_id:
            args += ["--security-group-ids", security_group_id]
        if disk_size_gb:
            args += [
                "--block-device-mappings",
                json.dumps(
                    [
                        {
                            "DeviceName": "/dev/sda1",
                            "Ebs": {
                                "VolumeSize": int(disk_size_gb),
                                "VolumeType": "gp3",
                                "DeleteOnTermination": True,
                                "Encrypted": True,
                            },
                        }
                    ],
                    separators=(",", ":"),
                ),
            ]
        return self._command(*args)

    def describe_instance_args(self, instance: Instance) -> list[str]:
        return self._command(
            "ec2",
            "describe-instances",
            "--filters",
            f"Name=tag:Name,Values={instance.name}",
        )

    def delete_instance_args(self, instance: Instance) -> list[str]:
        # terminate, never stop: a stopped instance keeps its EBS volumes.
        return self._command(
            "ec2",
            "terminate-instances",
            "--instance-ids",
            instance.name,
        )

    def _ssh_prefix(self, program: str) -> list[str]:
        args = [program, "-o", "StrictHostKeyChecking=accept-new"]
        if self.ssh_key:
            args += ["-i", str(self.ssh_key)]
        return args

    def _target_host(self, instance: Instance) -> str:
        return f"{self.ssh_user}@{instance.external_ip or instance.name}"

    def ssh_args(self, instance: Instance, remote_command: str) -> list[str]:
        return self._ssh_prefix("ssh") + [self._target_host(instance), remote_command]

    def scp_to_args(self, instance: Instance, source: Path, destination: str) -> list[str]:
        target = f"{self._target_host(instance)}:{destination}"
        return self._ssh_prefix("scp") + [str(source), target]

    def scp_from_args(self, instance: Instance, remote: str, destination: Path) -> list[str]:
        source = f"{self._target_host(instance)}:{remote}"
        return self._ssh_prefix("scp") + [source, str(destination)]

    # --------------------------------------------------------------- actions
    def list_zones(self) -> list[str]:
        result = self.runner.run(self.zone_list_args())
        return [str(value) for value in _parse_json(result.stdout, "listing zones")]

    def list_machine_type_zones(self, machine_type: str) -> list[str]:
        result = self.runner.run(self.machine_type_zone_list_args(machine_type))
        return [
            str(value)
            for value in _parse_json(result.stdout, "listing instance type zones")
        ]

    def create_instance(self, **kwargs: Any) -> Instance:
        result = self.runner.run(self.create_instance_args(**kwargs))
        values = _parse_json(result.stdout, "creating an instance")
        created = values.get("Instances") or []
        if not created:
            raise ValueError("run-instances returned no instance")
        return self._instance_from(created[0], kwargs["zone"])

    def describe_instance(self, instance: Instance) -> Instance:
        result = self.runner.run(self.describe_instance_args(instance))
        values = _parse_json(result.stdout, "describing an instance")
        for reservation in values.get("Reservations") or []:
            for item in reservation.get("Instances") or []:
                return self._instance_from(item, instance.zone)
        raise ValueError(f"instance {instance.name!r} was not found")

    def delete_instance(self, instance: Instance) -> None:
        self.runner.run(self.delete_instance_args(instance), check=False)

    def scp_to(self, instance: Instance, source: Path, destination: str) -> None:
        self.runner.run(self.scp_to_args(instance, source, destination))

    def scp_from(self, instance: Instance, remote: str, destination: Path) -> None:
        self.runner.run(self.scp_from_args(instance, remote, destination))

    def _instance_from(self, value: dict[str, Any], zone: str) -> Instance:
        placement = value.get("Placement") or {}
        state = (value.get("State") or {}).get("Name") or "UNKNOWN"
        return Instance(
            name=str(value.get("InstanceId") or ""),
            zone=str(placement.get("AvailabilityZone") or zone),
            machine_type=str(value.get("InstanceType") or self.instance_type),
            external_ip=value.get("PublicIpAddress"),
            status=str(state).upper(),
        )
