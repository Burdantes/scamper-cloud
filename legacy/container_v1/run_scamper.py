#!/usr/bin/env python3
from __future__ import annotations

import json
import ipaddress
import os
import re
import shlex
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path


MANAGED_FLAGS = {"-f", "-o", "-O"}


def safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return cleaned or "unknown"


def main() -> int:
    target_file = Path(os.environ.get("TARGET_FILE", "/experiment/targets.txt"))
    output_dir = Path(os.environ.get("OUTPUT_DIR", "/results"))
    scamper_args = shlex.split(os.environ.get("SCAMPER_ARGS", ""))

    managed = MANAGED_FLAGS.intersection(scamper_args)
    if managed:
        flags = ", ".join(sorted(managed))
        raise SystemExit(f"SCAMPER_ARGS cannot override managed flags: {flags}")
    if not target_file.is_file():
        raise SystemExit(f"target file does not exist: {target_file}")

    output_dir.mkdir(parents=True, exist_ok=True)
    probe_name = safe_component(os.environ.get("PROBE_NAME", socket.gethostname()))
    probe_ip_value = os.environ.get("PROBE_IP", "").strip()
    probe_ip = None
    if probe_ip_value:
        parsed_ip = ipaddress.ip_address(probe_ip_value)
        if parsed_ip.version != 4:
            raise SystemExit("PROBE_IP must be an IPv4 address")
        probe_ip = str(parsed_ip)
    experiment = safe_component(os.environ.get("EXPERIMENT_NAME", "experiment"))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    identity = f"{probe_name}-{probe_ip}" if probe_ip else probe_name
    stem = f"{experiment}-{identity}-{timestamp}"
    warts_path = output_dir / f"{stem}.warts"
    traces_path = output_dir / f"{stem}.traces.jsonl"
    traces_temporary_path = output_dir / f"{stem}.traces.jsonl.tmp"
    metadata_path = output_dir / f"{stem}.metadata.json"

    command = [
        "scamper",
        *scamper_args,
        "-f",
        str(target_file),
        "-o",
        str(warts_path),
        "-O",
        "warts",
    ]
    started_at = datetime.now(timezone.utc)
    completed = subprocess.run(command, check=False)
    finished_at = datetime.now(timezone.utc)

    converter_command = ["sc_warts2json", str(warts_path)]
    converter_return_code: int | None = None
    converter_stderr = ""
    if completed.returncode == 0:
        try:
            with traces_temporary_path.open("w", encoding="utf-8") as traces_file:
                converted = subprocess.run(
                    converter_command,
                    check=False,
                    stdout=traces_file,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            converter_return_code = converted.returncode
            converter_stderr = converted.stderr.strip()
            if converter_return_code == 0:
                traces_temporary_path.replace(traces_path)
            else:
                traces_temporary_path.unlink(missing_ok=True)
        except FileNotFoundError:
            converter_return_code = 127
            converter_stderr = "sc_warts2json was not found in the container"

    metadata = {
        "experiment": experiment,
        "probe": probe_name,
        "probe_ip": probe_ip,
        "hostname": socket.gethostname(),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": (finished_at - started_at).total_seconds(),
        "command": command,
        "target_file": str(target_file),
        "warts_file": str(warts_path),
        "traces_file": str(traces_path) if traces_path.exists() else None,
        "traces_format": "json-lines",
        "scamper_return_code": completed.returncode,
        "converter_command": converter_command,
        "converter_return_code": converter_return_code,
        "converter_stderr": converter_stderr,
        "return_code": completed.returncode or converter_return_code or 0,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return int(metadata["return_code"])


if __name__ == "__main__":
    raise SystemExit(main())
