"""Read-only launch validation, isolated by the scheduler's process deadline."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any


def eligible_regions(regions: list[str], errors: list[str]) -> list[str]:
    """Return regions without regional errors, or none when an error is global."""
    regional_failures = set()
    for error in errors:
        prefix, separator, _message = error.partition(":")
        if not separator or prefix not in regions:
            return []
        regional_failures.add(prefix)
    return [region for region in regions if region not in regional_failures]


def check_gcp(request: dict[str, Any]) -> dict[str, Any]:
    from googleapiclient import discovery
    from providers.gcs_credentials import google_credentials
    from providers import settings

    # Discovery clients are not thread safe; GCP checks use this client serially.
    client = discovery.build("compute", "v1", credentials=google_credentials(), cache_discovery=False)
    project = settings.GCP_PROJECT
    image_project = request.get("worker_image_project") or settings.GCP_IMAGE_PROJECT
    image_name = request.get("worker_image")
    if not image_name and not request.get("worker_image_family"):
        image_name = settings.GCP_IMAGE
    image_family = request.get("worker_image_family") or settings.GCP_IMAGE_FAMILY
    images = client.images()
    image = (
        images.get(project=image_project, image=image_name)
        if image_name else images.getFromFamily(project=image_project, family=image_family)
    ).execute()
    if image.get("deprecated", {}).get("state") in {"DEPRECATED", "OBSOLETE", "DELETED"}:
        raise ValueError(f"GCP image {image['name']} is deprecated")
    if image.get("status") != "READY":
        raise ValueError(f"GCP image {image['name']} is not READY")
    zones = []
    page = client.zones().list(project=project)
    while page is not None:
        response = page.execute()
        zones.extend(response.get("items", []))
        page = client.zones().list_next(previous_request=page, previous_response=response)
    errors = []
    eligible = {}
    default_machine = request.get("worker_machine_type") or settings.GCP_MACHINE_TYPE
    for region in request["regions"]:
        machine = (request.get("worker_machine_types_by_region") or {}).get(region, default_machine)
        candidates = [z["name"] for z in zones if z.get("status") == "UP" and z["region"].rsplit("/", 1)[-1] == region]
        failures = []
        eligible[region] = []
        for zone in candidates:
            try:
                client.machineTypes().get(project=project, zone=zone, machineType=machine).execute()
                eligible[region].append(zone)
            except Exception as error:
                failures.append(f"{zone}: {error}")
        if not eligible[region]:
            errors.append(f"{region}: no UP zone supports {machine}: {'; '.join(failures)}")
    return {"errors": errors, "worker_image_project": image_project,
            "worker_image": image["name"], "eligible_zones": eligible,
            "eligible_regions": [region for region in request["regions"] if eligible[region]]}


def azure_region_errors(region: str, size: str, providers: dict[str, Any], skus: list[Any]) -> list[str]:
    errors = []
    normalize = lambda value: value.replace(" ", "").lower()
    required = {"Microsoft.Network": ("publicIPAddresses", "virtualNetworks", "networkInterfaces", "networkSecurityGroups"),
                "Microsoft.Compute": ("virtualMachines", "disks")}
    for namespace, resource_types in required.items():
        provider = providers[namespace]
        if provider.registration_state != "Registered":
            errors.append(f"{region}: {namespace} is not registered")
        for resource_type in resource_types:
            matching = [r for r in provider.resource_types if r.resource_type.lower() == resource_type.lower()]
            if not matching or not any(normalize(location) == normalize(region) for r in matching for location in (r.locations or [])):
                errors.append(f"{region}: {namespace}/{resource_type} is unavailable")
    candidates = [sku for sku in skus if sku.resource_type == "virtualMachines" and sku.name == size and any(normalize(location) == normalize(region) for location in (sku.locations or []))]
    # Workers are regional (no explicit Azure zone). A zone-only restriction
    # does not prohibit a non-zonal VM; location restrictions do.
    def blocks_region(restriction: Any) -> bool:
        # Recent Azure SDK models implement Mapping: .values is a method,
        # not the JSON "values" field.
        kind = restriction.get("type") if isinstance(restriction, Mapping) else restriction.type
        values = restriction.get("values") if isinstance(restriction, Mapping) else restriction.values
        return str(getattr(kind, "value", kind)).lower() == "location" and (
            not values or any(normalize(value) == normalize(region) for value in values)
        )

    usable = [sku for sku in candidates if not any(blocks_region(r) for r in (sku.restrictions or []))]
    if not usable:
        errors.append(f"{region}: VM size {size} is unavailable or restricted for this subscription")
    return errors


def check_azure(request: dict[str, Any]) -> dict[str, Any]:
    from azure.mgmt.compute import ComputeManagementClient
    from providers.azure import driver
    from providers import settings

    resource = driver.get_resource_client()
    providers = {name: resource.providers.get(name) for name in ("Microsoft.Network", "Microsoft.Compute")}
    compute = ComputeManagementClient(driver.get_azure_credential(), driver.get_subscription_id(),
                                      connection_timeout=15, read_timeout=30, retry_total=2)
    default_size = request.get("worker_machine_type") or settings.AZR_VM_SIZE
    overrides = request.get("worker_machine_types_by_region") or {}
    errors = []
    versions = {}
    for region in request["regions"]:
        size = overrides.get(region, default_size)
        # The global SKU catalog can exhaust a small controller's memory.
        # Scope API paging to one region and retain only the requested VM size.
        skus = [sku for sku in compute.resource_skus.list(filter=f"location eq '{region}'")
                if sku.resource_type == "virtualMachines" and sku.name == size]
        regional = azure_region_errors(region, size, providers, skus)
        if not regional:
            try:
                version = (request.get("worker_image_versions_by_region") or {}).get(region, settings.AZR_IMAGE_VERSION)
                if version == "latest":
                    candidates = compute.virtual_machine_images.list(
                        region, settings.AZR_IMAGE_PUBLISHER, settings.AZR_IMAGE_OFFER, settings.AZR_IMAGE_SKU)
                    if not candidates:
                        raise ValueError("no worker image versions are available")
                    version = max((image.name for image in candidates),
                                  key=lambda name: tuple(int(part) for part in name.split(".")))
                compute.virtual_machine_images.get(
                    region, settings.AZR_IMAGE_PUBLISHER, settings.AZR_IMAGE_OFFER,
                    settings.AZR_IMAGE_SKU, version)
                versions[region] = version
            except Exception as error:
                regional.append(f"{region}: worker image unavailable: {error}")
        errors.extend(regional)
    return {"errors": errors, "image_versions": versions,
            "eligible_regions": [region for region in request["regions"] if region in versions]}


def check_aws(request: dict[str, Any]) -> dict[str, Any]:
    from controller.aws_setup import aws_readiness_errors

    candidates = tuple(request["worker_machine_type"].split(",")) if request.get("worker_machine_type") else None
    errors = aws_readiness_errors(request["regions"], instance_types=candidates)
    return {"errors": errors,
            "eligible_regions": eligible_regions(request["regions"], errors)}


def main() -> int:
    request = json.load(sys.stdin)
    # Isolate a pathological SDK response from the persistent controller.
    if sys.platform == "linux":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (1024 ** 3, 1024 ** 3))
    try:
        report = {"gcp": check_gcp, "aws": check_aws, "azure": check_azure}[request["provider"]](request)
    except Exception as error:
        report = {"errors": [f"{type(error).__name__}: {error}"]}
    report.update(ready=not report["errors"], checked_at=datetime.now(timezone.utc).isoformat())
    print(json.dumps(report))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
