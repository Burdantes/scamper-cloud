from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

from scamperctl.gcloud import GCloudClient
from scamperctl.models import (
    Deployment,
    Instance,
    RunInventory,
    utc_now,
    validate_resource_name,
)
from scamperctl.runner import CommandFailed
from scamperctl.store import Store


REMOTE_ROOT = "/var/lib/scamperctl"
DEFAULT_SCAMPER_ARGS = '-c "trace -l 20 -g 8 -w 3 -P ICMP" -p 10000'
RegistryAuth = Literal["auto", "none", "artifact-registry"]


@dataclass(frozen=True)
class ProvisionOptions:
    run_id: str
    zones: tuple[str, ...]
    machine_type: str
    count_per_zone: int = 1
    disk_size_gb: int = 20
    image_family: str = "ubuntu-2204-lts"
    image_project: str = "ubuntu-os-cloud"
    network: str = "default"
    service_account: str | None = None
    max_vms: int = 20

    def __post_init__(self) -> None:
        validate_resource_name(self.run_id, "run ID")
        if not self.zones:
            raise ValueError("at least one zone is required")
        if self.count_per_zone < 1:
            raise ValueError("count per zone must be at least 1")
        if self.disk_size_gb < 10:
            raise ValueError("boot disk size must be at least 10 GB")
        if self.max_vms < 1:
            raise ValueError("max VMs must be at least 1")


def startup_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker.io ca-certificates curl python3
systemctl enable --now docker
install -d -m 0755 /var/lib/scamperctl
"""


def instance_name(run_id: str, zone: str, index: int) -> str:
    candidate = f"scamper-{run_id}-{zone}-{index}"
    if len(candidate) <= 63:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:8]
    return f"{candidate[:54].rstrip('-')}-{digest}"


def shell_join(args: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in args)


def image_registry(image: str) -> str:
    first_component, separator, _ = image.partition("/")
    if not separator or not first_component:
        return "docker.io"
    if "." in first_component or ":" in first_component or first_component == "localhost":
        return first_component
    return "docker.io"


def resolved_registry_auth(image: str, requested: RegistryAuth) -> RegistryAuth:
    if requested not in {"auto", "none", "artifact-registry"}:
        raise ValueError(f"unsupported registry authentication mode: {requested}")
    registry = image_registry(image)
    if requested == "auto":
        return "artifact-registry" if registry.endswith("-docker.pkg.dev") else "none"
    if requested == "artifact-registry" and not registry.endswith("-docker.pkg.dev"):
        raise ValueError(
            "--registry-auth=artifact-registry requires an image hosted at "
            "LOCATION-docker.pkg.dev"
        )
    return requested


def artifact_registry_pull_command(image: str) -> str:
    registry = image_registry(image)
    metadata_url = (
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/token"
    )
    script = "\n".join(
        [
            "set -eu",
            'docker_config="$(mktemp -d)"',
            'trap \'rm -rf "$docker_config"\' EXIT',
            "token=\"$(curl -fsS -H 'Metadata-Flavor: Google' "
            f"{shlex.quote(metadata_url)} | "
            "python3 -c 'import json,sys; print(json.load(sys.stdin)[\"access_token\"])')\"",
            'printf %s "$token" | docker --config "$docker_config" login '
            f"--username oauth2accesstoken --password-stdin {shlex.quote(registry)}",
            f'docker --config "$docker_config" pull {shlex.quote(image)}',
            f'docker --config "$docker_config" logout {shlex.quote(registry)} '
            ">/dev/null 2>&1 || true",
        ]
    )
    return shell_join(["sudo", "sh", "-c", script])


def image_pull_command(image: str, registry_auth: RegistryAuth) -> str:
    resolved = resolved_registry_auth(image, registry_auth)
    if resolved == "artifact-registry":
        return artifact_registry_pull_command(image)
    return shell_join(["sudo", "docker", "pull", image])


def resolve_zones(client: GCloudClient, zones: Sequence[str]) -> tuple[str, ...]:
    if len(zones) == 1 and zones[0].lower() == "all":
        return tuple(client.list_zones())
    if any(zone.lower() == "all" for zone in zones):
        raise ValueError("'all' cannot be combined with explicit zones")
    return tuple(dict.fromkeys(zones))


def build_provision_plan(
    client: GCloudClient,
    options: ProvisionOptions,
    startup_path: Path,
) -> dict[str, Any]:
    zones = resolve_zones(client, options.zones)
    count = len(zones) * options.count_per_zone
    if count > options.max_vms:
        raise ValueError(
            f"plan would create {count} VMs, exceeding --max-vms={options.max_vms}; "
            "increase the limit explicitly after reviewing the cost"
        )

    instances: list[dict[str, Any]] = []
    for zone in zones:
        for index in range(1, options.count_per_zone + 1):
            name = instance_name(options.run_id, zone, index)
            command = client.create_instance_args(
                name=name,
                zone=zone,
                machine_type=options.machine_type,
                disk_size_gb=options.disk_size_gb,
                image_family=options.image_family,
                image_project=options.image_project,
                network=options.network,
                run_id=options.run_id,
                startup_script=startup_path,
                service_account=options.service_account,
            )
            instances.append({"name": name, "zone": zone, "command": command})

    return {
        "action": "provision",
        "project": client.profile.project,
        "configuration": client.profile.configuration,
        "run_id": options.run_id,
        "machine_type": options.machine_type,
        "disk_size_gb": options.disk_size_gb,
        "service_account": options.service_account,
        "vm_count": count,
        "instances": instances,
    }


def provision(
    client: GCloudClient,
    store: Store,
    options: ProvisionOptions,
) -> RunInventory:
    run_dir = store.run_directory(options.run_id)
    startup_path = run_dir / "startup.sh"
    startup_path.parent.mkdir(parents=True, exist_ok=True)
    startup_path.write_text(startup_script(), encoding="utf-8")
    plan = build_provision_plan(client, options, startup_path)

    inventory_path = store.run_path(options.run_id)
    if inventory_path.exists():
        existing = store.get_inventory(options.run_id)
        if existing.destroyed_at is None:
            raise ValueError(
                f"run {options.run_id!r} already exists; choose another run ID or destroy it"
            )

    inventory = RunInventory(
        run_id=options.run_id,
        profile=client.profile.name,
        project=client.profile.project,
        machine_type=options.machine_type,
    )
    store.save_inventory(inventory)

    for item in plan["instances"]:
        instance = client.create_instance(
            name=item["name"],
            zone=item["zone"],
            machine_type=options.machine_type,
            disk_size_gb=options.disk_size_gb,
            image_family=options.image_family,
            image_project=options.image_project,
            network=options.network,
            run_id=options.run_id,
            startup_script=startup_path,
            service_account=options.service_account,
        )
        inventory = replace(inventory, instances=(*inventory.instances, instance))
        store.save_inventory(inventory)
    return inventory


def deployment_commands(
    client: GCloudClient,
    instance: Instance,
    *,
    run_id: str,
    experiment: str,
    image: str,
    registry_auth: RegistryAuth,
    targets: Path,
    scamper_args: str,
) -> tuple[list[str], list[str]]:
    validate_resource_name(experiment, "experiment name")
    if not instance.external_ip:
        raise ValueError(
            f"instance {instance.name!r} has no external IPv4 address; the current "
            "analysis pipeline requires the probe address in each warts filename"
        )
    incoming_name = f"scamperctl-{run_id}-{experiment}-targets.txt"
    experiment_root = f"{REMOTE_ROOT}/{run_id}/{experiment}"
    target_path = f"{experiment_root}/targets.txt"
    result_path = f"{experiment_root}/results"
    container_name = f"scamper-{run_id}-{experiment}"
    pull_command = image_pull_command(image, registry_auth)

    docker_command = [
        "sudo",
        "docker",
        "run",
        "--detach",
        f"--name={container_name}",
        f"--label=io.scamper.run={run_id}",
        f"--label=io.scamper.experiment={experiment}",
        "--cap-add=NET_RAW",
        "--cap-add=NET_ADMIN",
        "--network=host",
        f"--volume={target_path}:/experiment/targets.txt:ro",
        f"--volume={result_path}:/results",
        f"--env=PROBE_NAME={instance.name}",
        f"--env=PROBE_IP={instance.external_ip}",
        f"--env=EXPERIMENT_NAME={experiment}",
        f"--env=SCAMPER_ARGS={scamper_args}",
        image,
    ]
    remote_command = "; ".join(
        [
            "set -eu",
            shell_join(["sudo", "install", "-d", "-m", "0755", experiment_root, result_path]),
            shell_join(["sudo", "install", "-m", "0644", incoming_name, target_path]),
            shell_join(["rm", "-f", incoming_name]),
            pull_command,
            f"{shell_join(['sudo', 'docker', 'rm', '-f', container_name])} >/dev/null 2>&1 || true",
            shell_join(docker_command),
        ]
    )
    return (
        client.scp_to_args(instance, targets, incoming_name),
        client.ssh_args(instance, remote_command),
    )


def build_deployment_plan(
    client: GCloudClient,
    inventory: RunInventory,
    *,
    experiment: str,
    image: str,
    registry_auth: RegistryAuth,
    targets: Path,
    scamper_args: str,
) -> dict[str, Any]:
    if not targets.is_file():
        raise FileNotFoundError(f"target file does not exist: {targets}")
    if inventory.destroyed_at is not None:
        raise ValueError(f"run {inventory.run_id!r} has already been destroyed")
    if not inventory.instances:
        raise ValueError(f"run {inventory.run_id!r} has no provisioned instances")

    commands = []
    for instance in inventory.instances:
        scp_command, ssh_command = deployment_commands(
            client,
            instance,
            run_id=inventory.run_id,
            experiment=experiment,
            image=image,
            registry_auth=registry_auth,
            targets=targets,
            scamper_args=scamper_args,
        )
        commands.append(
            {
                "instance": instance.name,
                "zone": instance.zone,
                "commands": [scp_command, ssh_command],
            }
        )
    return {
        "action": "deploy",
        "run_id": inventory.run_id,
        "experiment": experiment,
        "image": image,
        "registry_auth": resolved_registry_auth(image, registry_auth),
        "target_file": str(targets),
        "instance_count": len(inventory.instances),
        "instances": commands,
    }


def deploy(
    client: GCloudClient,
    store: Store,
    inventory: RunInventory,
    *,
    experiment: str,
    image: str,
    registry_auth: RegistryAuth,
    targets: Path,
    scamper_args: str,
) -> RunInventory:
    build_deployment_plan(
        client,
        inventory,
        experiment=experiment,
        image=image,
        registry_auth=registry_auth,
        targets=targets,
        scamper_args=scamper_args,
    )
    for instance in inventory.instances:
        incoming_name = f"scamperctl-{inventory.run_id}-{experiment}-targets.txt"
        _, ssh_command = deployment_commands(
            client,
            instance,
            run_id=inventory.run_id,
            experiment=experiment,
            image=image,
            registry_auth=registry_auth,
            targets=targets,
            scamper_args=scamper_args,
        )
        client.scp_to(instance, targets, incoming_name)
        client.runner.run(ssh_command)

    deployment = Deployment(
        experiment=experiment,
        image=image,
        registry_auth=resolved_registry_auth(image, registry_auth),
        target_file=str(targets),
        scamper_args=scamper_args,
    )
    deployments = tuple(
        item for item in inventory.deployments if item.experiment != experiment
    )
    updated = replace(inventory, deployments=(*deployments, deployment))
    store.save_inventory(updated)
    return updated


def status(client: GCloudClient, inventory: RunInventory) -> dict[str, Any]:
    instances = []
    for instance in inventory.instances:
        try:
            current = client.describe_instance(instance)
            instances.append(current.to_dict())
        except CommandFailed as err:
            instances.append({**instance.to_dict(), "status": "UNAVAILABLE", "error": str(err)})
    return {
        "run_id": inventory.run_id,
        "project": inventory.project,
        "destroyed_at": inventory.destroyed_at,
        "instances": instances,
        "deployments": [item.to_dict() for item in inventory.deployments],
    }


def build_destroy_plan(client: GCloudClient, inventory: RunInventory) -> dict[str, Any]:
    return {
        "action": "destroy",
        "run_id": inventory.run_id,
        "project": inventory.project,
        "instances": [
            {
                "name": instance.name,
                "zone": instance.zone,
                "command": client.delete_instance_args(instance),
            }
            for instance in inventory.instances
        ],
    }


def destroy(
    client: GCloudClient,
    store: Store,
    inventory: RunInventory,
) -> RunInventory:
    if inventory.destroyed_at is not None:
        return inventory
    for instance in inventory.instances:
        client.delete_instance(instance)
    updated = replace(inventory, destroyed_at=utc_now())
    store.save_inventory(updated)
    return updated


def collect(
    client: GCloudClient,
    inventory: RunInventory,
    *,
    experiment: str,
    destination: Path,
) -> Path:
    validate_resource_name(experiment, "experiment name")
    root = destination / inventory.run_id / experiment
    root.mkdir(parents=True, exist_ok=True)
    for instance in inventory.instances:
        instance_dir = root / instance.name
        instance_dir.mkdir(parents=True, exist_ok=True)
        remote_path = f"{REMOTE_ROOT}/{inventory.run_id}/{experiment}/results"
        client.scp_from(instance, remote_path, instance_dir)
    return root


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2))
