from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from providers import driver_module

STATE_ROOT = Path("/var/lib/scamper-controller")
INSTALL_ROOT = Path("/opt/scamper-cloud/current")
RUN_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
DEFAULT_PAYLOAD = (
    "Academic probing; please reach out to ls3748@columbia.edu to opt out."
)


def assert_controller_origin(allow_foreign: bool = False) -> str:
    """Refuse to submit a campaign from anywhere but the controller.

    Measurements must originate here so that provisioning, teardown and the job
    record live on one long-lived host. Campaigns launched from a workstation
    have no durable job record, so nobody can later tell what was run, with which
    code, or whether teardown completed - which is how a 44-VM Azure campaign ran
    for eight days on VMs nobody was tracking.

    Identified by the install root the release process creates, not by hostname,
    so a rebuilt or renamed controller still qualifies.
    """
    if INSTALL_ROOT.is_dir() and STATE_ROOT.is_dir():
        return "controller"
    if allow_foreign:
        return "foreign"
    raise SystemExit(
        f"refusing to submit: {INSTALL_ROOT} and {STATE_ROOT} are not both present, "
        "so this is not the scamper controller. Measurements must originate from "
        "the controller. Run this on the controller host, or pass "
        "--allow-foreign-origin if you have accepted that no durable job record "
        "will exist."
    )


def run_id(value: str) -> str:
    if RUN_ID_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "run ID must be 1-63 lowercase letters, digits, or hyphens"
        )
    return value


def existing_file(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"file does not exist: {path}")
    return path


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def campaign_command(args: argparse.Namespace, job_dir: Path) -> list[str]:
    command = [
        str(INSTALL_ROOT / ".venv/bin/python"),
        "-m",
        driver_module(args.provider),
        "--apply",
        "--prefix",
        args.run_id,
        "--log-dir",
        str(job_dir / "logs"),
        "--trace-target-source",
        str(args.trace_targets),
        "--rr-target-source",
        str(args.rr_targets),
        "--bucket-name",
        args.bucket,
        "--object-prefix",
        args.object_prefix or f"runs/{args.run_id}",
        "--regions",
        args.regions,
        "--measurements",
        args.measurements,
        "--trace-rate",
        str(args.trace_rate),
        "--rr-rate",
        str(args.rr_rate),
        "--rr-timeout",
        str(args.rr_timeout),
        "--probe-payload",
        args.probe_payload,
        "--measurement-contact",
        args.measurement_contact,
        "--do-not-probe-file",
        str(args.do_not_probe_file),
    ]
    if args.max_instances is not None:
        command.extend(["--max-instances", str(args.max_instances)])
    if args.max_targets is not None:
        command.extend(["--max-targets", str(args.max_targets)])
    if args.skip_smoke:
        command.append("--skip-smoke")
    return command


def systemd_command(args: argparse.Namespace, command: list[str]) -> list[str]:
    environment = [f"--setenv=GCP_MACHINE_TYPE={args.worker_machine_type}"]
    worker_image_project = getattr(args, "worker_image_project", None)
    worker_image_family = getattr(args, "worker_image_family", None)
    if worker_image_project:
        environment.append(f"--setenv=GCP_IMAGE_PROJECT={worker_image_project}")
    if worker_image_family:
        environment.append(f"--setenv=GCP_IMAGE_FAMILY={worker_image_family}")
    return [
        "systemd-run",
        f"--unit=scamper-campaign-{args.run_id}",
        "--collect",
        "--property=Type=exec",
        *environment,
        "--uid=scamper-controller",
        "--gid=scamper-controller",
        f"--working-directory={INSTALL_ROOT}",
        "/usr/local/bin/scamper-controller-run",
        *command,
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Submit a durable scamper campaign from the controller."
    )
    parser.add_argument(
        "--provider",
        default="gcp",
        help=(
            "cloud provider to launch on. Must have a supported campaign driver; "
            "see providers.DRIVER_MODULES."
        ),
    )
    parser.add_argument("--run-id", required=True, type=run_id)
    parser.add_argument("--trace-targets", required=True, type=existing_file)
    parser.add_argument("--rr-targets", required=True, type=existing_file)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--object-prefix")
    parser.add_argument("--regions", required=True)
    parser.add_argument("--worker-machine-type", default="e2-micro")
    parser.add_argument("--worker-image-project")
    parser.add_argument("--worker-image-family")
    parser.add_argument("--measurements", default="trace,rr")
    parser.add_argument("--max-instances", type=positive_int)
    parser.add_argument("--max-targets", type=positive_int)
    parser.add_argument("--trace-rate", type=positive_int, default=1000)
    parser.add_argument("--rr-rate", type=positive_int, default=1000)
    parser.add_argument("--rr-timeout", type=float, default=2.0)
    parser.add_argument("--probe-payload", default=DEFAULT_PAYLOAD)
    parser.add_argument("--measurement-contact", default="ls3748@columbia.edu")
    parser.add_argument("--do-not-probe-file", required=True, type=existing_file)
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allow-foreign-origin",
        action="store_true",
        help=(
            "submit from a host that is not the controller. Recorded in the job "
            "spec. Use only when you have accepted that no durable job record "
            "will exist on the controller."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    origin = assert_controller_origin(args.allow_foreign_origin)
    job_dir = STATE_ROOT / "jobs" / args.run_id
    job_spec = job_dir / "job.json"
    if job_spec.exists() and not args.dry_run:
        raise FileExistsError(f"run ID already submitted: {args.run_id}")
    command = campaign_command(args, job_dir)
    service_command = systemd_command(args, command)
    spec = {
        "schema_version": 1,
        "run_id": args.run_id,
        "provider": args.provider,
        "origin": origin,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "campaign_command": command,
        "systemd_command": service_command,
    }
    print(json.dumps(spec, indent=2))
    if args.dry_run:
        return 0
    job_dir.mkdir(parents=True, exist_ok=False)
    logs_dir = job_dir / "logs"
    logs_dir.mkdir()
    job_spec.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    controller_user = pwd.getpwnam("scamper-controller")
    for path in (job_dir, logs_dir, job_spec):
        os.chown(path, controller_user.pw_uid, controller_user.pw_gid)
    subprocess.run(service_command, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
