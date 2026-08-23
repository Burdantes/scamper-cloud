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


def test_profile_defaults_to_gcp_and_round_trips_provider() -> None:
    from scamperctl.models import Profile, GCPProfile

    assert GCPProfile is Profile  # old name kept working

    profile = Profile(name="p", project="proj")
    assert profile.provider == "gcp"
    assert profile.to_dict()["provider"] == "gcp"
    assert Profile.from_dict("p", profile.to_dict()) == profile


def test_profile_from_dict_accepts_inventories_written_before_providers() -> None:
    """Older files omit the provider field entirely; they must still load."""
    from scamperctl.models import Profile

    profile = Profile.from_dict("p", {"project": "proj"})
    assert profile.provider == "gcp"


def test_unsupported_provider_is_rejected_at_the_model_boundary() -> None:
    """An unported provider must not be able to enter a profile or inventory."""
    import pytest
    from scamperctl.models import Profile, RunInventory

    with pytest.raises(ValueError, match="not a supported provider"):
        Profile(name="p", project="proj", provider="azure")

    with pytest.raises(ValueError, match="not a supported provider"):
        RunInventory(
            run_id="r", profile="p", project="proj",
            machine_type="e2-small", provider="aws",
        )


def test_run_inventory_round_trips_provider() -> None:
    from scamperctl.models import RunInventory

    inventory = RunInventory(
        run_id="r", profile="p", project="proj", machine_type="e2-small"
    )
    assert inventory.provider == "gcp"
    assert inventory.to_dict()["provider"] == "gcp"
    assert RunInventory.from_dict(inventory.to_dict()).provider == "gcp"
    # A file written before the provider dimension existed.
    legacy_shaped = {k: v for k, v in inventory.to_dict().items() if k != "provider"}
    assert RunInventory.from_dict(legacy_shaped).provider == "gcp"


def test_gcloud_client_satisfies_the_provider_neutral_contract() -> None:
    """Adding a provider means satisfying CloudClient, not editing workflow.py."""
    from scamperctl.cloud import CloudClient, SupportsOSLogin
    from scamperctl.gcloud import GCloudClient

    required = [name for name in vars(CloudClient) if not name.startswith("_")]
    assert required, "protocol should declare methods"
    assert [m for m in required if not hasattr(GCloudClient, m)] == []
    # OS Login stays out of the shared contract: no AWS/Azure equivalent.
    assert "project_os_login_enabled" not in required
    assert hasattr(GCloudClient, "project_os_login_enabled")
    assert "project_os_login_enabled" in vars(SupportsOSLogin)
