from pathlib import Path

from scamperctl.models import CostGuard, GCPProfile, Instance, RunInventory
from scamperctl.store import Store


def test_profile_round_trip(tmp_path: Path) -> None:
    store = Store(tmp_path / ".scamper")
    profile = GCPProfile(
        name="lab",
        project="example-project",
        configuration="research",
        use_iap=True,
    )

    store.save_profile(profile)

    assert store.get_profile("lab") == profile


def test_inventory_round_trip(tmp_path: Path) -> None:
    store = Store(tmp_path / ".scamper")
    inventory = RunInventory(
        run_id="baseline",
        profile="lab",
        project="example-project",
        machine_type="e2-small",
        disk_size_gb=10,
        cost_guard=CostGuard(
            estimated_vm_hourly_usd=0.05,
            estimated_disk_gb_monthly_usd=0.05,
            max_runtime_hours=2,
            max_estimated_cost_usd=1,
        ),
        instances=(
            Instance(
                name="scamper-baseline-us-central1-a-1",
                zone="us-central1-a",
                machine_type="e2-small",
                external_ip="192.0.2.1",
                status="RUNNING",
            ),
        ),
    )

    store.save_inventory(inventory)

    assert store.get_inventory("baseline") == inventory
