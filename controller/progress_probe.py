"""Measure how far each running campaign has actually got.

A campaign is silent between "scamper started" and "artifact uploaded", which
is most of its week. scamper reads its target file sequentially, so the read
offset in /proc/<pid>/fdinfo against that file's size is an exact position
rather than an estimate. This probes each worker of each running campaign over
SSH, one at a time, and writes progress.json into the job directory for the
dashboard to read. The dashboard never opens an SSH connection itself.

It also records each ended run's outcome (outcome.json, from the run's GCS
manifest), because the dashboard is read-only and the local logs are deleted
after upload.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

STATE_ROOT = Path("/var/lib/scamper-controller")
SSH_KEY = STATE_ROOT / "ssh/id_ed25519"
RUN_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
SSH_USERS = {"aws": "ubuntu", "azure": "azureuser", "gcp": "scamper-gcp"}
DEFAULT_BUDGET_SECONDS = 900
PER_WORKER_TIMEOUT = 25

REMOTE_SCRIPT = r"""
pid=$(pgrep -x scamper | head -1)
if [ -z "$pid" ]; then echo "NOSCAMPER"; exit 0; fi
for fd in $(sudo ls /proc/$pid/fd 2>/dev/null); do
  t=$(sudo readlink /proc/$pid/fd/$fd 2>/dev/null)
  case "$t" in
    *.targets.txt)
      p=$(sudo awk '/^pos/{print $2}' /proc/$pid/fdinfo/$fd 2>/dev/null)
      s=$(stat -c %s "$t" 2>/dev/null)
      [ -n "$p" ] && [ -n "$s" ] && echo "POS $t $p $s"
      ;;
  esac
done
for w in results/*.warts; do
  [ -f "$w" ] && echo "WARTS $w $(stat -c %s "$w")"
done
"""


def ssh_runner(user: str, address: str, script: str, timeout: int = PER_WORKER_TIMEOUT) -> str:
    command = [
        "ssh", "-i", str(SSH_KEY),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        f"{user}@{address}", script,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    return result.stdout


def measurement_of(path: str) -> str:
    match = re.search(r"\.([a-z0-9]+)\.targets\.txt$", path)
    return match.group(1) if match else "unknown"


def parse_probe(output: str) -> dict[str, Any]:
    """Turn the remote script's output into one worker's progress."""
    if "NOSCAMPER" in output:
        return {"state": "no-scamper"}
    if not output.strip():
        return {"state": "unreachable"}
    position = size = None
    measurement = "unknown"
    warts = 0
    for line in output.splitlines():
        fields = line.split()
        if fields[:1] == ["POS"] and len(fields) == 4:
            measurement = measurement_of(fields[1])
            position, size = int(fields[2]), int(fields[3])
        elif fields[:1] == ["WARTS"] and len(fields) == 3:
            warts += int(fields[2])
    if position is None or not size:
        return {"state": "unknown"}
    return {
        "state": "measuring",
        "measurement": measurement,
        "position": position,
        "size": size,
        "fraction": round(position / size, 6),
        "warts_bytes": warts,
    }


def workers_of(job_dir: Path, run_id: str) -> list[tuple[str, str]]:
    """(location, address) for each worker, from the per-instance log names."""
    found = []
    for path in sorted((job_dir / "logs").glob(f"{run_id}-*.log")):
        remainder = path.name[len(run_id) + 1:-len(".log")]
        location, _, address = remainder.rpartition("-")
        if location and address:
            found.append((location, address))
    return found


def unit_is_active(run_id: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", f"scamper-campaign-{run_id}.service"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() in {"active", "activating"}


def probe_run(run_id: str, root: Path = STATE_ROOT, runner: Callable[..., str] = ssh_runner,
              deadline: float | None = None) -> dict[str, Any]:
    job_dir = root / "jobs" / run_id
    try:
        job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        job = {}
    user = SSH_USERS.get(job.get("provider", ""), "ubuntu")
    workers = []
    for location, address in workers_of(job_dir, run_id):
        if deadline is not None and time.monotonic() >= deadline:
            workers.append({"location": location, "address": address, "state": "skipped-budget"})
            continue
        entry = {"location": location, "address": address}
        try:
            entry.update(parse_probe(runner(user, address, REMOTE_SCRIPT)))
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            entry.update({"state": "unreachable", "error": f"{type(error).__name__}: {error}"})
        workers.append(entry)
    measuring = [item for item in workers if item.get("state") == "measuring"]
    fractions = [item["fraction"] for item in measuring]
    phases = {}
    for item in measuring:
        phases[item["measurement"]] = phases.get(item["measurement"], 0) + 1
    return {
        "schema_version": 1,
        "run_id": run_id,
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "workers": workers,
        "worker_count": len(workers),
        "measuring": len(measuring),
        "fraction": round(sum(fractions) / len(fractions), 6) if fractions else None,
        "slowest": round(min(fractions), 6) if fractions else None,
        "fastest": round(max(fractions), 6) if fractions else None,
        "measurement": max(phases, key=phases.get) if phases else None,
        "warts_bytes": sum(item.get("warts_bytes", 0) for item in measuring),
    }


def write_progress(job_dir: Path, value: dict[str, Any]) -> None:
    path = job_dir / "progress.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def running_runs(root: Path = STATE_ROOT) -> list[str]:
    names = sorted(path.parent.name for path in (root / "jobs").glob("*/job.json"))
    return [name for name in names if RUN_ID_PATTERN.fullmatch(name) and unit_is_active(name)]


# A run's outcome lives only in its GCS manifest: the drivers delete their local
# logs after uploading them. Record it once next to job.json, so the read-only
# dashboard can show "complete" or "failed" instead of guessing after systemd has
# collected the transient unit. A run younger than this may still be starting.
OUTCOME_GRACE = timedelta(minutes=15)


def _command_option(command: list[Any], name: str) -> str | None:
    for index, item in enumerate(command):
        if item == name and index + 1 < len(command):
            return str(command[index + 1])
    return None


def manifest_fetcher() -> Callable[[str, str], dict[str, Any] | None]:
    from google.api_core.exceptions import NotFound
    from google.cloud import storage

    client = storage.Client()

    def fetch(bucket: str, name: str) -> dict[str, Any] | None:
        try:
            return json.loads(client.bucket(bucket).blob(name).download_as_bytes())
        except NotFound:
            return None

    return fetch


def record_outcomes(root: Path = STATE_ROOT,
                    fetch: Callable[[str, str], dict[str, Any] | None] | None = None,
                    now: datetime | None = None) -> list[str]:
    """Write outcome.json for every ended run that lacks one; return the run IDs written."""
    now = now or datetime.now(timezone.utc)
    written = []
    for job_path in sorted((root / "jobs").glob("*/job.json")):
        job_dir, run_id = job_path.parent, job_path.parent.name
        if (job_dir / "outcome.json").exists() or unit_is_active(run_id):
            continue
        try:
            job = json.loads(job_path.read_text(encoding="utf-8"))
            submitted = datetime.fromisoformat(str(job.get("submitted_at")))
        except (OSError, ValueError):
            continue
        if now - submitted < OUTCOME_GRACE:
            continue
        command = job.get("campaign_command") or []
        bucket = _command_option(command, "--bucket-name")
        prefix = _command_option(command, "--object-prefix") or f"runs/{run_id}"
        if not bucket:
            continue
        fetch = fetch or manifest_fetcher()
        try:
            manifest = fetch(bucket, f"{prefix}/manifest.json")
        except Exception as error:  # an unreadable bucket is retried on the next pass
            print(json.dumps({"run_id": run_id, "outcome_error": f"{type(error).__name__}: {error}"}), flush=True)
            continue
        if manifest is None:
            outcome = {"status": "unrecorded"}
        else:
            outcome = {
                "status": "complete" if manifest.get("complete") else "failed",
                "failure": manifest.get("failure"),
                "nodes": len(manifest.get("nodes") or []),
                "failed_nodes": len(manifest.get("failed_nodes") or []),
            }
        outcome.update(manifest=f"gs://{bucket}/{prefix}/manifest.json", checked_at=now.isoformat())
        path = job_dir / "outcome.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(outcome, indent=1) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        written.append(run_id)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", action="append", default=None)
    parser.add_argument("--budget-seconds", type=int, default=DEFAULT_BUDGET_SECONDS)
    parser.add_argument("--state-root", type=Path, default=STATE_ROOT)
    args = parser.parse_args(argv)

    deadline = time.monotonic() + args.budget_seconds
    recorded = record_outcomes(args.state_root)
    if recorded:
        print(json.dumps({"outcomes_recorded": recorded}), flush=True)
    targets = args.run_id or running_runs(args.state_root)
    if not targets:
        print(json.dumps({"probed": [], "note": "no running campaigns"}))
        return 0
    for run_id in targets:
        value = probe_run(run_id, args.state_root, deadline=deadline)
        write_progress(args.state_root / "jobs" / run_id, value)
        print(json.dumps({k: value[k] for k in
                          ("run_id", "worker_count", "measuring", "measurement", "fraction")}),
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
