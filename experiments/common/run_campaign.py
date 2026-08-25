#!/usr/bin/env python3
"""Run IPv4/IPv6 traceroute and IPv4 Record Route on one probe node."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import random
import re
import secrets
import shlex
import socket
import subprocess
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MEASUREMENT_COMMANDS = {
    "trace": "trace -m 20 -g 8 -w 3 -q 2 -P ICMP{payload_option}",
    "trace6": "trace -m 20 -g 8 -w 3 -q 2 -P ICMP{payload_option}",
    "rr": "ping -P icmp-echo -R -c 1 -W {timeout}{payload_option}",
}
MEASUREMENT_FAMILIES = {"trace": 4, "trace6": 6, "rr": 4}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def artifact_path(output_prefix: Path, suffix: str) -> Path:
    return Path(f"{output_prefix}.{suffix}")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def sha256_hex(value: str) -> str:
    if SHA256_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "SHA-256 must contain 64 lowercase hexadecimal digits"
        )
    return value


def probe_payload_text(value: str) -> str:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise argparse.ArgumentTypeError(
            "probe payload must contain ASCII text"
        ) from error
    if not encoded:
        raise argparse.ArgumentTypeError("probe payload must not be empty")
    if len(encoded) > 128:
        raise argparse.ArgumentTypeError("probe payload must be at most 128 bytes")
    return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_and_count_targets(path: Path, *, expected_family: int = 4) -> int:
    target_count = 0
    with path.open("r", encoding="utf-8") as source:
        for line_number, raw_line in enumerate(source, start=1):
            value = raw_line.strip()
            if not value:
                raise ValueError(f"blank target on line {line_number}")
            try:
                address = ipaddress.ip_address(value)
            except ValueError as error:
                raise ValueError(
                    f"invalid IP target on line {line_number}: {value!r}"
                ) from error
            if address.version != expected_family:
                raise ValueError(
                    f"non-IPv{expected_family} target on line {line_number}: {value!r}"
                )
            if value != str(address):
                raise ValueError(
                    f"non-canonical IPv{expected_family} target on line "
                    f"{line_number}: {value!r}"
                )
            target_count += 1
    if target_count == 0:
        raise ValueError(f"target file is empty: {path}")
    return target_count


def verify_target_contract(
    path: Path,
    *,
    expected_count: int | None,
    expected_sha256: str | None,
    expected_family: int = 4,
) -> tuple[int, str, str]:
    if (expected_count is None) != (expected_sha256 is None):
        raise ValueError(
            "registered target count and SHA-256 must be provided together"
        )
    if expected_count is None or expected_sha256 is None:
        target_count = validate_and_count_targets(
            path, expected_family=expected_family
        )
        return target_count, sha256_file(path), f"strict-ipv{expected_family}-parse"

    actual_sha256 = sha256_file(path)
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"registered target SHA-256 mismatch for {path}: "
            f"{actual_sha256} != {expected_sha256}"
        )
    return expected_count, actual_sha256, "registered-sha256"


def shuffle_targets(
    source: Path,
    destination: Path,
    *,
    memory_limit: str = "128M",
) -> tuple[int, list[str]]:
    seed = secrets.randbits(64)
    random_source = artifact_path(destination, "random-source.tmp")
    random_source.write_bytes(random.Random(seed).randbytes(1024 * 1024))
    command = [
        "sort",
        "--random-sort",
        f"--random-source={random_source}",
        f"--buffer-size={memory_limit}",
        f"--temporary-directory={destination.parent}",
        f"--output={destination}",
        str(source),
    ]
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    try:
        completed = subprocess.run(command, check=False, env=environment)
    finally:
        random_source.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"external target shuffle failed with return code {completed.returncode}"
        )
    return seed, command


def flags_for(record: dict[str, Any]) -> set[str]:
    flags = record.get("flags", [])
    if isinstance(flags, str):
        return {
            part.strip() for part in flags.replace(",", " ").split() if part.strip()
        }
    if isinstance(flags, list):
        return {str(flag) for flag in flags}
    return set()


def summarize_json_lines(lines: Iterable[str], measurement: str) -> dict[str, Any]:
    record_types: Counter[str] = Counter()
    destination_count = 0
    rr_requested_records = 0
    rr_responses_with_data = 0
    destination_families: Counter[str] = Counter()
    first_record: dict[str, Any] | None = None
    sample: dict[str, Any] | None = None

    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON on decoded line {line_number}") from error
        if first_record is None:
            first_record = record
        if sample is None and record.get("dst"):
            sample = record
        record_type = str(record.get("type", "unknown"))
        record_types[record_type] += 1
        destination = record.get("dst")
        if destination:
            destination_count += 1
            try:
                destination_families[f"ipv{ipaddress.ip_address(destination).version}"] += 1
            except ValueError:
                destination_families["invalid"] += 1
        if record_type == "ping" and "v4rr" in flags_for(record):
            rr_requested_records += 1
        for response in record.get("responses", []) or []:
            if isinstance(response, dict) and response.get("RR"):
                rr_responses_with_data += 1

    return {
        "measurement": measurement,
        "record_count": sum(record_types.values()),
        "destination_count": destination_count,
        "record_types": dict(sorted(record_types.items())),
        "destination_address_families": dict(sorted(destination_families.items())),
        "record_route_requested_records": rr_requested_records,
        "record_route_responses_with_data": rr_responses_with_data,
        "sample": sample or first_record,
    }


def convert_and_summarize(
    warts_path: Path, measurement: str, stderr_path: Path
) -> tuple[int, str, dict[str, Any] | None]:
    converter_command = ["sc_warts2json", str(warts_path)]
    try:
        with stderr_path.open("w+", encoding="utf-8") as converter_stderr:
            process = subprocess.Popen(
                converter_command,
                stdout=subprocess.PIPE,
                stderr=converter_stderr,
                text=True,
            )
            if process.stdout is None:
                raise RuntimeError("converter stdout pipe was not created")
            parse_error: ValueError | None = None
            try:
                summary = summarize_json_lines(process.stdout, measurement)
            except ValueError as error:
                parse_error = error
                summary = None
            process.stdout.close()
            return_code = process.wait()
            converter_stderr.seek(0)
            stderr = converter_stderr.read().strip()
    except FileNotFoundError:
        return 127, "sc_warts2json was not found", None
    finally:
        stderr_path.unlink(missing_ok=True)
    if return_code == 0 and parse_error is not None:
        raise parse_error
    return return_code, stderr, summary if return_code == 0 else None


def measurement_command(
    measurement: str,
    *,
    target_file: Path,
    warts_file: Path,
    rate_pps: int,
    rr_timeout_seconds: float,
    payload_text: str | None,
) -> list[str]:
    inner = MEASUREMENT_COMMANDS[measurement]
    payload_hex = payload_text.encode("ascii").hex() if payload_text else None
    payload_flag = "-B" if measurement == "rr" else "-p"
    payload_option = f" {payload_flag} {payload_hex}" if payload_hex else ""
    inner = inner.format(
        timeout=f"{rr_timeout_seconds:g}",
        payload_option=payload_option,
    )
    return [
        "scamper",
        "-c",
        inner,
        "-p",
        str(rate_pps),
        "-f",
        str(target_file),
        "-o",
        str(warts_file),
        "-O",
        "warts",
    ]


def run_measurement(
    measurement: str,
    *,
    target_file: Path,
    target_count: int,
    output_prefix: Path,
    rate_pps: int,
    rr_timeout_seconds: float,
    payload_text: str | None,
    target_source: str,
    target_version: str,
    normalized_target_file: Path,
    normalized_target_sha256: str,
    target_validation: str,
    common_metadata: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    shuffled_path = artifact_path(output_prefix, f"{measurement}.targets.txt")
    warts_path = artifact_path(output_prefix, f"{measurement}.warts")
    metadata_path = artifact_path(output_prefix, f"{measurement}.metadata.json")
    converter_stderr_path = artifact_path(
        output_prefix, f"{measurement}.converter-stderr.tmp"
    )
    seed, shuffle_command = shuffle_targets(target_file, shuffled_path)

    command = measurement_command(
        measurement,
        target_file=shuffled_path,
        warts_file=warts_path,
        rate_pps=rate_pps,
        rr_timeout_seconds=rr_timeout_seconds,
        payload_text=payload_text,
    )
    print(f"SCAMPER_COMMAND[{measurement}]={shlex.join(command)}", flush=True)

    started_at = utc_now()
    completed = subprocess.run(command, check=False)
    finished_at = utc_now()

    converter_command = ["sc_warts2json", str(warts_path)]
    converter_return_code: int | None = None
    converter_stderr = ""
    parsed_summary: dict[str, Any] | None = None
    if warts_path.is_file() and warts_path.stat().st_size > 0:
        (
            converter_return_code,
            converter_stderr,
            parsed_summary,
        ) = convert_and_summarize(warts_path, measurement, converter_stderr_path)

    return_code = completed.returncode or converter_return_code or 0
    if measurement == "rr" and parsed_summary is not None:
        if parsed_summary["record_route_requested_records"] == 0:
            return_code = return_code or 65
            converter_stderr = (
                converter_stderr + "; " if converter_stderr else ""
            ) + "decoded RR results do not contain the v4rr flag"
    if parsed_summary is not None:
        decoded_destinations = parsed_summary["destination_count"]
        if decoded_destinations != target_count:
            return_code = return_code or 66
            converter_stderr = (converter_stderr + "; " if converter_stderr else "") + (
                "decoded destination count does not match target count: "
                f"{decoded_destinations} != {target_count}"
            )
        expected_family = f"ipv{MEASUREMENT_FAMILIES[measurement]}"
        unexpected_families = set(
            parsed_summary.get("destination_address_families", {})
        ) - {expected_family}
        if unexpected_families:
            return_code = return_code or 67
            converter_stderr = (converter_stderr + "; " if converter_stderr else "") + (
                "decoded destinations contain unexpected address families: "
                + ", ".join(sorted(unexpected_families))
            )

    metadata = {
        **common_metadata,
        "measurement": measurement,
        "address_family": MEASUREMENT_FAMILIES[measurement],
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": (finished_at - started_at).total_seconds(),
        "target_source": target_source,
        "target_version": target_version,
        "normalized_target_file": str(normalized_target_file),
        "normalized_target_sha256": normalized_target_sha256,
        "target_count": target_count,
        "target_validation": target_validation,
        "shuffle_seed": seed,
        "shuffle_method": "gnu-sort-random-external",
        "shuffle_memory_limit": "128M",
        "shuffle_command": shuffle_command,
        "shuffled_target_file": str(shuffled_path),
        "shuffled_target_sha256": sha256_file(shuffled_path),
        "rate_pps": rate_pps,
        "timeout_seconds": rr_timeout_seconds if measurement == "rr" else 3,
        "probe_count_per_destination": 1 if measurement == "rr" else None,
        "retry_behavior": "none configured",
        "probe_payload_text": payload_text,
        "probe_payload_hex": payload_text.encode("ascii").hex()
        if payload_text
        else None,
        "record_route_requested": measurement == "rr" and "-R" in command[2].split(),
        "command": command,
        "command_shell": shlex.join(command),
        "warts_file": str(warts_path),
        "decoded_file": None,
        "decoded_output_retained": False,
        "scamper_return_code": completed.returncode,
        "converter_command": converter_command,
        "converter_return_code": converter_return_code,
        "converter_stderr": converter_stderr,
        "parsed_summary": parsed_summary,
        "return_code": return_code,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return int(return_code), metadata


def checkpoint_measurement_artifacts(
    command_template: list[str],
    *,
    measurement: str,
    output_prefix: Path,
) -> None:
    artifacts = (
        artifact_path(output_prefix, f"{measurement}.warts"),
        artifact_path(output_prefix, f"{measurement}.metadata.json"),
        artifact_path(output_prefix, f"{measurement}.targets.txt"),
    )
    for artifact in artifacts:
        if not artifact.is_file() or artifact.stat().st_size == 0:
            raise RuntimeError(
                f"cannot checkpoint missing or empty {measurement} artifact: {artifact}"
            )
        values = {
            "artifact": str(artifact),
            "artifact_name": artifact.name,
            "measurement": measurement,
            "output_prefix": str(output_prefix),
        }
        command = [argument.format_map(values) for argument in command_template]
        print(
            f"CHECKPOINT_COMMAND[{measurement}]={shlex.join(command)}",
            flush=True,
        )
        subprocess.run(command, check=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=Path)
    parser.add_argument("--trace-targets", type=Path)
    parser.add_argument("--rr-targets", type=Path)
    parser.add_argument("--trace6-targets", type=Path)
    parser.add_argument("--trace-target-count", type=positive_int)
    parser.add_argument("--rr-target-count", type=positive_int)
    parser.add_argument("--trace6-target-count", type=positive_int)
    parser.add_argument("--trace-target-sha256", type=sha256_hex)
    parser.add_argument("--rr-target-sha256", type=sha256_hex)
    parser.add_argument("--trace6-target-sha256", type=sha256_hex)
    parser.add_argument("--output-prefix", type=Path, required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--target-source")
    parser.add_argument("--target-version")
    parser.add_argument("--trace-target-source")
    parser.add_argument("--trace-target-version")
    parser.add_argument("--rr-target-source")
    parser.add_argument("--rr-target-version")
    parser.add_argument("--trace6-target-source")
    parser.add_argument("--trace6-target-version")
    parser.add_argument("--trace-rate", type=positive_int, default=100)
    parser.add_argument("--rr-rate", type=positive_int, default=10)
    parser.add_argument("--trace6-rate", type=positive_int, default=100)
    parser.add_argument("--rr-timeout", type=positive_float, default=2.0)
    parser.add_argument("--probe-payload", type=probe_payload_text)
    parser.add_argument("--measurement-contact")
    parser.add_argument("--do-not-probe-version")
    parser.add_argument("--measurements", default="trace,rr")
    parser.add_argument(
        "--checkpoint-command",
        nargs=argparse.REMAINDER,
        help=(
            "command run once per completed measurement artifact; supports "
            "{artifact}, {artifact_name}, {measurement}, and {output_prefix}"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    measurements = tuple(part.strip() for part in args.measurements.split(","))
    if not all(measurements) or len(set(measurements)) != len(measurements):
        parser.error("--measurements must be a non-empty comma-separated unique list")
    unsupported = set(measurements) - set(MEASUREMENT_COMMANDS)
    if unsupported:
        parser.error("unsupported measurements: " + ", ".join(sorted(unsupported)))
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    campaign_started = utc_now()
    common_metadata = {
        "provider": args.provider,
        "region": args.region,
        "node": args.node,
        "hostname": socket.gethostname(),
        "campaign_started_at": campaign_started.isoformat(),
        "measurement_contact": args.measurement_contact,
        "do_not_probe_version": args.do_not_probe_version,
    }

    statuses: dict[str, int] = {}
    metadata: dict[str, Any] = {}
    target_sets: dict[str, dict[str, Any]] = {}
    for measurement in measurements:
        target_path = getattr(args, f"{measurement}_targets") or args.targets
        target_source = (
            getattr(args, f"{measurement}_target_source") or args.target_source
        )
        target_version = (
            getattr(args, f"{measurement}_target_version") or args.target_version
        )
        if target_path is None:
            parser.error(
                f"--{measurement}-targets or the legacy --targets option is required"
            )
        if not target_source:
            parser.error(
                f"--{measurement}-target-source or the legacy --target-source option is required"
            )
        if not target_version:
            parser.error(
                f"--{measurement}-target-version or the legacy --target-version option is required"
            )
        expected_count = getattr(args, f"{measurement}_target_count")
        expected_sha256 = getattr(args, f"{measurement}_target_sha256")
        target_count, normalized_sha256, target_validation = verify_target_contract(
            target_path,
            expected_count=expected_count,
            expected_sha256=expected_sha256,
            expected_family=MEASUREMENT_FAMILIES[measurement],
        )
        rate = {
            "trace": args.trace_rate,
            "trace6": args.trace6_rate,
            "rr": args.rr_rate,
        }[measurement]
        status, result_metadata = run_measurement(
            measurement,
            target_file=target_path,
            target_count=target_count,
            output_prefix=args.output_prefix,
            rate_pps=rate,
            rr_timeout_seconds=args.rr_timeout,
            payload_text=args.probe_payload,
            target_source=target_source,
            target_version=target_version,
            normalized_target_file=target_path,
            normalized_target_sha256=normalized_sha256,
            target_validation=target_validation,
            common_metadata=common_metadata,
        )
        statuses[measurement] = status
        metadata[measurement] = result_metadata
        target_sets[measurement] = {
            "source": target_source,
            "version": target_version,
            "normalized_file": str(target_path),
            "normalized_sha256": normalized_sha256,
            "target_count": target_count,
            "address_family": MEASUREMENT_FAMILIES[measurement],
        }
        if args.checkpoint_command:
            checkpoint_measurement_artifacts(
                args.checkpoint_command,
                measurement=measurement,
                output_prefix=args.output_prefix,
            )

    campaign_status = {
        **common_metadata,
        "campaign_finished_at": utc_now().isoformat(),
        "measurements": list(statuses),
        "measurement_return_codes": statuses,
        "complete": all(status == 0 for status in statuses.values()),
        "target_sets": target_sets,
        "metadata_files": {
            name: str(artifact_path(args.output_prefix, f"{name}.metadata.json"))
            for name in statuses
        },
    }
    status_path = artifact_path(args.output_prefix, "status.json")
    status_path.write_text(
        json.dumps(campaign_status, indent=2) + "\n", encoding="utf-8"
    )
    return max(statuses.values(), default=0)


if __name__ == "__main__":
    raise SystemExit(main())
