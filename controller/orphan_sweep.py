"""Find cloud workers that no running campaign owns.

A campaign deletes its own resources in a finally: block. That block does not
run if the driver is killed, the controller reboots, or the process is OOM
killed, and the workers then bill indefinitely with nothing left to stop them.
This sweeps each provider for resources whose campaign unit is no longer
active and reports them; --apply deletes them.

Reporting is the default deliberately: deleting a live campaign's workers would
destroy days of measurement, so a resource is only ever a candidate when the
controller has a job record proving it created it AND that campaign's systemd
unit is not running.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_ROOT = Path("/var/lib/scamper-controller")
REPORT_PATH = STATE_ROOT / "orphans.json"
RUN_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def known_run_ids(root: Path) -> set[str]:
    """Run IDs the controller has a job record for: the only deletion candidates."""
    return {
        path.parent.name
        for path in (root / "jobs").glob("*/job.json")
        if RUN_ID_PATTERN.fullmatch(path.parent.name)
    }


def campaign_is_active(run_id: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", f"scamper-campaign-{run_id}.service"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Unknown beats assuming dead: never delete on a failed status check.
        return True
    return result.stdout.strip() in {"active", "activating", "reloading", "deactivating"}


def azure_orphans(known: set[str]) -> list[dict[str, Any]]:
    from azure.identity import ClientSecretCredential
    from azure.mgmt.resource.resources import ResourceManagementClient

    subscription = os.environ["AZURE_SUBSCRIPTION_ID"]
    credential = ClientSecretCredential(
        os.environ["AZURE_TENANT_ID"],
        os.environ["AZURE_CLIENT_ID"],
        os.environ["AZURE_CLIENT_SECRET"],
    )
    client = ResourceManagementClient(credential, subscription)
    counts: dict[str, int] = {}
    for resource in client.resources.list():
        group = resource.id.split("/")[4]
        counts[group] = counts.get(group, 0) + 1
    found = []
    for group in client.resource_groups.list():
        name = group.name
        if name not in known or campaign_is_active(name):
            continue
        found.append({
            "provider": "azure", "run_id": name, "resource_group": name,
            "location": group.location, "resources": counts.get(name, 0),
        })
    return found


def delete_azure_group(name: str) -> None:
    from azure.identity import ClientSecretCredential
    from azure.mgmt.resource.resources import ResourceManagementClient

    credential = ClientSecretCredential(
        os.environ["AZURE_TENANT_ID"],
        os.environ["AZURE_CLIENT_ID"],
        os.environ["AZURE_CLIENT_SECRET"],
    )
    client = ResourceManagementClient(credential, os.environ["AZURE_SUBSCRIPTION_ID"])
    client.resource_groups.begin_delete(name).wait()


def write_report(value: dict[str, Any], path: Path = REPORT_PATH) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def sweep(root: Path = STATE_ROOT, apply: bool = False) -> dict[str, Any]:
    known = known_run_ids(root)
    orphans: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        orphans.extend(azure_orphans(known))
    except Exception as error:  # noqa: BLE001 - a provider outage must not hide the rest
        errors.append(f"azure: {type(error).__name__}: {error}")
    deleted = []
    if apply:
        for orphan in orphans:
            try:
                delete_azure_group(orphan["resource_group"])
                deleted.append(orphan["run_id"])
            except Exception as error:  # noqa: BLE001
                errors.append(f"delete {orphan['run_id']}: {type(error).__name__}: {error}")
    return {
        "schema_version": 1,
        "swept_at": datetime.now(timezone.utc).isoformat(),
        "applied": apply,
        "orphans": orphans,
        "orphan_count": len(orphans),
        "deleted": deleted,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="delete the orphans instead of only reporting them")
    parser.add_argument("--state-root", type=Path, default=STATE_ROOT)
    args = parser.parse_args(argv)
    report = sweep(args.state_root, apply=args.apply)
    write_report(report, args.state_root / "orphans.json")
    print(json.dumps(report, indent=1))
    # Non-zero makes the timer's failure visible in systemctl --failed.
    return 1 if report["orphans"] and not args.apply else 0


if __name__ == "__main__":
    raise SystemExit(main())
