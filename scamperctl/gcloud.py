from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from scamperctl.models import GCPProfile, Instance
from scamperctl.runner import CommandFailed, Runner


def _parse_json(value: str, context: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as err:
        raise CommandFailed(f"gcloud returned invalid JSON while {context}: {err}") from err


class GCloudClient:
    def __init__(self, profile: GCPProfile, runner: Runner) -> None:
        self.profile = profile
        self.runner = runner

    @staticmethod
    def configuration_list_args() -> list[str]:
        return [
            "gcloud",
            "config",
            "configurations",
            "list",
            "--format=json(name,is_active,properties.core.account,properties.core.project)",
        ]

    @classmethod
    def list_configurations(cls, runner: Runner) -> list[dict[str, Any]]:
        result = runner.run(cls.configuration_list_args())
        value = _parse_json(result.stdout, "listing configurations")
        if not isinstance(value, list):
            raise CommandFailed("gcloud configuration list did not return a JSON list")
        return value

    def _command(self, *args: str) -> list[str]:
        return [
            "gcloud",
            f"--configuration={self.profile.configuration}",
            f"--project={self.profile.project}",
            "--quiet",
            *args,
        ]

    def zone_list_args(self) -> list[str]:
        return self._command(
            "compute",
            "zones",
            "list",
            "--filter=status=UP",
            "--format=json",
        )

    def list_zones(self) -> list[str]:
        result = self.runner.run(self.zone_list_args())
        values = _parse_json(result.stdout, "listing zones")
        return sorted(str(value["name"]) for value in values)

    def machine_type_zone_list_args(self, machine_type: str) -> list[str]:
        return self._command(
            "compute",
            "machine-types",
            "list",
            f"--filter=name={machine_type}",
            "--format=json(name,zone)",
        )

    def list_machine_type_zones(self, machine_type: str) -> list[str]:
        result = self.runner.run(self.machine_type_zone_list_args(machine_type))
        values = _parse_json(result.stdout, "listing machine type zones")
        if not isinstance(values, list):
            raise CommandFailed("gcloud machine type zone list did not return a JSON list")
        return sorted(str(value["zone"]).rsplit("/", 1)[-1] for value in values)

    def project_info_args(self) -> list[str]:
        return self._command(
            "compute",
            "project-info",
            "describe",
            "--format=json(commonInstanceMetadata.items)",
        )

    def project_os_login_enabled(self) -> bool:
        result = self.runner.run(self.project_info_args())
        value = _parse_json(result.stdout, "checking project OS Login metadata")
        if not isinstance(value, dict):
            raise CommandFailed("gcloud project info did not return a JSON object")
        metadata = value.get("commonInstanceMetadata", {})
        items = metadata.get("items", []) if isinstance(metadata, dict) else []
        return any(
            str(item.get("key", "")).lower() == "enable-oslogin"
            and str(item.get("value", "")).lower() == "true"
            for item in items
            if isinstance(item, dict)
        )

    def machine_type_list_args(self, zone: str) -> list[str]:
        return self._command(
            "compute",
            "machine-types",
            "list",
            f"--zones={zone}",
            "--format=json(name,guestCpus,memoryMb,zone)",
        )

    def list_machine_types(self, zone: str) -> list[dict[str, Any]]:
        result = self.runner.run(self.machine_type_list_args(zone))
        values = _parse_json(result.stdout, "listing machine types")
        if not isinstance(values, list):
            raise CommandFailed("gcloud machine type list did not return a JSON list")
        return values

    def create_instance_args(
        self,
        *,
        name: str,
        zone: str,
        machine_type: str,
        disk_size_gb: int,
        image_family: str,
        image_project: str,
        network: str,
        run_id: str,
        startup_script: Path,
        service_account: str | None = None,
        max_run_duration_seconds: int | None = None,
        ssh_keys_file: Path | None = None,
    ) -> list[str]:
        metadata_files = f"startup-script={startup_script}"
        if ssh_keys_file is not None:
            metadata_files = f"{metadata_files},ssh-keys={ssh_keys_file}"
        args = [
            "compute",
            "instances",
            "create",
            name,
            f"--zone={zone}",
            f"--machine-type={machine_type}",
            f"--boot-disk-size={disk_size_gb}GB",
            f"--image-family={image_family}",
            f"--image-project={image_project}",
            f"--network={network}",
            "--tags=scamperctl",
            f"--labels=managed-by=scamperctl,scamper-run={run_id}",
            f"--metadata-from-file={metadata_files}",
        ]
        if service_account:
            args.extend(
                [
                    f"--service-account={service_account}",
                    "--scopes=https://www.googleapis.com/auth/devstorage.read_only",
                ]
            )
        if max_run_duration_seconds is not None:
            args.extend(
                [
                    f"--max-run-duration={max_run_duration_seconds}s",
                    "--instance-termination-action=DELETE",
                ]
            )
        args.append("--format=json")
        return self._command(*args)

    def create_instance(self, **kwargs: Any) -> Instance:
        result = self.runner.run(self.create_instance_args(**kwargs))
        values = _parse_json(result.stdout, "creating an instance")
        if isinstance(values, list):
            if len(values) != 1:
                raise CommandFailed("gcloud returned an unexpected number of instances")
            value = values[0]
        else:
            value = values
        return self._instance_from_json(value, kwargs["machine_type"], kwargs["zone"])

    def describe_instance_args(self, instance: Instance) -> list[str]:
        return self._command(
            "compute",
            "instances",
            "describe",
            instance.name,
            f"--zone={instance.zone}",
            "--format=json",
        )

    def describe_instance(self, instance: Instance) -> Instance:
        result = self.runner.run(self.describe_instance_args(instance))
        value = _parse_json(result.stdout, "describing an instance")
        return self._instance_from_json(value, instance.machine_type, instance.zone)

    def delete_instance_args(self, instance: Instance) -> list[str]:
        return self._command(
            "compute",
            "instances",
            "delete",
            instance.name,
            f"--zone={instance.zone}",
        )

    def delete_instance(self, instance: Instance) -> None:
        result = self.runner.run(self.delete_instance_args(instance), check=False)
        if result.returncode == 0:
            return
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        if "not found" in detail.lower():
            return
        raise CommandFailed(f"command failed ({result.returncode}): {detail}")

    def ssh_args(self, instance: Instance, remote_command: str) -> list[str]:
        args = self._command(
            "compute",
            "ssh",
            instance.name,
            f"--zone={instance.zone}",
            f"--command={remote_command}",
        )
        if self.profile.use_iap:
            args.append("--tunnel-through-iap")
        return args

    def ssh(self, instance: Instance, remote_command: str) -> None:
        self.runner.run(self.ssh_args(instance, remote_command))

    def scp_to_args(self, instance: Instance, source: Path, destination: str) -> list[str]:
        args = self._command(
            "compute",
            "scp",
            str(source),
            f"{instance.name}:{destination}",
            f"--zone={instance.zone}",
        )
        if self.profile.use_iap:
            args.append("--tunnel-through-iap")
        return args

    def scp_to(self, instance: Instance, source: Path, destination: str) -> None:
        self.runner.run(self.scp_to_args(instance, source, destination))

    def scp_from_args(
        self,
        instance: Instance,
        source: str,
        destination: Path,
    ) -> list[str]:
        args = self._command(
            "compute",
            "scp",
            "--recurse",
            f"{instance.name}:{source}",
            str(destination),
            f"--zone={instance.zone}",
        )
        if self.profile.use_iap:
            args.append("--tunnel-through-iap")
        return args

    def scp_from(self, instance: Instance, source: str, destination: Path) -> None:
        self.runner.run(self.scp_from_args(instance, source, destination))

    @staticmethod
    def _instance_from_json(
        value: dict[str, Any], fallback_machine_type: str, fallback_zone: str
    ) -> Instance:
        interfaces = value.get("networkInterfaces", [])
        access_configs = interfaces[0].get("accessConfigs", []) if interfaces else []
        external_ip = access_configs[0].get("natIP") if access_configs else None
        machine_type = str(value.get("machineType", fallback_machine_type)).rsplit("/", 1)[-1]
        zone = str(value.get("zone", fallback_zone)).rsplit("/", 1)[-1]
        return Instance(
            name=str(value["name"]),
            zone=zone,
            machine_type=machine_type,
            external_ip=external_ip,
            status=str(value.get("status", "UNKNOWN")),
        )
