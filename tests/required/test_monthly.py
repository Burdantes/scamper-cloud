from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from controller import monthly


@pytest.fixture(autouse=True)
def isolated_launch_checks(monkeypatch):
    monkeypatch.setattr(monthly, "_launch_readiness", lambda _provider: {"ready": True, "errors": []})


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
                "campaign_timeout_seconds": 172800,
            },
            "aws": {
                "regions": ["us-east-1"],
                "worker_machine_type": "t3.micro",
                "max_instances": 1,
                "max_targets": None,
                "campaign_timeout_seconds": 86400,
            },
            "azure": {
                "regions": ["eastus"],
                "worker_machine_type": "Standard_B2ts_v2",
                "max_instances": 1,
                "max_targets": None,
                "campaign_timeout_seconds": 86400,
            },
        },
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    (path.parent / "do-not-probe.txt").write_text("# empty\n", encoding="utf-8")
    return path


def test_regional_launch_errors_are_warnings_when_an_eligible_region_remains() -> None:
    failures, warnings = monthly._launch_failures_and_warnings(
        {"errors": ["west: unavailable"], "eligible_regions": ["east"]}
    )
    assert failures == []
    assert warnings == ["west: unavailable"]


def test_eligible_provider_drops_failed_regions_and_their_overrides() -> None:
    provider = monthly.ProviderSchedule(
        "azure", ("east", "west"), "small", 2, None, None, 86400,
        worker_machine_types_by_region={"east": "small", "west": "large"},
        worker_image_versions_by_region={"east": "1", "west": "2"},
    )
    effective = monthly._eligible_provider(
        provider, {"eligible_regions": ["east"]}
    )
    assert effective.regions == ("east",)
    assert effective.max_instances == 1
    assert effective.worker_machine_types_by_region == {"east": "small"}
    assert effective.worker_image_versions_by_region == {"east": "1"}


def test_schedule_requires_every_supported_provider(tmp_path: Path) -> None:
    path = write_config(tmp_path / "monthly.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    del value["providers"]["aws"]
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match=r"missing=\['aws'\]"):
        monthly.load_schedule(path)


def test_schedule_has_positive_cost_caps_and_distinct_target_ids(
    tmp_path: Path,
) -> None:
    schedule = monthly.load_schedule(write_config(tmp_path / "monthly.json"))

    assert {provider.provider for provider in schedule.providers} == {
        "gcp",
        "aws",
        "azure",
    }
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
    assert arguments[arguments.index("--campaign-timeout-seconds") + 1] == str(
        schedule.providers[0].campaign_timeout_seconds
    )
    assert "--wait-for-completion" in arguments
    assert "--trace6-targets" in arguments


def test_readiness_rejects_workload_that_cannot_fit_provider_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path / "monthly.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    value.update(
        {
            "schema_version": 2,
            "trace6_target_id": "sha256:" + "3" * 64,
            "measurements": ["trace", "trace6", "rr"],
        }
    )
    for provider in value["providers"].values():
        provider["campaign_timeout_seconds"] = 3600
        provider["max_trace6_targets"] = 1000
    path.write_text(json.dumps(value), encoding="utf-8")
    schedule = monthly.load_schedule(path)

    paths = {
        schedule.trace_target_id: tmp_path / "trace.targets.txt",
        schedule.rr_target_id: tmp_path / "rr.targets.txt",
        schedule.trace6_target_id: tmp_path / "trace6.targets.txt",
    }
    for target_path in paths.values():
        target_path.write_text("registered\n", encoding="utf-8")
    counts = {
        "trace.targets.txt": 100_000,
        "rr.targets.txt": 1_000,
        "trace6.targets.txt": 1_000,
    }
    families = {"trace.targets.txt": 4, "rr.targets.txt": 4, "trace6.targets.txt": 6}

    monkeypatch.setattr(monthly, "remote_target_path", paths.__getitem__)
    monkeypatch.setattr(
        monthly,
        "load_registered_target",
        lambda target_path: SimpleNamespace(
            target_id=next(key for key, value in paths.items() if value == target_path),
            target_count=counts[target_path.name],
            normalized_sha256="a" * 64,
            address_family=families[target_path.name],
        ),
    )
    monkeypatch.setattr(monthly, "missing_worker_assets", lambda _provider: [])
    monkeypatch.setattr(monthly, "_credential_errors", lambda _provider, _regions: [])

    report = monthly.readiness(schedule)

    assert report["ready"] is False
    assert all(
        provider["estimated_runtime_seconds"] > provider["campaign_timeout_seconds"]
        for provider in report["providers"].values()
    )
    assert any("estimated workload runtime" in error for error in report["errors"])


def test_readiness_applies_target_caps_before_runtime_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path / "monthly.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    for provider in value["providers"].values():
        provider["campaign_timeout_seconds"] = 3600
        provider["max_targets"] = 100
    path.write_text(json.dumps(value), encoding="utf-8")
    schedule = monthly.load_schedule(path)

    paths = {
        schedule.trace_target_id: tmp_path / "trace.targets.txt",
        schedule.rr_target_id: tmp_path / "rr.targets.txt",
    }
    for target_path in paths.values():
        target_path.write_text("registered\n", encoding="utf-8")
    monkeypatch.setattr(monthly, "remote_target_path", paths.__getitem__)
    monkeypatch.setattr(
        monthly,
        "load_registered_target",
        lambda target_path: SimpleNamespace(
            target_id=next(key for key, value in paths.items() if value == target_path),
            target_count=1_000_000,
            normalized_sha256="a" * 64,
            address_family=4,
        ),
    )
    monkeypatch.setattr(monthly, "missing_worker_assets", lambda _provider: [])
    monkeypatch.setattr(monthly, "_credential_errors", lambda _provider, _regions: [])

    report = monthly.readiness(schedule)

    assert report["ready"] is True
    assert all(
        provider["estimated_runtime_seconds"] < provider["campaign_timeout_seconds"]
        for provider in report["providers"].values()
    )


def test_dispatch_submits_each_provider_once_per_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schedule = monthly.load_schedule(write_config(tmp_path / "monthly.json"))
    state_root = tmp_path / "monthly-state"
    jobs_root = tmp_path / "controller-state"
    calls: list[list[str]] = []

    monkeypatch.setattr(monthly, "STATE_ROOT", state_root)
    monkeypatch.setattr(monthly.submit, "STATE_ROOT", jobs_root)
    monkeypatch.setattr(
        monthly, "readiness", lambda _schedule: {"ready": True, "errors": []}
    )

    def fake_submit(arguments: list[str]) -> int:
        calls.append(arguments)
        run_id = arguments[arguments.index("--run-id") + 1]
        job_dir = jobs_root / "jobs" / run_id
        job_dir.mkdir(parents=True)
        (job_dir / "job.json").write_text("{}", encoding="utf-8")
        return 0

    monkeypatch.setattr(monthly.submit, "main", fake_submit)

    first = monthly.dispatch(schedule, cycle="20260904")
    second = monthly.dispatch(schedule, cycle="20260904")

    assert len(calls) == 3
    assert {call[call.index("--provider") + 1] for call in calls} == {
        "gcp",
        "aws",
        "azure",
    }
    assert all("--max-instances" in call for call in calls)
    assert {
        call[call.index("--run-id") + 1] for call in calls
    } == {
        "monthly-aws-20260904",
        "monthly-azure-20260904",
        "monthly-gcp-20260904",
    }
    assert all(
        call[call.index("--object-prefix") + 1].startswith("runs/monthly/20260904/")
        for call in calls
    )
    assert all(result["status"] == "completed" for result in first["results"])
    assert all(result["status"] == "already-submitted" for result in second["results"])


@pytest.mark.parametrize("value", ["20269", "2026090", "202609040", "2026-09"])
def test_cycle_label_rejects_ambiguous_namespaces(value: str) -> None:
    with pytest.raises(ValueError, match="YYYYMM or YYYYMMDD"):
        monthly.cycle_label(value)


def test_dispatch_waits_for_each_provider_and_continues_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schedule = monthly.load_schedule(write_config(tmp_path / "monthly.json"))
    state_root = tmp_path / "monthly-state"
    jobs_root = tmp_path / "controller-state"
    calls: list[str] = []

    monkeypatch.setattr(monthly, "STATE_ROOT", state_root)
    monkeypatch.setattr(monthly.submit, "STATE_ROOT", jobs_root)
    monkeypatch.setattr(
        monthly, "readiness", lambda _schedule: {"ready": True, "errors": []}
    )

    def fake_submit(arguments: list[str]) -> int:
        provider = arguments[arguments.index("--provider") + 1]
        run_id = arguments[arguments.index("--run-id") + 1]
        assert "--wait-for-completion" in arguments
        calls.append(provider)
        job_dir = jobs_root / "jobs" / run_id
        job_dir.mkdir(parents=True)
        (job_dir / "job.json").write_text("{}", encoding="utf-8")
        if provider == "aws":
            raise subprocess.CalledProcessError(1, ["systemd-run", "--wait"])
        return 0

    monkeypatch.setattr(monthly.submit, "main", fake_submit)

    state = monthly.dispatch(schedule, cycle="202609")

    assert calls == ["aws", "azure", "gcp"]
    assert [result["status"] for result in state["results"]] == [
        "failed",
        "completed",
        "completed",
    ]
    assert state["results"][0]["exit_code"] == 1


def test_readiness_fails_closed_when_aws_credentials_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schedule = monthly.load_schedule(write_config(tmp_path / "monthly.json"))
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "missing-aws"))
    monkeypatch.setattr(monthly, "missing_worker_assets", lambda _provider: [])
    monkeypatch.setattr(
        monthly, "remote_target_path", lambda target: tmp_path / f"{target[-1]}.txt"
    )
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
    assert "TimeoutStartSec=infinity" in service
    assert "cd /opt/scamper-cloud/current" in wrapper
    assert "python -m controller.monthly" in wrapper


def test_controller_can_register_only_a_new_ipv6_target() -> None:
    args = monthly.build_parser().parse_args(
        ["register-targets", "--trace6-targets", "/tmp/trace6.txt"]
    )

    assert args.trace_targets is None
    assert args.rr_targets is None
    assert args.trace6_targets == Path("/tmp/trace6.txt")


def test_monthly_image_and_regional_size_are_forwarded(tmp_path):
    path = write_config(tmp_path / 'monthly.json')
    value = json.loads(path.read_text())
    value['providers']['gcp'].update(worker_image_project='debian-cloud',worker_image_family='debian-12')
    value['providers']['azure']['worker_machine_types_by_region'] = {'eastus':'Standard_B2s'}
    path.write_text(json.dumps(value))
    schedule = monthly.load_schedule(path)
    gcp = next(p for p in schedule.providers if p.provider == 'gcp')
    args = monthly._submission_args(schedule,gcp,'20260912')
    assert args[args.index('--worker-image-family')+1] == 'debian-12'
    azure = next(p for p in schedule.providers if p.provider == 'azure')
    args = monthly._submission_args(schedule,azure,'20260912')
    assert json.loads(args[args.index('--worker-machine-types-json')+1]) == {'eastus':'Standard_B2s'}


@pytest.mark.parametrize('regions,cap', [(['us-east-1','us-east-1'],2),(['us-east-1'],2),([],1)])
def test_monthly_rejects_ambiguous_coverage(tmp_path, regions, cap):
    path=write_config(tmp_path/'monthly.json')
    value=json.loads(path.read_text())
    value['providers']['aws'].update(regions=regions,max_instances=cap)
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError): monthly.load_schedule(path)


def test_dispatch_revalidates_and_pins_image_before_each_job(tmp_path, monkeypatch):
    schedule=monthly.load_schedule(write_config(tmp_path/'monthly.json'))
    monkeypatch.setattr(monthly,'STATE_ROOT',tmp_path/'state')
    monkeypatch.setattr(monthly.submit,'STATE_ROOT',tmp_path/'jobs')
    monkeypatch.setattr(monthly,'readiness',lambda _: {'ready':True,'errors':[]})
    events=[]
    def check(provider):
        events.append('check-'+provider.provider)
        return {'errors':[],'worker_image':'debian-12-fixed','worker_image_project':'debian-cloud'}
    def submit(args):
        provider=args[args.index('--provider')+1]
        events.append('submit-'+provider)
        if provider=='gcp': assert args[args.index('--worker-image')+1]=='debian-12-fixed'
        if provider=='aws': return 1
        return 0
    monkeypatch.setattr(monthly,'_launch_readiness',check)
    monkeypatch.setattr(monthly.submit,'main',submit)
    result=monthly.dispatch(schedule,cycle='20260912')
    assert events==['check-aws','submit-aws','check-azure','submit-azure','check-gcp','submit-gcp']
    assert result['complete'] is False
    assert json.loads((tmp_path/'state/20260912.json').read_text())['complete'] is False


def test_monthly_main_returns_failure_for_failed_campaign(tmp_path, monkeypatch):
    path=write_config(tmp_path/'monthly.json')
    monkeypatch.setattr(monthly,'dispatch',lambda *a,**k:{'complete':False,'results':[{'status':'failed'}]})
    assert monthly.main(['--config',str(path),'run']) == 1
