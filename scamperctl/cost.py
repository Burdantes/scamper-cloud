from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from scamperctl.models import CostGuard, RunInventory


HOURS_PER_MONTH = 730.0
SERVER_TERMINATION_GRACE_SECONDS = 30
ESTIMATE_NOTICE = (
    "Local runtime estimate only; excludes network egress, taxes, discounts, "
    "and other Google Cloud services. Cloud Billing remains authoritative."
)


def _rounded(value: float) -> float:
    return round(value, 6)


def _cost_for_hours(
    *,
    vm_count: int,
    disk_size_gb: int,
    elapsed_hours: float,
    guard: CostGuard,
) -> tuple[float, float, float]:
    compute = vm_count * guard.estimated_vm_hourly_usd * elapsed_hours
    disk = (
        vm_count
        * disk_size_gb
        * guard.estimated_disk_gb_monthly_usd
        * elapsed_hours
        / HOURS_PER_MONTH
    )
    return compute, disk, compute + disk


def planned_cost_ceiling(
    *,
    vm_count: int,
    disk_size_gb: int,
    guard: CostGuard,
) -> dict[str, Any]:
    estimated_hours = (
        guard.max_runtime_hours + SERVER_TERMINATION_GRACE_SECONDS / 3600
    )
    compute, disk, total = _cost_for_hours(
        vm_count=vm_count,
        disk_size_gb=disk_size_gb,
        elapsed_hours=estimated_hours,
        guard=guard,
    )
    return {
        "currency": "USD",
        "vm_count": vm_count,
        "max_runtime_hours": guard.max_runtime_hours,
        "termination_grace_seconds": SERVER_TERMINATION_GRACE_SECONDS,
        "estimated_compute_usd": _rounded(compute),
        "estimated_disk_usd": _rounded(disk),
        "estimated_total_usd": _rounded(total),
        "max_estimated_cost_usd": guard.max_estimated_cost_usd,
        "within_configured_bound": total <= guard.max_estimated_cost_usd,
        "notice": ESTIMATE_NOTICE,
    }


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a timezone: {value}")
    return parsed.astimezone(timezone.utc)


def cost_snapshot(
    inventory: RunInventory,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    guard = inventory.cost_guard
    if guard is None:
        raise ValueError(
            f"run {inventory.run_id!r} has no cost guard; provision a new run with "
            "the estimated-cost and maximum-runtime flags"
        )

    started_at = _parse_timestamp(inventory.created_at)
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise ValueError("monitor time must include a timezone")
    observed_at = observed_at.astimezone(timezone.utc)
    server_delete_deadline = started_at + timedelta(
        hours=guard.max_runtime_hours,
        seconds=SERVER_TERMINATION_GRACE_SECONDS,
    )
    ended_at = (
        _parse_timestamp(inventory.destroyed_at)
        if inventory.destroyed_at is not None
        else min(observed_at, server_delete_deadline)
    )
    elapsed_hours = max(0.0, (ended_at - started_at).total_seconds() / 3600)
    compute, disk, total = _cost_for_hours(
        vm_count=len(inventory.instances),
        disk_size_gb=inventory.disk_size_gb,
        elapsed_hours=elapsed_hours,
        guard=guard,
    )
    runtime_limit_reached = elapsed_hours >= guard.max_runtime_hours
    cost_limit_reached = total >= guard.max_estimated_cost_usd
    runtime_fraction = elapsed_hours / guard.max_runtime_hours
    cost_fraction = total / guard.max_estimated_cost_usd

    return {
        "source": "local-runtime-estimate",
        "run_id": inventory.run_id,
        "project": inventory.project,
        "observed_at": observed_at.isoformat(),
        "destroyed_at": inventory.destroyed_at,
        "server_delete_deadline": server_delete_deadline.isoformat(),
        "vm_count": len(inventory.instances),
        "elapsed_hours": _rounded(elapsed_hours),
        "estimated_compute_usd": _rounded(compute),
        "estimated_disk_usd": _rounded(disk),
        "estimated_total_usd": _rounded(total),
        "max_runtime_hours": guard.max_runtime_hours,
        "max_estimated_cost_usd": guard.max_estimated_cost_usd,
        "remaining_estimated_budget_usd": _rounded(
            max(0.0, guard.max_estimated_cost_usd - total)
        ),
        "runtime_fraction": _rounded(runtime_fraction),
        "cost_fraction": _rounded(cost_fraction),
        "runtime_limit_reached": runtime_limit_reached,
        "cost_limit_reached": cost_limit_reached,
        "limit_reached": runtime_limit_reached or cost_limit_reached,
        "notice": ESTIMATE_NOTICE,
    }
