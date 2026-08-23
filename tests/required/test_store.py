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

    # An entirely unknown provider cannot enter a model.
    with pytest.raises(ValueError, match="not a supported provider"):
        Profile(name="p", project="proj", provider="oracle")

    with pytest.raises(ValueError, match="not a supported provider"):
        RunInventory(
            run_id="r", profile="p", project="proj",
            machine_type="e2-small", provider="oracle",
        )

    # aws and azure have CloudClients, so they are valid in a model...
    for provider in ("aws", "azure"):
        assert Profile(name="p", project="proj", provider=provider).provider == provider

    # ...but neither has a campaign driver, which must stay refused.
    from providers import driver_module

    for provider in ("aws", "azure"):
        with pytest.raises(ValueError, match="no supported campaign driver"):
            driver_module(provider)


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


def test_aws_client_satisfies_the_contract_and_never_stops_instances() -> None:
    """Teardown must terminate: a stopped instance keeps paying for its volumes."""
    from pathlib import Path

    from providers import client_for, supported_providers
    from scamperctl.cloud import CloudClient
    from scamperctl.models import Instance

    assert "aws" in supported_providers()
    client = client_for("aws", region="eu-west-1")
    required = [m for m in vars(CloudClient) if not m.startswith("_")]
    assert [m for m in required if not hasattr(client, m)] == []

    instance = Instance(name="i-0abc", zone="eu-west-1a", machine_type="t3.micro")
    delete = client.delete_instance_args(instance)
    assert "terminate-instances" in delete
    assert "stop-instances" not in delete

    # Instance type is configurable, unlike the legacy driver's hardcoded list.
    create = client_for(
        "aws", region="eu-west-1", instance_type="t4g.nano"
    ).create_instance_args(name="n", zone="eu-west-1a", image_id="ami-1")
    assert "t4g.nano" in create
    assert "terminate" in create  # instance-initiated-shutdown-behavior

    # OS Login is GCP-only and must not have been stubbed onto AWS.
    assert not hasattr(client, "project_os_login_enabled")


def test_azure_client_always_opens_inbound_icmp_on_create() -> None:
    """The 2026-08-13 regression: without this rule a traceroute VM sees no path.

    Azure NSGs are stateful per flow, so an echo reply is admitted while an ICMP
    Time Exceeded from a different source hits DenyAllInbound. 43 of 44 regions
    recorded destinations and zero intermediate hops because of it.
    """
    from providers import client_for
    from scamperctl.runner import CommandResult

    calls: list[list[str]] = []

    class RecordingRunner:
        def run(self, args, *, check: bool = True) -> CommandResult:
            calls.append(list(args))
            return CommandResult(returncode=0, stdout="{}", stderr="")

    client = client_for(
        "azure", region="westeurope", resource_group="rg", runner=RecordingRunner()
    )
    client.create_instance(name="probe-1")

    joined = [" ".join(c) for c in calls]
    icmp = [c for c in joined if "nsg" in c and "rule" in c]
    assert icmp, f"instance creation must add an inbound ICMP rule; saw {joined}"
    rule = icmp[0]
    for expected in ("--protocol Icmp", "--direction Inbound", "--access Allow"):
        assert expected in rule, f"missing {expected!r} in {rule!r}"


def test_azure_defaults_to_a_cheap_size_and_deletes_on_teardown() -> None:
    """D2s_v5 at ~$0.10/hr x 44 VMs, left stopped, cost ~$150/day for 8 days."""
    from providers import client_for
    from providers.azure.client import DEFAULT_INSTANCE_TYPE
    from scamperctl.models import Instance

    assert DEFAULT_INSTANCE_TYPE == "Standard_B2ts_v2"
    assert "D2s_v5" not in DEFAULT_INSTANCE_TYPE

    client = client_for("azure", region="westeurope", resource_group="rg")
    create = " ".join(client.create_instance_args(name="probe-1"))
    assert "Standard_B2ts_v2" in create
    assert "StandardSSD_LRS" in create  # not the Premium default of D-series

    instance = Instance(name="probe-1", zone="westeurope", machine_type="Standard_B2ts_v2")
    delete = client.delete_instance_args(instance)
    assert "delete" in delete
    # "stop" leaves the VM allocated and billing compute at full rate.
    assert "stop" not in delete
    # Deallocate exists, but it is explicitly not teardown.
    assert "deallocate" in client.deallocate_instance_args(instance)
