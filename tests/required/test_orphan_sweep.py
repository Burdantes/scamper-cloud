from __future__ import annotations

import json
from pathlib import Path

from controller import orphan_sweep


def write_job(root: Path, run_id: str) -> None:
    (root / "jobs" / run_id).mkdir(parents=True)
    (root / "jobs" / run_id / "job.json").write_text("{}", encoding="utf-8")


def test_only_runs_the_controller_created_are_candidates(tmp_path: Path) -> None:
    write_job(tmp_path, "monthly-azure-20260915-part1b")
    known = orphan_sweep.known_run_ids(tmp_path)
    assert known == {"monthly-azure-20260915-part1b"}
    # A resource group the controller never created is never a candidate, so an
    # unrelated group in the subscription cannot be swept away.
    assert "NetworkWatcherRG" not in known
    assert "PythonAzureExample-VM-rg" not in known


def test_an_unreadable_unit_status_counts_as_active(monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise OSError("systemctl missing")

    monkeypatch.setattr(orphan_sweep.subprocess, "run", explode)
    # Failing safe matters: treating an unknown status as dead would delete a
    # live campaign's workers.
    assert orphan_sweep.campaign_is_active("monthly-aws-20260915") is True


def test_active_states_are_recognised(monkeypatch) -> None:
    class Result:
        def __init__(self, text): self.stdout = text

    for state in ("active", "activating", "reloading", "deactivating"):
        monkeypatch.setattr(orphan_sweep.subprocess, "run", lambda *a, **k: Result(state + "\n"))
        assert orphan_sweep.campaign_is_active("run") is True
    monkeypatch.setattr(orphan_sweep.subprocess, "run", lambda *a, **k: Result("inactive\n"))
    assert orphan_sweep.campaign_is_active("run") is False


def test_sweep_reports_without_deleting_by_default(tmp_path: Path, monkeypatch) -> None:
    write_job(tmp_path, "monthly-azure-20260915-part1")
    monkeypatch.setattr(orphan_sweep, "azure_orphans", lambda known: [
        {"provider": "azure", "run_id": "monthly-azure-20260915-part1", "deletable": True,
         "resource_group": "monthly-azure-20260915-part1", "location": "eastus",
         "resources": 120, "vms": 20, "disks": 20, "public_ips": 20, "idle_billable": False},
    ])
    deleted = []
    monkeypatch.setattr(orphan_sweep, "delete_azure_group", lambda name: deleted.append(name))
    report = orphan_sweep.sweep(tmp_path)
    assert report["orphan_count"] == 1
    assert report["applied"] is False
    assert deleted == []


def test_sweep_deletes_only_with_apply(tmp_path: Path, monkeypatch) -> None:
    write_job(tmp_path, "run")
    monkeypatch.setattr(orphan_sweep, "azure_orphans", lambda known: [
        {"provider": "azure", "run_id": "run", "resource_group": "run", "deletable": True,
         "location": "eastus", "resources": 6, "vms": 1, "disks": 1, "public_ips": 1,
         "idle_billable": False},
    ])
    deleted = []
    monkeypatch.setattr(orphan_sweep, "delete_azure_group", lambda name: deleted.append(name))
    report = orphan_sweep.sweep(tmp_path, apply=True)
    assert deleted == ["run"]
    assert report["deleted"] == ["run"]


def test_a_provider_error_is_recorded_not_raised(tmp_path: Path, monkeypatch) -> None:
    def explode(known):
        raise RuntimeError("credential expired")

    monkeypatch.setattr(orphan_sweep, "azure_orphans", explode)
    report = orphan_sweep.sweep(tmp_path)
    assert report["orphan_count"] == 0
    assert any("credential expired" in error for error in report["errors"])


def test_report_exit_code_surfaces_orphans(tmp_path: Path, monkeypatch) -> None:
    write_job(tmp_path, "run")
    monkeypatch.setattr(orphan_sweep, "azure_orphans", lambda known: [
        {"provider": "azure", "run_id": "run", "resource_group": "run", "deletable": True,
         "location": "eastus", "resources": 6, "vms": 1, "disks": 1, "public_ips": 1,
         "idle_billable": False},
    ])
    assert orphan_sweep.main(["--state-root", str(tmp_path)]) == 1
    saved = json.loads((tmp_path / "orphans.json").read_text())
    assert saved["orphan_count"] == 1


def test_campaign_units_get_time_to_tear_down() -> None:
    root = Path(__file__).resolve().parents[2]
    submit = (root / "controller/submit.py").read_text(encoding="utf-8")
    # systemd's 90s default would SIGKILL a driver mid-teardown; an Azure
    # resource group took 304s to delete.
    assert "--property=TimeoutStopSec=900" in submit


def test_every_driver_cleans_up_on_sigterm() -> None:
    root = Path(__file__).resolve().parents[2]
    for provider in ("aws", "gcp", "azure"):
        text = (root / f"providers/{provider}/driver.py").read_text(encoding="utf-8")
        assert "install_termination_handlers" in text, provider
        assert "signal.SIGTERM" in text, provider


def test_sweep_units_are_installed_by_bootstrap() -> None:
    root = Path(__file__).resolve().parents[2]
    bootstrap = (root / "controller/bootstrap.sh").read_text(encoding="utf-8")
    service = (root / "controller/scamper-orphan-sweep.service").read_text(encoding="utf-8")
    assert "scamper-orphan-sweep.service" in bootstrap
    assert "scamper-orphan-sweep.timer" in bootstrap
    # Reporting, never deleting, on the timer's automatic path.
    assert "--apply" not in service


def test_a_group_with_no_job_record_is_reported_but_never_deleted(tmp_path: Path, monkeypatch) -> None:
    """The March 2026 bill came from azr-1773102831, which matched no job record.

    Detection has to cover it; deletion must not, because the controller cannot
    prove it created the group.
    """
    monkeypatch.setattr(orphan_sweep, "azure_orphans", lambda known: [
        {"provider": "azure", "run_id": None, "resource_group": "azr-1773102831",
         "location": "eastus", "resources": 118, "vms": 0, "disks": 59, "public_ips": 59,
         "deletable": False, "idle_billable": True},
    ])
    deleted = []
    monkeypatch.setattr(orphan_sweep, "delete_azure_group", lambda name: deleted.append(name))
    report = orphan_sweep.sweep(tmp_path, apply=True)
    assert report["orphan_count"] == 1
    assert report["idle_billable_count"] == 1
    assert report["billable_resources"] == 118
    assert deleted == []
    assert any("delete it by hand" in error for error in report["errors"])


def test_billable_types_cover_what_actually_bills_without_a_vm() -> None:
    # Disks and public IPs kept billing after the VMs went away: $142 Storage
    # plus $96 Virtual Network against $6 of compute.
    assert "Microsoft.Compute/disks" in orphan_sweep.BILLABLE_TYPES
    assert "Microsoft.Network/publicIPAddresses" in orphan_sweep.BILLABLE_TYPES
    assert "Microsoft.Compute/virtualMachines" in orphan_sweep.BILLABLE_TYPES
    # Free resources must not raise an alert on their own.
    assert "Microsoft.Network/virtualNetworks" not in orphan_sweep.BILLABLE_TYPES
    assert "Microsoft.Network/networkSecurityGroups" not in orphan_sweep.BILLABLE_TYPES
    assert "NetworkWatcherRG" in orphan_sweep.IGNORED_GROUPS
