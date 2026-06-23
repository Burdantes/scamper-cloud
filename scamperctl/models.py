from __future__ import annotations

import base64
import re
from binascii import Error as Base64Error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
from typing import Any


_NAME_PATTERN = re.compile(r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SSH_USER_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_SUPPORTED_SSH_KEY_TYPES = {
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ecdsa-sha2-nistp256@openssh.com",
    "sk-ssh-ed25519@openssh.com",
    "ssh-ed25519",
    "ssh-rsa",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_resource_name(value: str, field_name: str = "name") -> str:
    if not _NAME_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field_name} must start with a lowercase letter, contain only "
            "lowercase letters, numbers, or hyphens, and be at most 63 characters"
        )
    return value


@dataclass(frozen=True)
class GCPProfile:
    name: str
    project: str
    configuration: str = "default"
    use_iap: bool = False

    def __post_init__(self) -> None:
        validate_resource_name(self.name, "profile name")
        if not self.project.strip():
            raise ValueError("project cannot be empty")
        if not self.configuration.strip():
            raise ValueError("gcloud configuration cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": "gcp",
            "project": self.project,
            "configuration": self.configuration,
            "use_iap": self.use_iap,
        }

    @classmethod
    def from_dict(cls, name: str, value: dict[str, Any]) -> "GCPProfile":
        if value.get("provider") != "gcp":
            raise ValueError(f"profile {name!r} is not a GCP profile")
        return cls(
            name=name,
            project=str(value["project"]),
            configuration=str(value.get("configuration", "default")),
            use_iap=bool(value.get("use_iap", False)),
        )


@dataclass(frozen=True)
class Instance:
    name: str
    zone: str
    machine_type: str
    external_ip: str | None = None
    status: str = "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "zone": self.zone,
            "machine_type": self.machine_type,
            "external_ip": self.external_ip,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Instance":
        return cls(
            name=str(value["name"]),
            zone=str(value["zone"]),
            machine_type=str(value["machine_type"]),
            external_ip=value.get("external_ip"),
            status=str(value.get("status", "UNKNOWN")),
        )


@dataclass(frozen=True)
class CostGuard:
    estimated_vm_hourly_usd: float
    estimated_disk_gb_monthly_usd: float
    max_runtime_hours: float
    max_estimated_cost_usd: float

    def __post_init__(self) -> None:
        values = {
            "estimated VM hourly cost": self.estimated_vm_hourly_usd,
            "estimated disk GB-month cost": self.estimated_disk_gb_monthly_usd,
            "maximum runtime": self.max_runtime_hours,
            "maximum estimated cost": self.max_estimated_cost_usd,
        }
        for name, value in values.items():
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        if self.max_runtime_hours * 3600 < 30:
            raise ValueError("maximum runtime must be at least 30 seconds")
        if self.max_runtime_hours > 120 * 24:
            raise ValueError("maximum runtime cannot exceed 120 days")

    def to_dict(self) -> dict[str, float]:
        return {
            "estimated_vm_hourly_usd": self.estimated_vm_hourly_usd,
            "estimated_disk_gb_monthly_usd": self.estimated_disk_gb_monthly_usd,
            "max_runtime_hours": self.max_runtime_hours,
            "max_estimated_cost_usd": self.max_estimated_cost_usd,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CostGuard":
        return cls(
            estimated_vm_hourly_usd=float(value["estimated_vm_hourly_usd"]),
            estimated_disk_gb_monthly_usd=float(
                value["estimated_disk_gb_monthly_usd"]
            ),
            max_runtime_hours=float(value["max_runtime_hours"]),
            max_estimated_cost_usd=float(value["max_estimated_cost_usd"]),
        )


@dataclass(frozen=True)
class SSHAccess:
    username: str
    public_key: str

    def __post_init__(self) -> None:
        if self.username == "root" or not _SSH_USER_PATTERN.fullmatch(self.username):
            raise ValueError(
                "SSH username must be a non-root Linux username containing only "
                "lowercase letters, numbers, underscores, or hyphens"
            )
        if len(self.public_key.splitlines()) != 1:
            raise ValueError("SSH public key must contain exactly one line")
        parts = self.public_key.strip().split()
        if len(parts) < 2 or parts[0] not in _SUPPORTED_SSH_KEY_TYPES:
            raise ValueError("file does not contain a supported OpenSSH public key")
        encoded = parts[1]
        try:
            decoded = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4),
                validate=True,
            )
        except (Base64Error, ValueError) as err:
            raise ValueError("SSH public key body is not valid base64") from err
        if not decoded:
            raise ValueError("SSH public key body cannot be empty")

    @property
    def metadata_line(self) -> str:
        return f"{self.username}:{self.public_key.strip()}"

    @property
    def fingerprint(self) -> str:
        encoded = self.public_key.strip().split()[1]
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4))
        digest = base64.b64encode(sha256(decoded).digest()).decode("ascii")
        return f"SHA256:{digest.rstrip('=')}"


@dataclass(frozen=True)
class Deployment:
    experiment: str
    image: str
    registry_auth: str
    target_file: str
    scamper_args: str
    deployed_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, str]:
        return {
            "experiment": self.experiment,
            "image": self.image,
            "registry_auth": self.registry_auth,
            "target_file": self.target_file,
            "scamper_args": self.scamper_args,
            "deployed_at": self.deployed_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Deployment":
        return cls(
            experiment=str(value["experiment"]),
            image=str(value["image"]),
            registry_auth=str(value.get("registry_auth", "auto")),
            target_file=str(value["target_file"]),
            scamper_args=str(value["scamper_args"]),
            deployed_at=str(value["deployed_at"]),
        )


@dataclass(frozen=True)
class RunInventory:
    run_id: str
    profile: str
    project: str
    machine_type: str
    disk_size_gb: int = 20
    cost_guard: CostGuard | None = None
    created_at: str = field(default_factory=utc_now)
    instances: tuple[Instance, ...] = ()
    deployments: tuple[Deployment, ...] = ()
    destroyed_at: str | None = None

    def __post_init__(self) -> None:
        validate_resource_name(self.run_id, "run ID")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "provider": "gcp",
            "run_id": self.run_id,
            "profile": self.profile,
            "project": self.project,
            "machine_type": self.machine_type,
            "disk_size_gb": self.disk_size_gb,
            "cost_guard": self.cost_guard.to_dict() if self.cost_guard else None,
            "created_at": self.created_at,
            "instances": [instance.to_dict() for instance in self.instances],
            "deployments": [deployment.to_dict() for deployment in self.deployments],
            "destroyed_at": self.destroyed_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunInventory":
        if value.get("provider") != "gcp":
            raise ValueError("only GCP run inventories are currently supported")
        return cls(
            run_id=str(value["run_id"]),
            profile=str(value["profile"]),
            project=str(value["project"]),
            machine_type=str(value["machine_type"]),
            disk_size_gb=int(value.get("disk_size_gb", 20)),
            cost_guard=(
                CostGuard.from_dict(value["cost_guard"])
                if value.get("cost_guard")
                else None
            ),
            created_at=str(value["created_at"]),
            instances=tuple(Instance.from_dict(item) for item in value.get("instances", [])),
            deployments=tuple(
                Deployment.from_dict(item) for item in value.get("deployments", [])
            ),
            destroyed_at=value.get("destroyed_at"),
        )
