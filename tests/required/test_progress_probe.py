from __future__ import annotations

import json
from pathlib import Path

from controller import progress_probe


REAL_OUTPUT = """POS /home/ubuntu/results/run-af-south-1a-1.2.3.4.trace.targets.txt 7212032 173236528
WARTS results/run-af-south-1a-1.2.3.4.trace.warts 185572379
"""


def write_job(root: Path, run_id: str, provider: str, workers: list[tuple[str, str]]) -> Path:
    job_dir = root / "jobs" / run_id
    (job_dir / "logs").mkdir(parents=True)
    (job_dir / "job.json").write_text(json.dumps({"provider": provider}), encoding="utf-8")
    for location, address in workers:
        (job_dir / "logs" / f"{run_id}-{location}-{address}.log").write_text("x\n", encoding="utf-8")
    return job_dir


def test_parse_probe_reads_the_scamper_target_offset() -> None:
    value = progress_probe.parse_probe(REAL_OUTPUT)
    assert value["state"] == "measuring"
    assert value["measurement"] == "trace"
    assert value["position"] == 7212032
    assert value["size"] == 173236528
    assert value["fraction"] == round(7212032 / 173236528, 6)
    assert value["warts_bytes"] == 185572379


def test_parse_probe_handles_a_worker_with_no_scamper() -> None:
    assert progress_probe.parse_probe("NOSCAMPER\n") == {"state": "no-scamper"}


def test_parse_probe_distinguishes_silence_from_a_running_scamper() -> None:
    # Empty output means the SSH attempt produced nothing, not that scamper is
    # running without a target file; conflating the two hid ten dead workers.
    assert progress_probe.parse_probe("")["state"] == "unreachable"
    assert progress_probe.parse_probe("WARTS results/x.warts 5\n")["state"] == "unknown"


def test_remote_script_does_not_match_its_own_shell() -> None:
    # pgrep -f 'scamper -c' matched the bash wrapper carrying that same text, so
    # a worker with no scamper reported "unknown" instead of "no-scamper".
    assert "pgrep -x scamper" in progress_probe.REMOTE_SCRIPT
    assert "pgrep -f" not in progress_probe.REMOTE_SCRIPT


def test_parse_probe_survives_a_zero_sized_target_file() -> None:
    assert progress_probe.parse_probe("POS /x/y.trace.targets.txt 0 0\n")["state"] == "unknown"


def test_workers_come_from_instance_log_names(tmp_path: Path) -> None:
    write_job(tmp_path, "run", "aws", [("af-south-1a", "1.2.3.4"), ("us-east-1a", "5.6.7.8")])
    (tmp_path / "jobs/run/logs/run.log").write_text("campaign log\n", encoding="utf-8")
    assert progress_probe.workers_of(tmp_path / "jobs/run", "run") == [
        ("af-south-1a", "1.2.3.4"), ("us-east-1a", "5.6.7.8"),
    ]


def test_probe_run_aggregates_and_picks_the_provider_ssh_user(tmp_path: Path) -> None:
    write_job(tmp_path, "run", "azure", [("eastus", "1.1.1.1"), ("uksouth", "2.2.2.2")])
    seen = []

    def runner(user, address, script, timeout=25):
        seen.append((user, address))
        offset = 8000000 if address == "1.1.1.1" else 4000000
        return f"POS /r/x.trace.targets.txt {offset} 100000000\nWARTS results/x.trace.warts 5\n"

    value = progress_probe.probe_run("run", tmp_path, runner=runner)
    assert [user for user, _ in seen] == ["azureuser", "azureuser"]
    assert value["measuring"] == 2
    assert value["fraction"] == 0.06
    assert value["slowest"] == 0.04
    assert value["fastest"] == 0.08
    assert value["measurement"] == "trace"
    assert value["warts_bytes"] == 10


def test_probe_run_records_an_unreachable_worker_without_failing(tmp_path: Path) -> None:
    write_job(tmp_path, "run", "aws", [("a", "1.1.1.1"), ("b", "2.2.2.2")])

    def runner(user, address, script, timeout=25):
        if address == "2.2.2.2":
            raise OSError("connection refused")
        return "POS /r/x.rr.targets.txt 50 100\n"

    value = progress_probe.probe_run("run", tmp_path, runner=runner)
    states = {item["address"]: item["state"] for item in value["workers"]}
    assert states["1.1.1.1"] == "measuring"
    assert states["2.2.2.2"] == "unreachable"
    assert value["measuring"] == 1
    assert value["fraction"] == 0.5


def test_probe_run_stops_at_its_time_budget(tmp_path: Path) -> None:
    write_job(tmp_path, "run", "aws", [("a", "1.1.1.1"), ("b", "2.2.2.2")])
    value = progress_probe.probe_run("run", tmp_path, runner=lambda *a, **k: "", deadline=-1.0)
    assert [item["state"] for item in value["workers"]] == ["skipped-budget", "skipped-budget"]
    assert value["fraction"] is None


def test_write_progress_replaces_atomically(tmp_path: Path) -> None:
    job_dir = tmp_path / "jobs/run"
    job_dir.mkdir(parents=True)
    progress_probe.write_progress(job_dir, {"run_id": "run", "fraction": 0.5})
    progress_probe.write_progress(job_dir, {"run_id": "run", "fraction": 0.6})
    assert json.loads((job_dir / "progress.json").read_text())["fraction"] == 0.6
    assert not (job_dir / "progress.tmp").exists()


def test_probe_units_are_installed_and_bounded() -> None:
    root = Path(__file__).resolve().parents[2]
    service = (root / "controller/scamper-progress.service").read_text(encoding="utf-8")
    timer = (root / "controller/scamper-progress.timer").read_text(encoding="utf-8")
    bootstrap = (root / "controller/bootstrap.sh").read_text(encoding="utf-8")
    assert "User=scamper-controller" in service
    assert "RuntimeMaxSec=" in service
    assert "ReadWritePaths=/var/lib/scamper-controller" in service
    assert "OnUnitActiveSec=30min" in timer
    assert "scamper-progress.service" in bootstrap
    assert "scamper-progress.timer" in bootstrap
