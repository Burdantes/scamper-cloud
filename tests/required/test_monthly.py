from __future__ import annotations

import json
from pathlib import Path

import pytest

from controller import monthly


def write_config(path: Path, *, enabled: bool = True) -> Path:
    value = {
        "schema_version": 1,
        "enabled": enabled,
        "trace_target_id": "sha256:" + "1" * 64,
        "rr_target_id": "sha256:" + "2" * 64,
        "bucket": "measurement-results",
        "measurements": ["trace", "rr"],
        "trace_rate": 1000,
        "rr_rate": 100,
        "rr_timeout": 2.0,
        "probe_payload": "Academic measurement",
        "measurement_contact": "research@example.edu",
        "do_not_probe_file": str(path.parent / "do-not-probe.txt"),
        "providers": {
            "gcp": {
                "regions": ["us-central1"],
                "worker_machine_type": "e2-micro",
                "max_instances": 1,
                "max_targets": None,
            },
            "aws": {
                "regions": ["us-east-1"],
                "worker_machine_type": "t3.micro",
                "max_instances": 1,
                "max_targets": None,
            },
            "azure": {
                "regions": ["eastus"],
                "worker_machine_type": "Standard_B2ts_v2",
                "max_instances": 1,
                "max_targets": None,
            },
        },
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    (path.parent / "do-not-probe.txt").write_text("# empty\n", encoding="utf-8")
    return path


def test_schedule_requires_every_supported_provider(tmp_path: Path) -> None:
    path = write_config(tmp_path / "monthly.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    del value["providers"]["aws"]
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match=r"missing=\['aws'\]"):
        monthly.load_schedule(path)


def test_schedule_has_positive_cost_caps_and_distinct_target_ids(tmp_path: Path) -> None:
    schedule = monthly.load_schedule(write_config(tmp_path / "monthly.json"))

    assert {provider.provider for provider in schedule.providers} == {"gcp", "aws", "azure"}
    assert all(provider.max_instances == 1 for provider in schedule.providers)
    assert schedule.trace_target_id != schedule.rr_target_id


def test_schema_two_accepts_trace6_with_an_independent_cap(tmp_path: Path) -> None:
    path = write_config(tmp_path / "monthly.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    value.update(
        {
            "schema_version": 2,
            "trace6_target_id": "sha256:" + "3" * 64,
            "measurements": ["trace", "trace6", "rr"],
            "trace6_rate": 250,
        }
    )
    for provider in value["providers"].values():
        provider["max_trace6_targets"] = 1000
    path.write_text(json.dumps(value), encoding="utf-8")

    schedule = monthly.load_schedule(path)
    arguments = monthly._submission_args(schedule, schedule.providers[0], "202610")

    assert schedule.trace6_target_id == "sha256:" + "3" * 64
    assert arguments[arguments.index("--trace6-rate") + 1] == "250"
    assert arguments[arguments.index("--max-trace6-targets") + 1] == "1000"
    assert "--trace6-targets" in arguments


def test_dispatch_submits_each_provider_once_per_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schedule = monthly.load_schedule(write_config(tmp_path / "monthly.json"))
    state_root = tmp_path / "monthly-state"
    jobs_root = tmp_path / "controller-state"
    calls: list[list[str]] = []

    monkeypatch.setattr(monthly, "STATE_ROOT", state_root)
    monkeypatch.setattr(monthly.submit, "STATE_ROOT", jobs_root)
    monkeypatch.setattr(monthly, "readiness", lambda _schedule: {"ready": True, "errors": []})

    def fake_submit(arguments: list[str]) -> int:
        calls.append(arguments)
        run_id = arguments[arguments.index("--run-id") + 1]
        job_dir = jobs_root / "jobs" / run_id
        job_dir.mkdir(parents=True)
        (job_dir / "job.json").write_text("{}", encoding="utf-8")
        return 0

    monkeypatch.setattr(monthly.submit, "main", fake_submit)

    first = monthly.dispatch(schedule, cycle="202609")
    second = monthly.dispatch(schedule, cycle="202609")

    assert len(calls) == 3
    assert {call[call.index("--provider") + 1] for call in calls} == {
        "gcp",
        "aws",
        "azure",
    }
    assert all("--max-instances" in call for call in calls)
    assert all(result["status"] == "submitted" for result in first["results"])
    assert all(result["status"] == "already-submitted" for result in second["results"])


def test_readiness_fails_closed_when_aws_credentials_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schedule = monthly.load_schedule(write_config(tmp_path / "monthly.json"))
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "missing-aws"))
    monkeypatch.setattr(monthly, "missing_worker_assets", lambda _provider: [])
    monkeypatch.setattr(monthly, "remote_target_path", lambda target: tmp_path / f"{target[-1]}.txt")
    monkeypatch.setattr(monthly, "load_registered_target", lambda _path: None)

    report = monthly.readiness(schedule)

    assert report["ready"] is False
    assert any("AWS credentials" in error for error in report["errors"])
    assert any("target ID is not registered" in error for error in report["errors"])


def test_systemd_timer_is_persistent_and_monthly() -> None:
    root = Path(__file__).resolve().parents[2]
    timer = (root / "controller/scamper-monthly.timer").read_text(encoding="utf-8")
    service = (root / "controller/scamper-monthly.service").read_text(encoding="utf-8")
    wrapper = (root / "controller/run-monthly").read_text(encoding="utf-8")

    assert "OnCalendar=*-*-01" in timer
    assert "Persistent=true" in timer
    assert "scamper-controller-monthly run" in service
    assert "cd /opt/scamper-cloud/current" in wrapper
    assert "python -m controller.monthly" in wrapper
