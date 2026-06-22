from pathlib import Path

from scamperctl.models import GCPProfile, Instance, RunInventory
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
