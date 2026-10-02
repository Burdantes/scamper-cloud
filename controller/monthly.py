"""Fail-closed monthly multi-cloud campaign scheduling on the controller."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import shutil
import subprocess
import tempfile
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from controller import submit
from controller.aws_setup import _environment_errors
from controller.target_registry import (
    RegisteredTarget,
    load_registered_target,
    register_local_target,
    remote_target_dir,
    remote_target_path,
    target_id,
)
from providers import DRIVER_MODULES
from providers.preflight import missing_worker_assets

CONFIG_PATH = Path("/etc/scamper-controller-monthly.json")
STATE_ROOT = Path("/var/lib/scamper-controller/monthly")
DEFAULT_DO_NOT_PROBE = Path("/opt/scamper-cloud/current/config/do-not-probe.txt")
SCHEMA_VERSION = 2
TRACE_PROBES_PER_TARGET = 40
RR_PROBES_PER_TARGET = 1
WORKLOAD_SAFETY_FACTOR = 1.25
WORKLOAD_FIXED_OVERHEAD_SECONDS = 900


@dataclass(frozen=True)
class ProviderSchedule:
    provider: str
    regions: tuple[str, ...]
    worker_machine_type: str | None
    max_instances: int
    max_targets: int | None
    max_trace6_targets: int | None
    campaign_timeout_seconds: int
    worker_image_project: str | None = None
    worker_image_family: str | None = None
    worker_image: str | None = None
    worker_machine_types_by_region: dict[str, str] = field(default_factory=dict)
    worker_image_versions_by_region: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MonthlySchedule:
    enabled: bool
    trace_target_id: str
    rr_target_id: str
    trace6_target_id: str | None
    bucket: str
    measurements: tuple[str, ...]
    trace_rate: int
    rr_rate: int
    trace6_rate: int
    rr_timeout: float
    probe_payload: str
    measurement_contact: str
    do_not_probe_file: Path
    providers: tuple[ProviderSchedule, ...]


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def load_schedule(path: Path = CONFIG_PATH) -> MonthlySchedule:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(
            f"monthly schedule configuration does not exist: {path}"
        ) from error
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid monthly schedule JSON in {path}: {error}") from error
    if not isinstance(raw, dict) or raw.get("schema_version") not in {
        1,
        SCHEMA_VERSION,
    }:
        raise ValueError(
            f"monthly schedule must use schema_version 1 or {SCHEMA_VERSION}"
        )

    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    try:
        trace_id = target_id(
            _nonempty_string(raw.get("trace_target_id"), "trace_target_id")
        )
        rr_id = target_id(_nonempty_string(raw.get("rr_target_id"), "rr_target_id"))
    except argparse.ArgumentTypeError as error:
        raise ValueError(str(error)) from error
    if trace_id == rr_id:
        raise ValueError("trace_target_id and rr_target_id must be distinct")

    measurements_raw = raw.get("measurements", ["trace", "rr"])
    if not isinstance(measurements_raw, list) or not measurements_raw:
        raise ValueError("measurements must be a non-empty list")
    measurements = tuple(
        _nonempty_string(value, "measurement") for value in measurements_raw
    )
    if len(set(measurements)) != len(measurements):
        raise ValueError("measurements must not contain duplicates")
    unsupported = set(measurements) - {"trace", "trace6", "rr"}
    if unsupported:
        raise ValueError(f"unsupported measurements: {', '.join(sorted(unsupported))}")
    trace6_id = None
    if raw.get("trace6_target_id") is not None:
        try:
            trace6_id = target_id(
                _nonempty_string(raw["trace6_target_id"], "trace6_target_id")
            )
        except argparse.ArgumentTypeError as error:
            raise ValueError(str(error)) from error
    if "trace6" in measurements and trace6_id is None:
        raise ValueError("trace6_target_id is required when trace6 is enabled")
    if len({value for value in (trace_id, rr_id, trace6_id) if value}) != (
        2 + (trace6_id is not None)
    ):
        raise ValueError("trace, trace6, and rr target IDs must be distinct")

    providers_raw = raw.get("providers")
    if not isinstance(providers_raw, dict):
        raise ValueError("providers must be an object")
    if set(providers_raw) != set(DRIVER_MODULES):
        missing = sorted(set(DRIVER_MODULES) - set(providers_raw))
        extra = sorted(set(providers_raw) - set(DRIVER_MODULES))
        raise ValueError(
            f"providers must exactly match supported providers; missing={missing}, extra={extra}"
        )

    providers: list[ProviderSchedule] = []
    for provider in sorted(providers_raw):
        value = providers_raw[provider]
        if not isinstance(value, dict):
            raise ValueError(f"providers.{provider} must be an object")
        regions_raw = value.get("regions")
        if regions_raw is None:
            regions: tuple[str, ...] = ()
        elif isinstance(regions_raw, list):
            regions = tuple(
                _nonempty_string(region, f"providers.{provider}.regions")
                for region in regions_raw
            )
            if not regions:
                raise ValueError(
                    f"providers.{provider}.regions must be null or a non-empty list"
                )
        else:
            raise ValueError(f"providers.{provider}.regions must be null or a list")
        machine_type = value.get("worker_machine_type")
        if machine_type is not None:
            machine_type = _nonempty_string(
                machine_type, f"providers.{provider}.worker_machine_type"
            )
        max_targets = value.get("max_targets")
        if max_targets is not None:
            max_targets = _positive_int(
                max_targets, f"providers.{provider}.max_targets"
            )
        max_trace6_targets = value.get("max_trace6_targets")
        if max_trace6_targets is not None:
            max_trace6_targets = _positive_int(
                max_trace6_targets, f"providers.{provider}.max_trace6_targets"
            )
        if len(set(regions)) != len(regions):
            raise ValueError(f"providers.{provider}.regions must not contain duplicates")
        if not regions or value.get("max_instances") != len(regions):
            raise ValueError(f"providers.{provider}.max_instances must equal the explicit region count")
        image_fields = {}
        for key in ("worker_image_project", "worker_image_family", "worker_image"):
            if value.get(key) is not None:
                if provider != "gcp":
                    raise ValueError(f"{key} is only supported for GCP")
                image_fields[key] = _nonempty_string(value[key], key)
        overrides = value.get("worker_machine_types_by_region", {})
        if not isinstance(overrides, dict) or (overrides and provider not in {"azure", "gcp"}):
            raise ValueError("worker_machine_types_by_region requires an Azure or GCP region-to-size object")
        for region, size in overrides.items():
            if region not in regions:
                raise ValueError(f"VM size override names unrequested region: {region}")
            _nonempty_string(size, f"worker size for {region}")
        image_versions = value.get("worker_image_versions_by_region", {})
        if not isinstance(image_versions, dict) or (image_versions and provider != "azure"):
            raise ValueError("worker_image_versions_by_region requires an Azure region-to-version object")
        for region, version in image_versions.items():
            if region not in regions:
                raise ValueError(f"image version override names unrequested region: {region}")
            _nonempty_string(version, f"image version for {region}")
        providers.append(
            ProviderSchedule(
                **image_fields,
                worker_machine_types_by_region=overrides,
                worker_image_versions_by_region=image_versions,
                provider=provider,
                regions=regions,
                worker_machine_type=machine_type,
                max_instances=_positive_int(
                    value.get("max_instances"), f"providers.{provider}.max_instances"
                ),
                max_targets=max_targets,
                max_trace6_targets=max_trace6_targets,
                campaign_timeout_seconds=_positive_int(
                    value.get(
                        "campaign_timeout_seconds",
                        submit.default_campaign_timeout_seconds(provider),
                    ),
                    f"providers.{provider}.campaign_timeout_seconds",
                ),
            )
        )

    probe_payload = _nonempty_string(raw.get("probe_payload"), "probe_payload")
    try:
        payload_bytes = probe_payload.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("probe_payload must contain ASCII text") from error
    if len(payload_bytes) > 128:
        raise ValueError("probe_payload must be at most 128 bytes")

    return MonthlySchedule(
        enabled=enabled,
        trace_target_id=trace_id,
        rr_target_id=rr_id,
        trace6_target_id=trace6_id,
        bucket=_nonempty_string(raw.get("bucket"), "bucket"),
        measurements=measurements,
        trace_rate=_positive_int(raw.get("trace_rate", 1000), "trace_rate"),
        rr_rate=_positive_int(raw.get("rr_rate", 1000), "rr_rate"),
        trace6_rate=_positive_int(raw.get("trace6_rate", 1000), "trace6_rate"),
        rr_timeout=float(raw.get("rr_timeout", 2.0)),
        probe_payload=probe_payload,
        measurement_contact=_nonempty_string(
            raw.get("measurement_contact"), "measurement_contact"
        ),
        do_not_probe_file=Path(raw.get("do_not_probe_file") or DEFAULT_DO_NOT_PROBE),
        providers=tuple(providers),
    )


def _credential_errors(provider: str, regions: tuple[str, ...] = ()) -> list[str]:
    if provider == "aws":
        return _environment_errors(os.environ)
    if provider == "azure":
        required = (
            "AZURE_TENANT_ID",
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
            "AZURE_SUBSCRIPTION_ID",
        )
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            return [f"Azure credential variables are missing: {', '.join(missing)}"]
    return []


def estimated_runtime_seconds(
    schedule: MonthlySchedule,
    provider: ProviderSchedule,
    target_counts: dict[str, int],
) -> int | None:
    if any(measurement not in target_counts for measurement in schedule.measurements):
        return None

    rates = {
        "trace": schedule.trace_rate,
        "trace6": schedule.trace6_rate,
        "rr": schedule.rr_rate,
    }
    probes_per_target = {
        "trace": TRACE_PROBES_PER_TARGET,
        "trace6": TRACE_PROBES_PER_TARGET,
        "rr": RR_PROBES_PER_TARGET,
    }
    packet_seconds = 0.0
    for measurement in schedule.measurements:
        cap = (
            provider.max_trace6_targets
            if measurement == "trace6"
            else provider.max_targets
        )
        count = target_counts[measurement]
        if cap is not None:
            count = min(count, cap)
        packet_seconds += count * probes_per_target[measurement] / rates[measurement]
    return math.ceil(
        packet_seconds * WORKLOAD_SAFETY_FACTOR + WORKLOAD_FIXED_OVERHEAD_SECONDS
    )


def _launch_readiness(provider: ProviderSchedule) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "controller.launch_check"],
            input=json.dumps(asdict(provider)), text=True, capture_output=True,
            timeout=600, check=False,
        )
        report = json.loads(result.stdout)
        if not isinstance(report, dict) or not isinstance(report.get("errors"), list):
            raise ValueError("invalid launch check result")
        if result.returncode and not report["errors"]:
            raise ValueError("launch checker exited unsuccessfully without an error")
        return report
    except (subprocess.TimeoutExpired, ValueError, OSError) as error:
        return {"ready": False, "errors": [f"launch validation failed: {error}"]}


def _launch_failures_and_warnings(
    launch: dict[str, Any],
) -> tuple[list[str], list[str]]:
    errors = list(launch.get("errors", []))
    if errors and launch.get("eligible_regions"):
        return [], errors
    return errors, []


def _eligible_provider(
    provider: ProviderSchedule, launch: dict[str, Any]
) -> ProviderSchedule:
    eligible = launch.get("eligible_regions")
    if eligible is None:
        return provider
    eligible_regions = tuple(region for region in provider.regions if region in eligible)
    return replace(
        provider,
        regions=eligible_regions,
        max_instances=len(eligible_regions),
        worker_machine_types_by_region={
            region: size
            for region, size in provider.worker_machine_types_by_region.items()
            if region in eligible_regions
        },
        worker_image_versions_by_region={
            region: version
            for region, version in provider.worker_image_versions_by_region.items()
            if region in eligible_regions
        },
    )


def readiness(schedule: MonthlySchedule) -> dict[str, Any]:
    errors: list[str] = []
    if not schedule.enabled:
        errors.append("monthly schedule is disabled")
    if schedule.rr_timeout <= 0:
        errors.append("rr_timeout must be greater than zero")
    if not schedule.do_not_probe_file.is_file():
        errors.append(f"do-not-probe file does not exist: {schedule.do_not_probe_file}")

    targets: dict[str, dict[str, Any]] = {}
    target_roles = [
        ("trace", schedule.trace_target_id),
        ("rr", schedule.rr_target_id),
    ]
    if schedule.trace6_target_id is not None:
        target_roles.append(("trace6", schedule.trace6_target_id))
    for role, registered_id in target_roles:
        path = remote_target_path(registered_id)
        registered = load_registered_target(path) if path.is_file() else None
        if registered is None:
            errors.append(f"{role} target ID is not registered: {registered_id}")
            continue
        expected_family = 6 if role == "trace6" else 4
        if registered.address_family != expected_family:
            errors.append(
                f"{role} target ID is IPv{registered.address_family}; "
                f"expected IPv{expected_family}: {registered_id}"
            )
        targets[role] = {
            "target_id": registered.target_id,
            "path": str(path),
            "target_count": registered.target_count,
            "normalized_sha256": registered.normalized_sha256,
            "address_family": registered.address_family,
        }

    providers: dict[str, dict[str, Any]] = {}
    target_counts = {
        role: int(target["target_count"]) for role, target in targets.items()
    }
    for provider in schedule.providers:
        provider_warnings: list[str] = []
        provider_errors = [
            f"missing worker asset {name}: {path}"
            for name, path in missing_worker_assets(provider.provider)
        ]
        provider_errors.extend(_credential_errors(provider.provider, provider.regions))
        launch = {"ready": False, "errors": []}
        if not provider_errors:
            launch = _launch_readiness(provider)
            launch_errors, launch_warnings = _launch_failures_and_warnings(launch)
            provider_errors.extend(launch_errors)
            provider_warnings.extend(launch_warnings)
        runtime_seconds = estimated_runtime_seconds(schedule, provider, target_counts)
        if (
            runtime_seconds is not None
            and runtime_seconds > provider.campaign_timeout_seconds
        ):
            provider_errors.append(
                "estimated workload runtime "
                f"{runtime_seconds}s exceeds campaign timeout "
                f"{provider.campaign_timeout_seconds}s"
            )
        providers[provider.provider] = {
            "ready": not provider_errors,
            "errors": provider_errors,
            "warnings": provider_warnings,
            "launch": launch,
            "regions": list(provider.regions) if provider.regions else "all",
            "max_instances": provider.max_instances,
            "max_targets": provider.max_targets,
            "max_trace6_targets": provider.max_trace6_targets,
            "estimated_runtime_seconds": runtime_seconds,
            "campaign_timeout_seconds": provider.campaign_timeout_seconds,
            "runtime_headroom_seconds": (
                provider.campaign_timeout_seconds - runtime_seconds
                if runtime_seconds is not None
                else None
            ),
        }
        errors.extend(f"{provider.provider}: {error}" for error in provider_errors)

    return {
        "ready": not errors,
        "errors": errors,
        "targets": targets,
        "providers": providers,
    }


def cycle_id(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y%m")


def cycle_label(value: str) -> str:
    """Validate a monthly or date-qualified campaign namespace."""
    if len(value) not in {6, 8} or not value.isdigit():
        raise ValueError("cycle must have the form YYYYMM or YYYYMMDD")
    return value


def _submission_args(
    schedule: MonthlySchedule,
    provider: ProviderSchedule,
    cycle: str,
) -> list[str]:
    run_id = f"monthly-{provider.provider}-{cycle}"
    arguments = [
        "--provider",
        provider.provider,
        "--run-id",
        run_id,
        "--trace-targets",
        str(remote_target_path(schedule.trace_target_id)),
        "--rr-targets",
        str(remote_target_path(schedule.rr_target_id)),
        "--bucket",
        schedule.bucket,
        "--object-prefix",
        f"runs/monthly/{cycle}/{provider.provider}",
        "--measurements",
        ",".join(schedule.measurements),
        "--max-instances",
        str(provider.max_instances),
        "--campaign-timeout-seconds",
        str(provider.campaign_timeout_seconds),
        "--wait-for-completion",
        "--trace-rate",
        str(schedule.trace_rate),
        "--rr-rate",
        str(schedule.rr_rate),
        "--trace6-rate",
        str(schedule.trace6_rate),
        "--rr-timeout",
        f"{schedule.rr_timeout:g}",
        "--probe-payload",
        schedule.probe_payload,
        "--measurement-contact",
        schedule.measurement_contact,
        "--do-not-probe-file",
        str(schedule.do_not_probe_file),
    ]
    if schedule.trace6_target_id is not None:
        arguments.extend(
            ["--trace6-targets", str(remote_target_path(schedule.trace6_target_id))]
        )
    if provider.regions:
        arguments.extend(["--regions", ",".join(provider.regions)])
    if provider.worker_machine_type:
        arguments.extend(["--worker-machine-type", provider.worker_machine_type])
    for field_name in ("worker_image_project", "worker_image_family", "worker_image"):
        if getattr(provider, field_name):
            arguments.extend(["--" + field_name.replace("_", "-"), getattr(provider, field_name)])
    if provider.worker_machine_types_by_region:
        arguments.extend(["--worker-machine-types-json", json.dumps(provider.worker_machine_types_by_region)])
    if provider.worker_image_versions_by_region:
        arguments.extend(["--worker-image-versions-json", json.dumps(provider.worker_image_versions_by_region)])
    if provider.max_targets:
        arguments.extend(["--max-targets", str(provider.max_targets)])
    if provider.max_trace6_targets:
        arguments.extend(["--max-trace6-targets", str(provider.max_trace6_targets)])
    return arguments


def dispatch(schedule: MonthlySchedule, *, cycle: str | None = None) -> dict[str, Any]:
    report = readiness(schedule)
    if not report["ready"]:
        raise RuntimeError(
            "monthly schedule is not ready:\n" + "\n".join(report["errors"])
        )

    selected_cycle = cycle_label(cycle or cycle_id())
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_ROOT / "dispatch.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another monthly dispatch is already running") from error

        state_path = STATE_ROOT / f"{selected_cycle}.json"
        previous = json.loads(state_path.read_text()) if state_path.is_file() else {}
        previous_results = {item["run_id"]: item for item in previous.get("results", [])}
        results: list[dict[str, Any]] = []
        state = {"schema_version": 1, "cycle": selected_cycle,
                 "dispatched_at": datetime.now(timezone.utc).isoformat(),
                 "complete": False, "results": results}

        def record(result: dict[str, Any]) -> None:
            results.append(result)
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            temporary.replace(state_path)

        for provider in schedule.providers:
            run_id = f"monthly-{provider.provider}-{selected_cycle}"
            job_spec = submit.STATE_ROOT / "jobs" / run_id / "job.json"
            if job_spec.is_file():
                record(
                    {
                        "provider": provider.provider,
                        "run_id": run_id,
                        "status": "already-submitted",
                        "previous_status": previous_results.get(run_id, {}).get("previous_status") or previous_results.get(run_id, {}).get("status"),
                    }
                )
                continue
            # A prior provider can run for days. Resolve mutable dependencies
            # again immediately before this provider, and pin the GCP image.
            launch = _launch_readiness(provider)
            launch_errors, launch_warnings = _launch_failures_and_warnings(launch)
            if launch_errors:
                record({"provider": provider.provider, "run_id": run_id,
                                "status": "failed", "errors": launch_errors})
                continue
            effective = _eligible_provider(provider, launch)
            if provider.provider == "gcp" and launch.get("worker_image"):
                effective = replace(provider, worker_image=launch["worker_image"],
                                    worker_image_project=launch["worker_image_project"])
            if provider.provider == "azure" and launch.get("image_versions"):
                effective = replace(provider, worker_image_versions_by_region=launch["image_versions"])
            try:
                return_code = submit.main(_submission_args(schedule, effective, selected_cycle))
                if return_code:
                    raise subprocess.CalledProcessError(return_code, ["controller.submit"])
            except subprocess.CalledProcessError as error:
                record(
                    {
                        "provider": provider.provider,
                        "run_id": run_id,
                        "status": "failed",
                        "exit_code": error.returncode,
                    }
                )
                continue
            result = {"provider": provider.provider, "run_id": run_id,
                      "status": "completed"}
            if launch_warnings:
                result["warnings"] = launch_warnings
            record(result)

        state = {
            "schema_version": 1,
            "cycle": selected_cycle,
            "dispatched_at": datetime.now(timezone.utc).isoformat(),
            "complete": all(result["status"] == "completed" or
                            (result["status"] == "already-submitted" and result.get("previous_status") == "completed")
                            for result in results),
            "results": results,
        }
        state_path = STATE_ROOT / f"{selected_cycle}.json"
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        temporary.replace(state_path)
        return state


def register_controller_target(source: Path) -> RegisteredTarget:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="target-registration-", dir=STATE_ROOT
    ) as temp_dir:
        registration = register_local_target(source, Path(temp_dir))
        destination = remote_target_dir(registration.target_id)
        target_path = remote_target_path(registration.target_id)
        if target_path.is_file():
            existing = load_registered_target(target_path)
            if existing is None:
                raise ValueError(f"invalid existing target registration: {destination}")
            return existing
        destination.mkdir(parents=True, exist_ok=False)
        shutil.copy2(registration.targets_path, target_path)
        shutil.copy2(registration.manifest_path, destination / "manifest.json")
        return load_registered_target(target_path) or registration


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage monthly multi-cloud campaigns."
    )
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("check")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--cycle")
    register_parser = subparsers.add_parser("register-targets")
    register_parser.add_argument("--trace-targets", type=Path)
    register_parser.add_argument("--rr-targets", type=Path)
    register_parser.add_argument("--trace6-targets", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "register-targets":
        sources = {
            role: getattr(args, f"{role}_targets")
            for role in ("trace", "rr", "trace6")
            if getattr(args, f"{role}_targets") is not None
        }
        if not sources:
            raise ValueError("at least one target source is required")
        registrations = {
            role: register_controller_target(source) for role, source in sources.items()
        }
        trace6 = registrations.get("trace6")
        if trace6 is not None:
            if trace6.address_family != 6:
                raise ValueError("--trace6-targets must contain IPv6 destinations")
        values = {
            f"{role}_target_id": registration.target_id
            for role, registration in registrations.items()
        }
        print(json.dumps(values, indent=2))
        return 0
    schedule = load_schedule(args.config)
    if args.action == "check":
        report = readiness(schedule)
        print(json.dumps(report, indent=2))
        return 0 if report["ready"] else 1
    state = dispatch(schedule, cycle=args.cycle)
    print(json.dumps(state, indent=2))
    return 0 if state["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
