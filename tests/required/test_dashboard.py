from __future__ import annotations

import json
from pathlib import Path

from controller import dashboard


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_newest_cycle_uses_jobs_and_completed_state(tmp_path: Path) -> None:
    write_json(tmp_path / "jobs/monthly-aws-20260915/job.json", {})
    write_json(tmp_path / "monthly/20260916.json", {})
    write_json(tmp_path / "monthly/202609.json", {})
    assert dashboard.newest_cycle(tmp_path) == "20260916"


def test_provider_status_reports_running_job_and_milestones(tmp_path: Path, monkeypatch) -> None:
    run_id = "monthly-aws-20260915"
    command = ["driver", "--regions", "us-east-1,us-west-2", "--measurements", "trace,trace6,rr"]
    write_json(tmp_path / f"jobs/{run_id}/job.json", {"campaign_command": command, "submitted_at": "now"})
    log = tmp_path / f"jobs/{run_id}/logs/{run_id}.log"
    log.parent.mkdir(parents=True)
    log.write_text("Creating Instance in us-east-1\nInstance us-east-1 is ready for ssh\nInstance us-east-1 started\nWaiting for 5 AWS campaign artifacts\n")
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "active", "sub": "running", "since": "now", "exit_code": "0"})
    value = dashboard.provider_status("aws", "20260915", tmp_path, {}, {})
    assert value["state"] == "running"
    assert value["regions"] == 2
    assert value["measurements"] == ["trace", "trace6", "rr"]
    assert value["milestones"] == {"created": 1, "ready": 1, "started": 1, "artifacts_remaining": 5, "errors": []}


def test_provider_status_reports_readiness_block_without_job(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "inactive", "sub": "dead", "since": "", "exit_code": "0"})
    value = dashboard.provider_status("azure", "20260915", tmp_path, {}, {"blocked": {"azure": ["region unavailable"]}})
    assert value["state"] == "blocked"
    assert value["blocked_count"] == 1


def test_provider_status_uses_recorded_failure_after_transient_unit_is_collected(tmp_path: Path, monkeypatch) -> None:
    run_id = "monthly-aws-20260915"
    write_json(tmp_path / f"jobs/{run_id}/job.json", {"campaign_command": []})
    write_json(tmp_path / "monthly/20260915.json", {"results": [{"provider": "aws", "status": "failed", "exit_code": 1}]})
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "inactive", "sub": "dead", "since": "", "exit_code": ""})
    value = dashboard.provider_status("aws", "20260915", tmp_path, {}, {})
    assert value["state"] == "failed"
    assert value["exit_code"] == 1


def test_provider_status_includes_persisted_failure_reason(tmp_path: Path, monkeypatch) -> None:
    run_id = "monthly-aws-20260915"
    write_json(tmp_path / f"jobs/{run_id}/job.json", {"campaign_command": []})
    write_json(tmp_path / f"jobs/{run_id}/summary.json", {"failure_reason": "region unavailable"})
    write_json(tmp_path / "monthly/20260915.json", {"results": [{"provider": "aws", "status": "failed"}]})
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "inactive", "sub": "dead", "since": "", "exit_code": ""})
    value = dashboard.provider_status("aws", "20260915", tmp_path, {}, {})
    assert value["failure_reason"] == "region unavailable"


def test_milestones_read_the_artifact_wait_line_from_every_driver() -> None:
    aws = dashboard.milestones(["Waiting for 4 AWS artifacts to upload"])
    gcp = dashboard.milestones(["Waiting for 5 GCP campaign artifacts across 3 workers"])
    assert aws["artifacts_remaining"] == 4
    assert gcp["artifacts_remaining"] == 5


def test_milestones_count_aws_instance_creation_with_its_security_group_suffix() -> None:
    value = dashboard.milestones([
        "Creating Instance in us-east-1 with security group sg-0a1b2c3d",
        "Creating Instance in us-west-2 with security group sg-0e4f5a6b",
        "Instance aws-us-east-1-0 started",
    ])
    assert value["created"] == 2
    assert value["started"] == 1


def test_dashboard_state_lists_every_submitted_run_regardless_of_name(tmp_path: Path, monkeypatch) -> None:
    write_json(tmp_path / "jobs/monthly-aws-20260915/job.json", {"provider": "aws", "submitted_at": "2026-09-15T00:00:00Z"})
    write_json(tmp_path / "jobs/adhoc-gcp-probe/job.json", {"provider": "gcp", "submitted_at": "2026-09-14T00:00:00Z"})
    write_json(tmp_path / "jobs/rr-followup/job.json", {"provider": "azure", "submitted_at": "2026-08-02T00:00:00Z"})
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "inactive", "sub": "dead", "since": "", "exit_code": "0"})
    value = dashboard.dashboard_state(None, tmp_path, tmp_path / "release")
    listed = {item["run_id"] for item in value["runs"]}
    assert {"monthly-aws-20260915", "adhoc-gcp-probe", "rr-followup"} <= listed
    assert value["run_count"] == len(value["runs"])
    by_id = {item["run_id"]: item for item in value["runs"]}
    assert by_id["adhoc-gcp-probe"]["provider"] == "gcp"
    assert by_id["adhoc-gcp-probe"]["cycle"] == "20260914"
    assert by_id["rr-followup"]["state"] == "submitted"


def test_dashboard_state_still_lists_queued_clouds_for_the_newest_cycle(tmp_path: Path, monkeypatch) -> None:
    write_json(tmp_path / "jobs/monthly-aws-20260915/job.json", {"provider": "aws"})
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "inactive", "sub": "dead", "since": "", "exit_code": "0"})
    value = dashboard.dashboard_state(None, tmp_path, tmp_path / "release")
    queued = {item["run_id"]: item["state"] for item in value["runs"]}
    assert queued["monthly-azure-20260915"] == "queued"
    assert queued["monthly-gcp-20260915"] == "queued"


def test_dashboard_state_cycle_filter_keeps_only_that_cycle(tmp_path: Path, monkeypatch) -> None:
    write_json(tmp_path / "jobs/monthly-aws-20260915/job.json", {"provider": "aws"})
    write_json(tmp_path / "jobs/adhoc-gcp-probe/job.json", {"provider": "gcp", "submitted_at": "2026-08-14T00:00:00Z"})
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "inactive", "sub": "dead", "since": "", "exit_code": "0"})
    value = dashboard.dashboard_state("20260915", tmp_path, tmp_path / "release")
    assert {item["run_id"] for item in value["runs"]} == {
        "monthly-aws-20260915", "monthly-azure-20260915", "monthly-gcp-20260915",
    }


def test_run_status_scopes_preflight_findings_to_the_matching_cycle(tmp_path: Path, monkeypatch) -> None:
    write_json(tmp_path / "jobs/adhoc-azure-probe/job.json", {"provider": "azure", "submitted_at": "2026-08-14T00:00:00Z"})
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "inactive", "sub": "dead", "since": "", "exit_code": "0"})
    blocked = {"blocked": {"azure": ["region unavailable"]}}
    assert dashboard.run_status("adhoc-azure-probe", tmp_path, {}, blocked, "20260915")["blocked_count"] == 0
    assert dashboard.run_status("adhoc-azure-probe", tmp_path, {}, blocked, "20260814")["blocked_count"] == 1


def test_run_status_links_where_the_data_is_saved(tmp_path: Path, monkeypatch) -> None:
    run_id = "monthly-aws-20260915"
    command = ["driver", "--bucket-name", "nsf-2148275-66720-scamper-measurements",
               "--object-prefix", "runs/monthly/20260915/aws"]
    write_json(tmp_path / f"jobs/{run_id}/job.json", {"provider": "aws", "campaign_command": command})
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "inactive", "sub": "dead", "since": "", "exit_code": "0"})
    results = dashboard.run_status(run_id, tmp_path, {}, {})["results"]
    assert results["bucket"] == "nsf-2148275-66720-scamper-measurements"
    assert results["prefix"] == "runs/monthly/20260915/aws"
    assert results["uri"] == "gs://nsf-2148275-66720-scamper-measurements/runs/monthly/20260915/aws"
    assert results["console"] == (
        "https://console.cloud.google.com/storage/browser/"
        "nsf-2148275-66720-scamper-measurements/runs/monthly/20260915/aws"
    )


def test_results_location_is_empty_without_a_bucket() -> None:
    assert dashboard.results_location([]) == {}
    assert dashboard.results_location(["driver", "--regions", "us-east-1"]) == {}


def test_dashboard_links_the_results_bucket() -> None:
    assert "<th>Results</th>" in dashboard.INDEX
    assert 'rel="noopener"' in dashboard.INDEX
    assert 'id="bucket"' in dashboard.INDEX


def test_already_submitted_record_keeps_the_previous_outcome(tmp_path: Path, monkeypatch) -> None:
    run_id = "monthly-aws-20260915"
    write_json(tmp_path / f"jobs/{run_id}/job.json", {"provider": "aws", "campaign_command": []})
    write_json(tmp_path / "monthly/20260915.json", {"results": [
        {"provider": "aws", "run_id": run_id, "status": "already-submitted", "previous_status": "failed"},
    ]})
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "inactive", "sub": "dead", "since": "", "exit_code": ""})
    assert dashboard.run_status(run_id, tmp_path, {}, {})["state"] == "failed"


def test_already_submitted_without_a_previous_outcome_is_not_invented(tmp_path: Path, monkeypatch) -> None:
    run_id = "monthly-gcp-20260915"
    write_json(tmp_path / f"jobs/{run_id}/job.json", {"provider": "gcp", "campaign_command": []})
    write_json(tmp_path / "monthly/20260915.json", {"results": [
        {"provider": "gcp", "run_id": run_id, "status": "already-submitted"},
    ]})
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "inactive", "sub": "dead", "since": "", "exit_code": ""})
    assert dashboard.run_status(run_id, tmp_path, {}, {})["state"] == "submitted"


def test_instance_details_report_each_worker_phase(tmp_path: Path) -> None:
    run_id = "monthly-aws-20260915-retry1"
    logs = tmp_path / "jobs" / run_id / "logs"
    logs.mkdir(parents=True)
    (logs / f"{run_id}-af-south-1a-15.240.168.115.log").write_text(
        "apt-get update\nRunning scamper smoke test for aws against 8.8.8.8\n"
        "SCAMPER_SMOKE_OK target=8.8.8.8 hops=5\nSCAMPER_SMOKE_OK target=2606:4700:4700::1111 hops=5\n"
        "SCAMPER_COMMAND[trace]=scamper -c 'trace'\n"
    )
    (logs / f"{run_id}-us-east-1a-44.1.2.3.log").write_text("apt-get update\napt install -y scamper\n")
    (logs / f"{run_id}-eu-west-3a-52.9.9.9.log").write_text("No campaign artifacts were produced\n")
    (logs / f"{run_id}.log").write_text("Creating Instance in af-south-1\n")
    items = dashboard.instance_details(tmp_path / "jobs" / run_id, run_id)
    by_location = {item["location"]: item for item in items}
    assert by_location["af-south-1a"]["phase"] == "measuring"
    assert by_location["af-south-1a"]["smoke_ok"] == 2
    assert by_location["af-south-1a"]["address"] == "15.240.168.115"
    assert by_location["us-east-1a"]["phase"] == "installing"
    assert by_location["eu-west-3a"]["phase"] == "failed"
    # The campaign's own log must not be mistaken for a worker.
    assert run_id not in by_location


def test_instance_details_handle_gcp_zone_names(tmp_path: Path) -> None:
    run_id = "monthly-gcp-20260915"
    logs = tmp_path / "jobs" / run_id / "logs"
    logs.mkdir(parents=True)
    (logs / f"{run_id}-africa-south1-a-34.1.218.192.log").write_text("SCAMPER_COMMAND[trace]=x\n")
    items = dashboard.instance_details(tmp_path / "jobs" / run_id, run_id)
    assert items[0]["location"] == "africa-south1-a"
    assert items[0]["address"] == "34.1.218.192"


def test_run_detail_summarises_phases(tmp_path: Path, monkeypatch) -> None:
    run_id = "monthly-azure-20260915-part1"
    logs = tmp_path / "jobs" / run_id / "logs"
    logs.mkdir(parents=True)
    write_json(tmp_path / f"jobs/{run_id}/job.json", {"provider": "azure", "campaign_command": []})
    (logs / f"{run_id}-eastus-20.1.1.1.log").write_text("SCAMPER_COMMAND[trace]=x\n")
    (logs / f"{run_id}-uksouth-20.2.2.2.log").write_text("SCAMPER_COMMAND[trace]=x\n")
    (logs / f"{run_id}-japaneast-20.3.3.3.log").write_text("apt-get update\n")
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "active", "sub": "running", "since": "now", "exit_code": "0"})
    detail = dashboard.run_detail(run_id, tmp_path)
    assert detail["instance_count"] == 3
    assert detail["phases"] == {"measuring": 2, "installing": 1}
    assert detail["run"]["state"] == "running"


def test_run_detail_endpoint_rejects_path_traversal() -> None:
    assert dashboard.RUN_ID_PATTERN.fullmatch("monthly-aws-20260915-retry1")
    assert not dashboard.RUN_ID_PATTERN.fullmatch("../../etc/passwd")
    assert not dashboard.RUN_ID_PATTERN.fullmatch("run/../../x")
    assert not dashboard.RUN_ID_PATTERN.fullmatch("Run-With-Caps")


def test_run_row_carries_measured_progress(tmp_path: Path, monkeypatch) -> None:
    run_id = "monthly-aws-20260915-retry1"
    write_json(tmp_path / f"jobs/{run_id}/job.json", {"provider": "aws", "campaign_command": []})
    write_json(tmp_path / f"jobs/{run_id}/progress.json", {
        "fraction": 0.0412, "slowest": 0.0406, "fastest": 0.0416, "measurement": "trace",
        "measuring": 27, "probed_at": "2026-09-15T23:20:00+00:00", "warts_bytes": 5_000_000_000,
    })
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "active", "sub": "running", "since": "now", "exit_code": "0"})
    progress = dashboard.run_status(run_id, tmp_path, {}, {})["progress"]
    assert progress["fraction"] == 0.0412
    assert progress["measurement"] == "trace"
    assert progress["measuring"] == 27


def test_run_row_has_no_progress_before_the_prober_runs(tmp_path: Path, monkeypatch) -> None:
    run_id = "monthly-gcp-20260915"
    write_json(tmp_path / f"jobs/{run_id}/job.json", {"provider": "gcp", "campaign_command": []})
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "active", "sub": "running", "since": "now", "exit_code": "0"})
    assert dashboard.run_status(run_id, tmp_path, {}, {})["progress"] == {}
    write_json(tmp_path / f"jobs/{run_id}/progress.json", {"fraction": None, "workers": []})
    assert dashboard.run_status(run_id, tmp_path, {}, {})["progress"] == {}


def test_run_detail_merges_measured_progress_per_worker(tmp_path: Path, monkeypatch) -> None:
    run_id = "monthly-aws-20260915-retry1"
    logs = tmp_path / "jobs" / run_id / "logs"
    logs.mkdir(parents=True)
    write_json(tmp_path / f"jobs/{run_id}/job.json", {"provider": "aws", "campaign_command": []})
    (logs / f"{run_id}-af-south-1a-15.240.168.115.log").write_text("SCAMPER_COMMAND[trace]=x\n")
    (logs / f"{run_id}-us-east-1a-44.1.2.3.log").write_text("SCAMPER_COMMAND[trace]=x\n")
    write_json(tmp_path / f"jobs/{run_id}/progress.json", {"fraction": 0.04, "workers": [
        {"address": "15.240.168.115", "state": "measuring", "fraction": 0.0416,
         "measurement": "trace", "warts_bytes": 185572379},
        {"address": "44.1.2.3", "state": "unreachable"},
    ]})
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "active", "sub": "running", "since": "now", "exit_code": "0"})
    detail = dashboard.run_detail(run_id, tmp_path)
    by_address = {item["address"]: item for item in detail["instances"]}
    assert by_address["15.240.168.115"]["fraction"] == 0.0416
    assert by_address["15.240.168.115"]["warts_bytes"] == 185572379
    assert by_address["44.1.2.3"]["probe_state"] == "unreachable"
    assert "fraction" not in by_address["44.1.2.3"]


def test_dashboard_renders_a_progress_column() -> None:
    assert "<th>Progress</th>" in dashboard.INDEX
    assert "progCell" in dashboard.INDEX


def test_dashboard_surfaces_unowned_cloud_resources(tmp_path: Path, monkeypatch) -> None:
    write_json(tmp_path / "orphans.json", {
        "orphan_count": 1, "swept_at": "2026-09-16T00:00:00+00:00", "errors": [],
        "orphans": [{"run_id": "monthly-azure-202609-r2", "resource_group": "monthly-azure-202609-r2"}],
    })
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "inactive", "sub": "dead", "since": "", "exit_code": "0"})
    value = dashboard.dashboard_state(None, tmp_path, tmp_path / "release")
    assert value["orphans"]["count"] == 1
    assert value["orphans"]["runs"] == ["monthly-azure-202609-r2"]


def test_dashboard_reports_a_failed_orphan_sweep(tmp_path: Path, monkeypatch) -> None:
    write_json(tmp_path / "orphans.json", {"orphan_count": 0, "orphans": [], "errors": ["azure: credential expired"]})
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "inactive", "sub": "dead", "since": "", "exit_code": "0"})
    value = dashboard.dashboard_state(None, tmp_path, tmp_path / "release")
    assert value["orphans"]["count"] == 0
    assert value["orphans"]["errors"] == ["azure: credential expired"]


def test_dashboard_has_an_orphan_banner() -> None:
    assert 'id="orphans"' in dashboard.INDEX
    assert "drawOrphans" in dashboard.INDEX


def test_dashboard_uses_plain_status_table() -> None:
    assert "Scamper run status" in dashboard.INDEX
    assert "<table>" in dashboard.INDEX
    assert "flight board" not in dashboard.INDEX
    assert 'id="runs"' in dashboard.INDEX
    assert "linear-gradient" not in dashboard.INDEX
    assert ".join('\\n')" in dashboard.INDEX


def test_dashboard_state_does_not_expose_campaign_command(tmp_path: Path, monkeypatch) -> None:
    write_json(tmp_path / "september15/readiness.json", {"targets": {"trace": {"target_count": 10}}, "providers": {}})
    write_json(tmp_path / "september15/result.json", {"blocked": {}})
    monkeypatch.setattr(dashboard, "systemd_state", lambda unit: {"active": "inactive", "sub": "dead", "since": "", "exit_code": "0"})
    value = dashboard.dashboard_state("20260915", tmp_path, tmp_path / "release")
    assert value["targets"] == {"trace": 10}
    assert "command" not in json.dumps(value)


def test_dashboard_service_is_local_only_and_installed_by_bootstrap() -> None:
    root = Path(__file__).resolve().parents[2]
    unit = (root / "controller/scamper-dashboard.service").read_text(encoding="utf-8")
    bootstrap = (root / "controller/bootstrap.sh").read_text(encoding="utf-8")
    assert "--host 127.0.0.1" in unit
    assert "User=scamper-controller" in unit
    assert "NoNewPrivileges=true" in unit
    assert "scamper-dashboard.service" in bootstrap
