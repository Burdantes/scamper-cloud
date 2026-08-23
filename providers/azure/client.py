"""Azure client satisfying the scamperctl CloudClient contract.

Every difference from the legacy Azure path here exists because the
2026-08-13 campaign got it wrong and the data was unusable. Three of them:

* **Inbound ICMP is allowed as part of instance creation, not as an optional
  extra.** Azure NSGs are stateful per flow: an outbound echo request admits the
  destination's echo reply, but an ICMP Time Exceeded arrives from a different
  source, matches no flow, and hits the default ``DenyAllInbound``. A traceroute
  VM without an inbound ICMP allow rule therefore sees destinations and *no
  intermediate hops at all*. That produced 43 of 44 regions with zero IP links
  while every run reported success. ``create_instance`` ensures the rule, so it
  cannot be omitted by a caller who does not know to ask.

* **``Standard_B2ts_v2`` is the default size.** The campaign used
  ``Standard_D2s_v5`` (2 vCPU / 8 GiB) for a network-bound workload at roughly
  $0.096-0.14/hr per VM. B2ts_v2 is ~$0.0083/hr with the same vCPU count and a
  30% CPU baseline, so it does not throttle on a multi-hour run the way B1s
  (10% baseline) can.

* **Teardown deletes.** ``az vm stop`` leaves a VM *allocated* and still billing
  compute at full rate; only deallocation releases it, and even then disks and
  public IPs keep charging. 43 stopped VMs cost roughly $150/day for eight days.
  ``delete_instance`` deletes. ``deallocate_instance_args`` exists for pausing a
  VM you intend to restart, and its docstring says it is not teardown.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scamperctl.models import Instance
from scamperctl.runner import Runner, SubprocessRunner

DEFAULT_INSTANCE_TYPE = "Standard_B2ts_v2"
DEFAULT_OS_DISK_SKU = "StandardSSD_LRS"
ICMP_RULE_NAME = "AllowInboundICMP"
ICMP_RULE_PRIORITY = 100


def _parse_json(payload: str, context: str) -> Any:
    try:
        return json.loads(payload or "{}")
    except json.JSONDecodeError as err:
        raise ValueError(f"invalid JSON while {context}: {err}") from err


class AzureClient:
    """Drive Azure VMs through the ``az`` CLI."""

    def __init__(
        self,
        *,
        region: str,
        resource_group: str,
        runner: Runner | None = None,
        executable: str = "az",
        subscription: str | None = None,
        instance_type: str = DEFAULT_INSTANCE_TYPE,
        os_disk_sku: str = DEFAULT_OS_DISK_SKU,
        image: str = "Ubuntu2204",
        ssh_user: str = "azureuser",
        ssh_key: Path | None = None,
    ) -> None:
        if not region.strip():
            raise ValueError("region cannot be empty")
        if not resource_group.strip():
            raise ValueError("resource group cannot be empty")
        if not instance_type.strip():
            raise ValueError("instance type cannot be empty")
        self.region = region
        self.resource_group = resource_group
        self.runner = runner or SubprocessRunner()
        self.executable = executable
        self.subscription = subscription
        self.instance_type = instance_type
        self.os_disk_sku = os_disk_sku
        self.image = image
        self.ssh_user = ssh_user
        self.ssh_key = ssh_key

    # ------------------------------------------------------------------ argv
    def _command(self, *args: str) -> list[str]:
        command = [self.executable, *args]
        if self.subscription:
            command += ["--subscription", self.subscription]
        command += ["--output", "json"]
        return command

    def nsg_name(self, instance_name: str) -> str:
        return f"{instance_name}NSG"

    def zone_list_args(self) -> list[str]:
        return self._command(
            "account",
            "list-locations",
            "--query",
            "[?metadata.regionType=='Physical'].name",
        )

    def machine_type_zone_list_args(self, machine_type: str) -> list[str]:
        return self._command(
            "vm",
            "list-skus",
            "--size",
            machine_type,
            "--query",
            "[?resourceType=='virtualMachines'].locationInfo[].location",
        )

    def allow_icmp_rule_args(self, instance_name: str) -> list[str]:
        """Inbound ICMP allow rule.

        Without this the VM receives echo replies but not Time Exceeded, so a
        traceroute observes destinations and no path. See the module docstring.
        """
        return self._command(
            "network",
            "nsg",
            "rule",
            "create",
            "--resource-group",
            self.resource_group,
            "--nsg-name",
            self.nsg_name(instance_name),
            "--name",
            ICMP_RULE_NAME,
            "--priority",
            str(ICMP_RULE_PRIORITY),
            "--direction",
            "Inbound",
            "--access",
            "Allow",
            "--protocol",
            "Icmp",
            "--source-address-prefixes",
            "*",
            "--destination-port-ranges",
            "*",
        )

    def create_instance_args(
        self,
        *,
        name: str,
        zone: str | None = None,
        machine_type: str | None = None,
        disk_size_gb: int | None = None,
        image: str | None = None,
    ) -> list[str]:
        args = [
            "vm",
            "create",
            "--resource-group",
            self.resource_group,
            "--name",
            name,
            "--location",
            self.region,
            "--size",
            machine_type or self.instance_type,
            "--image",
            image or self.image,
            "--admin-username",
            self.ssh_user,
            "--public-ip-sku",
            "Standard",
            "--storage-sku",
            self.os_disk_sku,
            "--nsg",
            self.nsg_name(name),
        ]
        if self.ssh_key:
            args += ["--ssh-key-values", str(self.ssh_key)]
        else:
            args += ["--generate-ssh-keys"]
        if disk_size_gb:
            args += ["--os-disk-size-gb", str(int(disk_size_gb))]
        if zone:
            args += ["--zone", str(zone)]
        return self._command(*args)

    def describe_instance_args(self, instance: Instance) -> list[str]:
        return self._command(
            "vm",
            "show",
            "--resource-group",
            self.resource_group,
            "--name",
            instance.name,
            "--show-details",
        )

    def delete_instance_args(self, instance: Instance) -> list[str]:
        """Delete, not stop and not deallocate. See the module docstring."""
        return self._command(
            "vm",
            "delete",
            "--resource-group",
            self.resource_group,
            "--name",
            instance.name,
            "--yes",
        )

    def deallocate_instance_args(self, instance: Instance) -> list[str]:
        """Release compute while keeping the VM.

        NOT teardown: disks and public IPs keep billing. Use only to pause a VM
        you intend to restart; use :meth:`delete_instance_args` to finish with
        it. Never use ``az vm stop``, which leaves the VM allocated and billing
        compute at full rate.
        """
        return self._command(
            "vm",
            "deallocate",
            "--resource-group",
            self.resource_group,
            "--name",
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
        return [str(value) for value in _parse_json(result.stdout, "listing locations")]

    def list_machine_type_zones(self, machine_type: str) -> list[str]:
        result = self.runner.run(self.machine_type_zone_list_args(machine_type))
        return [
            str(value)
            for value in _parse_json(result.stdout, "listing VM size locations")
        ]

    def create_instance(self, **kwargs: Any) -> Instance:
        result = self.runner.run(self.create_instance_args(**kwargs))
        values = _parse_json(result.stdout, "creating an instance")
        instance = self._instance_from(values, kwargs.get("zone"), kwargs["name"])
        # Ensured here rather than left to the caller: a traceroute VM without
        # this rule silently measures nothing.
        self.runner.run(self.allow_icmp_rule_args(kwargs["name"]))
        return instance

    def describe_instance(self, instance: Instance) -> Instance:
        result = self.runner.run(self.describe_instance_args(instance))
        values = _parse_json(result.stdout, "describing an instance")
        return self._instance_from(values, instance.zone, instance.name)

    def delete_instance(self, instance: Instance) -> None:
        self.runner.run(self.delete_instance_args(instance), check=False)

    def scp_to(self, instance: Instance, source: Path, destination: str) -> None:
        self.runner.run(self.scp_to_args(instance, source, destination))

    def scp_from(self, instance: Instance, remote: str, destination: Path) -> None:
        self.runner.run(self.scp_from_args(instance, remote, destination))

    def _instance_from(
        self, value: dict[str, Any], zone: Any, fallback_name: str
    ) -> Instance:
        hardware = value.get("hardwareProfile") or {}
        zones = value.get("zones") or ([] if zone is None else [str(zone)])
        return Instance(
            name=str(value.get("name") or fallback_name),
            zone=str(zones[0]) if zones else str(value.get("location") or self.region),
            machine_type=str(hardware.get("vmSize") or self.instance_type),
            external_ip=value.get("publicIps") or None,
            status=str(value.get("powerState") or "UNKNOWN").upper(),
        )
