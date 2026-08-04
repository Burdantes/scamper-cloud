from datetime import datetime, timezone

import pytest

from scamperctl.cost import cost_snapshot, planned_cost_ceiling
from scamperctl.models import CostGuard, Instance, RunInventory


def guard() -> CostGuard:
    return CostGuard(
        estimated_vm_hourly_usd=0.10,
        estimated_disk_gb_monthly_usd=0.05,
        max_runtime_hours=4,
        max_estimated_cost_usd=1,
    )


def test_planned_cost_ceiling_includes_compute_and_disk() -> None:
    estimate = planned_cost_ceiling(vm_count=2, disk_size_gb=10, guard=guard())

    assert estimate["estimated_compute_usd"] == pytest.approx(0.801667, abs=0.000001)
    assert estimate["estimated_disk_usd"] == pytest.approx(0.005491, abs=0.000001)
    assert estimate["within_configured_bound"] is True


def test_cost_snapshot_uses_elapsed_runtime() -> None:
    inventory = RunInventory(
        run_id="bounded",
        profile="lab",
        project="example-project",
        machine_type="e2-small",
        disk_size_gb=10,
        cost_guard=guard(),
        created_at="2026-06-23T10:00:00+00:00",
        instances=(
            Instance("probe-a", "us-central1-a", "e2-small"),
            Instance("probe-b", "us-east1-b", "e2-small"),
        ),
    )

    snapshot = cost_snapshot(
        inventory,
        now=datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc),
    )

    assert snapshot["elapsed_hours"] == 2
    assert snapshot["estimated_compute_usd"] == 0.4
    assert snapshot["runtime_fraction"] == 0.5
    assert snapshot["limit_reached"] is False


def test_cost_snapshot_requires_guard() -> None:
    inventory = RunInventory(
        run_id="legacy",
        profile="lab",
        project="example-project",
        machine_type="e2-small",
    )

    with pytest.raises(ValueError, match="has no cost guard"):
        cost_snapshot(inventory)


def test_cost_snapshot_stops_accruing_after_server_delete_deadline() -> None:
    inventory = RunInventory(
        run_id="bounded",
        profile="lab",
        project="example-project",
        machine_type="e2-small",
        cost_guard=guard(),
        created_at="2026-06-23T10:00:00+00:00",
        instances=(Instance("probe-a", "us-central1-a", "e2-small"),),
    )

    snapshot = cost_snapshot(
        inventory,
        now=datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc),
    )

    assert snapshot["elapsed_hours"] == pytest.approx(4.008333, abs=0.000001)
    assert snapshot["runtime_limit_reached"] is True
