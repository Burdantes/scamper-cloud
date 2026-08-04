from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from controller.target_registry import (
    RegisteredTarget,
    register_local_target,
    remote_target_dir,
    remote_target_path,
    target_id,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT = os.environ.get("GCP_PROJECT", "nsf-2148275-66720")
DEFAULT_SERVICE_ACCOUNT = os.environ.get(
    "GCP_SERVICE_ACCOUNT", "345441712870-compute@developer.gserviceaccount.com"
)
DEFAULT_BUCKET = os.environ.get(
    "SCAMPER_RESULTS_BUCKET", f"{DEFAULT_PROJECT}-scamper-measurements"
)
DEFAULT_NAME = "scamper-controller-uscentral1"
DEFAULT_ZONE = "us-central1-c"
DEFAULT_WORKER_IMAGE_PROJECT = os.environ.get("GCP_WORKER_IMAGE_PROJECT")
DEFAULT_WORKER_IMAGE_FAMILY = os.environ.get("GCP_WORKER_IMAGE_FAMILY")


def run(command: list[str], apply: bool) -> None:
    print("$ " + " ".join(command))
    if apply:
        subprocess.run(command, check=True)


def command_succeeds(command: list[str], apply: bool) -> bool:
    print("$ " + " ".join(command))
    if not apply:
        return False
    return subprocess.run(command, check=False).returncode == 0


def gcloud_base(args: argparse.Namespace) -> list[str]:
    return ["gcloud", "--project", args.project]


def provision_command(args: argparse.Namespace) -> list[str]:
    return [
        *gcloud_base(args),
        "compute",
        "instances",
        "create",
        args.name,
        "--zone",
        args.zone,
        "--machine-type",
        args.machine_type,
        "--network-tier",
        "STANDARD",
        "--image-family",
        "debian-12",
        "--image-project",
        "debian-cloud",
        "--boot-disk-size",
        args.boot_disk_size,
        "--service-account",
        args.service_account,
        "--scopes",
        "cloud-platform",
        "--labels",
        "role=scamper-controller",
    ]


def bundle_repository(destination: Path) -> None:
    excluded_top_level = {
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".scamper",
        "__pycache__",
        "credentials",
        "datasets",
        "logs",
        "outputs",
        "warts",
    }
    with tarfile.open(destination, "w:gz") as archive:
        for path in REPO_ROOT.rglob("*"):
            relative = path.relative_to(REPO_ROOT)
            if relative.parts[0] in excluded_top_level:
                continue
            if "__pycache__" in relative.parts or path.suffix == ".pyc":
                continue
            archive.add(path, arcname=relative, recursive=False)


def ssh_command(args: argparse.Namespace, remote_command: str) -> list[str]:
    return [
        *gcloud_base(args),
        "compute",
        "ssh",
        args.name,
        "--zone",
        args.zone,
        "--command",
        remote_command,
    ]


def scp_command(
    args: argparse.Namespace, sources: list[Path], destination: str
) -> list[str]:
    return [
        *gcloud_base(args),
        "compute",
        "scp",
        *(str(source) for source in sources),
        f"{args.name}:{destination}",
        "--zone",
        args.zone,
    ]


def registered_target_exists_command(
    args: argparse.Namespace, registered_id: str
) -> list[str]:
    target_dir = remote_target_dir(registered_id)
    target_path = remote_target_path(registered_id)
    remote = (
        f"sudo test -f {shlex.quote(str(target_path))} "
        f"-a -f {shlex.quote(str(target_dir / 'manifest.json'))}"
    )
    return ssh_command(args, remote)


def upload_registered_target(
    args: argparse.Namespace,
    registration: RegisteredTarget,
) -> None:
    if command_succeeds(
        registered_target_exists_command(args, registration.target_id),
        args.apply,
    ):
        print(f"TARGET_REGISTERED_REUSED id={registration.target_id}")
        return

    digest = registration.normalized_sha256
    staging_dir = f"/tmp/scamper-target-{digest}"
    target_dir = remote_target_dir(registration.target_id)
    target_path = remote_target_path(registration.target_id)
    run(ssh_command(args, f"install -d {shlex.quote(staging_dir)}"), args.apply)
    run(
        scp_command(
            args,
            [registration.targets_path, registration.manifest_path],
            f"{staging_dir}/",
        ),
        args.apply,
    )
    install_command = " && ".join(
        (
            f"sudo install -d -m 0755 {shlex.quote(str(target_dir))}",
            "sudo install -m 0644 "
            f"{shlex.quote(f'{staging_dir}/targets.txt')} "
            f"{shlex.quote(str(target_path))}",
            "sudo install -m 0644 "
            f"{shlex.quote(f'{staging_dir}/manifest.json')} "
            f"{shlex.quote(str(target_dir / 'manifest.json'))}",
            f"rm -f -- {shlex.quote(f'{staging_dir}/targets.txt')} "
            f"{shlex.quote(f'{staging_dir}/manifest.json')}",
            f"rmdir -- {shlex.quote(staging_dir)}",
        )
    )
    run(ssh_command(args, install_command), args.apply)
    print(f"TARGET_REGISTERED id={registration.target_id}")


def register_target(
    args: argparse.Namespace,
    source: Path,
    staging_root: Path,
) -> RegisteredTarget:
    registration = register_local_target(source, staging_root)
    upload_registered_target(args, registration)
    return registration


def require_registered_target(args: argparse.Namespace, registered_id: str) -> Path:
    exists = command_succeeds(
        registered_target_exists_command(args, registered_id), args.apply
    )
    if args.apply and not exists:
        raise FileNotFoundError(
            f"target ID is not registered on the controller: {registered_id}"
        )
    return remote_target_path(registered_id)


def resolve_submission_target(
    args: argparse.Namespace,
    *,
    role: str,
    staging_root: Path,
) -> Path:
    source = getattr(args, f"{role}_targets")
    registered_id = getattr(args, f"{role}_target_id")
    if registered_id:
        return require_registered_target(args, registered_id)
    if source is None:
        raise ValueError(f"missing {role} target source")
    return remote_target_path(register_target(args, source, staging_root).target_id)


def deploy(args: argparse.Namespace) -> None:
    release = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory(prefix="scamper-controller-") as temp_dir:
        bundle = Path(temp_dir) / f"scamper-cloud-{release}.tar.gz"
        bundle_repository(bundle)
        bootstrap = REPO_ROOT / "controller/bootstrap.sh"
        run(scp_command(args, [bundle, bootstrap], "/tmp/"), args.apply)
        remote = (
            "sudo bash /tmp/bootstrap.sh "
            f"/tmp/{bundle.name} {args.project} {args.service_account} "
            f"{args.bucket} {release}"
        )
        run(ssh_command(args, remote), args.apply)


def submit(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="scamper-target-registry-") as temp_dir:
        staging_root = Path(temp_dir)
        trace_target = resolve_submission_target(
            args, role="trace", staging_root=staging_root
        )
        rr_target = resolve_submission_target(
            args, role="rr", staging_root=staging_root
        )

    remote_dir = f"/var/lib/scamper-controller/targets/{args.run_id}"
    run(
        ssh_command(
            args,
            f"sudo install -d -o scamper-controller -g scamper-controller {remote_dir}",
        ),
        args.apply,
    )
    sources = [args.do_not_probe_file]
    run(scp_command(args, sources, "/tmp/"), args.apply)
    names = [source.name for source in sources]
    install_parts = [
        "sudo install -o scamper-controller -g scamper-controller "
        f"{shlex.quote(f'/tmp/{name}')} {shlex.quote(f'{remote_dir}/{name}')}"
        for name in names
    ]
    run(ssh_command(args, " && ".join(install_parts)), args.apply)

    command = [
        "sudo",
        "/opt/scamper-cloud/current/.venv/bin/python",
        "/opt/scamper-cloud/current/controller/submit.py",
        "--run-id",
        args.run_id,
        "--trace-targets",
        str(trace_target),
        "--rr-targets",
        str(rr_target),
        "--do-not-probe-file",
        f"{remote_dir}/{args.do_not_probe_file.name}",
        "--bucket",
        args.bucket,
        "--regions",
        args.regions,
        "--worker-machine-type",
        args.worker_machine_type,
        "--measurements",
        args.measurements,
        "--max-instances",
        str(args.max_instances),
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
    ]
    if args.worker_image_project:
        command.extend(["--worker-image-project", args.worker_image_project])
    if args.worker_image_family:
        command.extend(["--worker-image-family", args.worker_image_family])
    if args.max_targets is not None:
        command.extend(["--max-targets", str(args.max_targets)])
    if args.skip_smoke:
        command.append("--skip-smoke")
    if not args.apply:
        command.append("--dry-run")
    run(ssh_command(args, shlex.join(command)), args.apply)


def register_targets(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="scamper-target-registry-") as temp_dir:
        staging_root = Path(temp_dir)
        trace = register_target(args, args.trace_targets, staging_root)
        rr = register_target(args, args.rr_targets, staging_root)
    print(
        json.dumps(
            {
                "trace_target_id": trace.target_id,
                "rr_target_id": rr.target_id,
                "trace_target_count": trace.target_count,
                "rr_target_count": rr.target_count,
            },
            indent=2,
        )
    )


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument("--zone", default=DEFAULT_ZONE)
    parser.add_argument("--service-account", default=DEFAULT_SERVICE_ACCOUNT)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--apply", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the persistent GCP controller VM."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    provision_parser = subparsers.add_parser("provision")
    add_common(provision_parser)
    provision_parser.add_argument("--machine-type", default="e2-small")
    provision_parser.add_argument("--boot-disk-size", default="20GB")

    deploy_parser = subparsers.add_parser("deploy")
    add_common(deploy_parser)

    register_parser = subparsers.add_parser("register-targets")
    add_common(register_parser)
    register_parser.add_argument("--trace-targets", type=Path, required=True)
    register_parser.add_argument("--rr-targets", type=Path, required=True)

    submit_parser = subparsers.add_parser("submit")
    add_common(submit_parser)
    submit_parser.add_argument("--run-id", required=True)
    trace_targets = submit_parser.add_mutually_exclusive_group(required=True)
    trace_targets.add_argument("--trace-targets", type=Path)
    trace_targets.add_argument("--trace-target-id", type=target_id)
    rr_targets = submit_parser.add_mutually_exclusive_group(required=True)
    rr_targets.add_argument("--rr-targets", type=Path)
    rr_targets.add_argument("--rr-target-id", type=target_id)
    submit_parser.add_argument(
        "--do-not-probe-file", type=Path, default=REPO_ROOT / "config/do-not-probe.txt"
    )
    submit_parser.add_argument("--regions", required=True)
    submit_parser.add_argument("--worker-machine-type", default="e2-micro")
    submit_parser.add_argument(
        "--worker-image-project", default=DEFAULT_WORKER_IMAGE_PROJECT
    )
    submit_parser.add_argument(
        "--worker-image-family", default=DEFAULT_WORKER_IMAGE_FAMILY
    )
    submit_parser.add_argument("--measurements", default="trace,rr")
    submit_parser.add_argument("--max-instances", type=int, default=1)
    submit_parser.add_argument("--max-targets", type=int)
    submit_parser.add_argument("--trace-rate", type=int, default=1000)
    submit_parser.add_argument("--rr-rate", type=int, default=1000)
    submit_parser.add_argument("--rr-timeout", type=float, default=2.0)
    submit_parser.add_argument(
        "--probe-payload",
        default="Academic probing; please reach out to ls3748@columbia.edu to opt out.",
    )
    submit_parser.add_argument("--measurement-contact", default="ls3748@columbia.edu")
    submit_parser.add_argument("--skip-smoke", action="store_true")

    status_parser = subparsers.add_parser("status")
    add_common(status_parser)
    status_parser.add_argument("--run-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "provision":
        run(provision_command(args), args.apply)
    elif args.action == "deploy":
        deploy(args)
    elif args.action == "register-targets":
        register_targets(args)
    elif args.action == "submit":
        submit(args)
    else:
        suffix = f" {args.run_id}" if args.run_id else ""
        run(ssh_command(args, f"sudo scamper-controller-status{suffix}"), args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
