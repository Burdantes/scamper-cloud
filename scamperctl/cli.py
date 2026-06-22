from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Sequence

from scamperctl.gcloud import GCloudClient
from scamperctl.models import GCPProfile
from scamperctl.runner import CommandFailed, SubprocessRunner
from scamperctl.store import Store, default_home
from scamperctl.workflow import (
    DEFAULT_SCAMPER_ARGS,
    ProvisionOptions,
    build_deployment_plan,
    build_destroy_plan,
    build_provision_plan,
    collect,
    deploy,
    destroy,
    print_json,
    provision,
    startup_script,
    status,
)


logger = logging.getLogger("scamperctl")


def comma_separated(value: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in value.split(",") if item.strip())
    if not values:
        raise argparse.ArgumentTypeError("provide at least one value")
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scamperctl",
        description="Provision GCP probes separately from container deployment.",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=default_home(),
        help="local configuration and state directory (default: .scamper)",
    )
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("accounts", help="list local gcloud configurations")

    configure_parser = subparsers.add_parser(
        "configure", help="save a local GCP account/project profile"
    )
    configure_parser.add_argument("--profile", required=True)
    configure_parser.add_argument("--configuration", default="default")
    configure_parser.add_argument("--project", required=True)
    configure_parser.add_argument(
        "--use-iap",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="use Identity-Aware Proxy for SSH and SCP",
    )

    machine_parser = subparsers.add_parser(
        "machine-types", help="list GCP machine types available in a zone"
    )
    machine_parser.add_argument("--profile", required=True)
    machine_parser.add_argument("--zone", required=True)

    provision_parser = subparsers.add_parser(
        "provision", help="plan or create a set of Docker-ready GCP VMs"
    )
    provision_parser.add_argument("--profile", required=True)
    provision_parser.add_argument("--run", required=True)
    provision_parser.add_argument(
        "--zones",
        required=True,
        type=comma_separated,
        help="comma-separated zones, or 'all'",
    )
    provision_parser.add_argument("--machine-type", default="e2-small")
    provision_parser.add_argument("--count-per-zone", type=int, default=1)
    provision_parser.add_argument("--disk-size-gb", type=int, default=20)
    provision_parser.add_argument("--image-family", default="ubuntu-2204-lts")
    provision_parser.add_argument("--image-project", default="ubuntu-os-cloud")
    provision_parser.add_argument("--network", default="default")
    provision_parser.add_argument(
        "--service-account",
        help="service account email to attach to each VM",
    )
    provision_parser.add_argument("--max-vms", type=int, default=20)
    provision_parser.add_argument(
        "--apply",
        action="store_true",
        help="create resources; without this flag only the plan is printed",
    )

    deploy_parser = subparsers.add_parser(
        "deploy", help="plan or deploy a containerized experiment to a run"
    )
    deploy_parser.add_argument("--run", required=True)
    deploy_parser.add_argument("--experiment", required=True)
    deploy_parser.add_argument("--image", required=True)
    deploy_parser.add_argument(
        "--registry-auth",
        choices=("auto", "none", "artifact-registry"),
        default="auto",
        help="image-pull authentication (default: auto-detect Artifact Registry)",
    )
    deploy_parser.add_argument("--targets", required=True, type=Path)
    deploy_parser.add_argument("--scamper-args", default=DEFAULT_SCAMPER_ARGS)
    deploy_parser.add_argument(
        "--apply",
        action="store_true",
        help="transfer targets and start containers; otherwise print the plan",
    )

    status_parser = subparsers.add_parser("status", help="show run and VM status")
    status_parser.add_argument("--run", required=True)

    collect_parser = subparsers.add_parser(
        "collect", help="download an experiment's results from every VM"
    )
    collect_parser.add_argument("--run", required=True)
    collect_parser.add_argument("--experiment", required=True)
    collect_parser.add_argument("--output", type=Path, default=Path("outputs/collected"))

    destroy_parser = subparsers.add_parser(
        "destroy", help="plan or delete all VMs belonging to a run"
    )
    destroy_parser.add_argument("--run", required=True)
    destroy_parser.add_argument(
        "--apply",
        action="store_true",
        help="delete resources; without this flag only the plan is printed",
    )

    subparsers.add_parser("runs", help="list locally recorded runs")
    return parser


def _client_for_run(store: Store, run_id: str, runner: SubprocessRunner) -> GCloudClient:
    inventory = store.get_inventory(run_id)
    profile = store.get_profile(inventory.profile)
    if profile.project != inventory.project:
        raise ValueError(
            f"profile {profile.name!r} now points to {profile.project!r}, but run "
            f"{run_id!r} belongs to {inventory.project!r}"
        )
    return GCloudClient(profile, runner)


def execute(args: argparse.Namespace) -> int:
    store = Store(args.home)
    runner = SubprocessRunner()

    if args.command == "accounts":
        print_json(
            {
                "gcloud_configurations": GCloudClient.list_configurations(runner),
                "scamperctl_profiles": [
                    {"name": profile.name, **profile.to_dict()}
                    for profile in store.list_profiles()
                ],
            }
        )
        return 0

    if args.command == "configure":
        configurations = GCloudClient.list_configurations(runner)
        names = {str(item.get("name")) for item in configurations}
        if args.configuration not in names:
            raise ValueError(
                f"gcloud configuration {args.configuration!r} was not found; "
                f"available configurations: {', '.join(sorted(names)) or 'none'}"
            )
        profile = GCPProfile(
            name=args.profile,
            project=args.project,
            configuration=args.configuration,
            use_iap=args.use_iap,
        )
        store.save_profile(profile)
        print_json({"saved": profile.name, **profile.to_dict(), "path": str(store.config_path)})
        return 0

    if args.command == "machine-types":
        profile = store.get_profile(args.profile)
        print_json(GCloudClient(profile, runner).list_machine_types(args.zone))
        return 0

    if args.command == "provision":
        profile = store.get_profile(args.profile)
        client = GCloudClient(profile, runner)
        options = ProvisionOptions(
            run_id=args.run,
            zones=args.zones,
            machine_type=args.machine_type,
            count_per_zone=args.count_per_zone,
            disk_size_gb=args.disk_size_gb,
            image_family=args.image_family,
            image_project=args.image_project,
            network=args.network,
            service_account=args.service_account,
            max_vms=args.max_vms,
        )
        startup_path = store.run_directory(args.run) / "startup.sh"
        startup_path.parent.mkdir(parents=True, exist_ok=True)
        startup_path.write_text(startup_script(), encoding="utf-8")
        plan = build_provision_plan(client, options, startup_path)
        if not args.apply:
            print_json(plan)
            return 0
        inventory = provision(client, store, options)
        print_json(inventory.to_dict())
        return 0

    if args.command == "deploy":
        inventory = store.get_inventory(args.run)
        client = _client_for_run(store, args.run, runner)
        plan = build_deployment_plan(
            client,
            inventory,
            experiment=args.experiment,
            image=args.image,
            registry_auth=args.registry_auth,
            targets=args.targets,
            scamper_args=args.scamper_args,
        )
        if not args.apply:
            print_json(plan)
            return 0
        updated = deploy(
            client,
            store,
            inventory,
            experiment=args.experiment,
            image=args.image,
            registry_auth=args.registry_auth,
            targets=args.targets,
            scamper_args=args.scamper_args,
        )
        print_json(updated.to_dict())
        return 0

    if args.command == "status":
        inventory = store.get_inventory(args.run)
        client = _client_for_run(store, args.run, runner)
        print_json(status(client, inventory))
        return 0

    if args.command == "collect":
        inventory = store.get_inventory(args.run)
        client = _client_for_run(store, args.run, runner)
        path = collect(
            client,
            inventory,
            experiment=args.experiment,
            destination=args.output,
        )
        print_json({"collected": str(path)})
        return 0

    if args.command == "destroy":
        inventory = store.get_inventory(args.run)
        client = _client_for_run(store, args.run, runner)
        plan = build_destroy_plan(client, inventory)
        if not args.apply:
            print_json(plan)
            return 0
        updated = destroy(client, store, inventory)
        print_json(updated.to_dict())
        return 0

    if args.command == "runs":
        print_json([inventory.to_dict() for inventory in store.list_inventories()])
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    try:
        return execute(args)
    except (CommandFailed, FileNotFoundError, KeyError, ValueError) as err:
        logger.error("%s", err)
        return 2


if __name__ == "__main__":
    sys.exit(main())
