"""The pre-measurement check: a failing preflight stops that one measurement.

On the 2026-10-12 Azure run, 38 of 39 workers passed the launch IPv6 smoke
test, then ~18 h later every trace6 probe failed to bind and empty chunks were
committed with rc=0. The preflight re-checks right before each measurement.
"""
from __future__ import annotations

import gzip
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "experiments/common"))
import run_campaign as rc  # noqa: E402

SMOKE_SCRIPT = REPO_ROOT / "providers/common/worker/scamper-smoke.sh"
WORKER_SCRIPTS = {
    "gcp": REPO_ROOT / "providers/gcp/worker/run-scamper-gcp.sh",
    "aws": REPO_ROOT / "providers/aws/worker/run-scamper-aws.sh",
    "azr": REPO_ROOT / "providers/azure/worker/run-scamper-azr.sh",
}


def _targets(path: Path, family: int, count: int = 4) -> Path:
    if family == 4:
        lines = [f"10.0.0.{i}" for i in range(count)]
    else:
        lines = [f"2001:db8::{i + 1:x}" for i in range(count)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _ok_convert(warts_path, jsonl_path, measurement, stderr_path):
    with gzip.open(jsonl_path, "wt", encoding="utf-8") as handle:
        handle.write('{"type":"trace"}\n')
    return 0, "", {"records": 1}


def _fake_shuffle(source, destination, seed=None):
    Path(destination).write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")
    return seed or 1, ["shuf"]


def _run_main(tmp_path: Path, monkeypatch, *, failing: set[str]):
    calls: list[tuple[str, list[str]]] = []

    def fake_run(command, check=False, **kwargs):
        if command[0] == "preflight":
            calls.append(("preflight", command))
            code = 1 if command[1] in failing else 0
        elif command[0] == "upload":
            calls.append(("upload", command))
            code = 0
        else:
            calls.append(("scamper", command))
            Path(command[command.index("-o") + 1]).write_text("warts", encoding="utf-8")
            code = 0
        return subprocess.CompletedProcess(command, code)

    monkeypatch.setattr(rc.subprocess, "run", fake_run)
    monkeypatch.setattr(rc, "convert_chunk_to_jsonl", _ok_convert)
    monkeypatch.setattr(rc, "shuffle_targets", _fake_shuffle)
    v4 = _targets(tmp_path / "v4.txt", 4)
    v6 = _targets(tmp_path / "v6.txt", 6)
    code = rc.main([
        "--trace-targets", str(v4), "--rr-targets", str(v4), "--trace6-targets", str(v6),
        "--output-prefix", str(tmp_path / "out"), "--provider", "azure", "--region", "r",
        "--node", "n",
        "--trace-target-source", "t", "--trace-target-version", "v1",
        "--rr-target-source", "t", "--rr-target-version", "v1",
        "--trace6-target-source", "t", "--trace6-target-version", "v1",
        "--measurements", "trace,trace6", "--chunk-targets", "2",
        "--preflight-command", "preflight {measurement}",
        "--checkpoint-command", "upload", "{artifact}",
    ])
    return code, calls


def test_a_failed_preflight_skips_only_that_measurement(tmp_path: Path, monkeypatch) -> None:
    code, calls = _run_main(tmp_path, monkeypatch, failing={"trace6"})

    preflights = [command[1] for kind, command in calls if kind == "preflight"]
    assert preflights == ["trace", "trace6"]
    probed = [command for kind, command in calls if kind == "scamper"]
    assert probed and all("trace6" not in " ".join(c) for c in probed), "trace6 must not be probed"
    assert code == rc.PREFLIGHT_FAILED

    status = json.loads((tmp_path / "out.status.json").read_text())
    assert status["complete"] is False
    assert status["measurement_return_codes"] == {"trace": 0, "trace6": rc.PREFLIGHT_FAILED}


def test_a_failed_preflight_commits_no_chunk_and_uploads_its_record(tmp_path: Path, monkeypatch) -> None:
    _code, calls = _run_main(tmp_path, monkeypatch, failing={"trace6"})

    uploaded = [Path(command[1]).name for kind, command in calls if kind == "upload"]
    assert "out.trace6.metadata.json" in uploaded
    assert not any(".trace6." in name and ".part-" in name for name in uploaded), \
        "no trace6 chunk may be committed, so a resume measures it"
    record = json.loads((tmp_path / "out.trace6.metadata.json").read_text())
    assert record["preflight_failed"] is True
    assert record["preflight_command"] == ["preflight", "trace6"]
    assert record["return_code"] == rc.PREFLIGHT_FAILED
    assert record["address_family"] == 6


def test_a_passing_preflight_changes_nothing(tmp_path: Path, monkeypatch) -> None:
    code, calls = _run_main(tmp_path, monkeypatch, failing=set())

    assert code == 0
    probed = " ".join(" ".join(command) for kind, command in calls if kind == "scamper")
    assert "trace6" in probed
    # Each preflight runs right before its own measurement.
    kinds = [kind if kind != "preflight" else f"preflight:{command[1]}" for kind, command in calls]
    assert kinds.index("preflight:trace6") > max(
        i for i, (kind, command) in enumerate(calls)
        if kind == "scamper" and ".trace." in " ".join(command)
    )


def test_without_the_option_nothing_extra_runs(tmp_path: Path, monkeypatch) -> None:
    assert rc.run_preflight(None, "trace6") is None
    assert rc.run_preflight("", "trace6") is None


def _fake_bin(tmp_path: Path, scamper_hops: bool) -> Path:
    """Stand-ins for sudo, scamper, sc_warts2text, ip and curl."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    scripts = {
        "sudo": '#!/bin/sh\nexec "$@"\n',
        "scamper": "#!/bin/sh\nexit 0\n",
        "ip": "#!/bin/sh\necho \"ip $*\"\n",
        "curl": "#!/bin/sh\necho 2001:db8::ffff\n",
        "ip6tables": "#!/bin/sh\nexit 0\n",
        "sc_warts2text": (
            "#!/bin/sh\n"
            "echo 'traceroute from 2001:db8::4 to 2606:4700:4700::1111'\n"
            + ("echo ' 1  2001:db8::1  1.0 ms'\necho ' 2  2001:db8::2  2.0 ms'\n"
               if scamper_hops else "echo ' 1  *'\necho ' 2  *'\n")
        ),
    }
    for name, body in scripts.items():
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return bin_dir


def _preflight(tmp_path: Path, measurement: str, *, hops: bool, **env: str):
    bin_dir = _fake_bin(tmp_path, hops)
    environment = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "SCAMPER_SMOKE_RETRY_SECONDS": "0",
        "SCAMPER_SMOKE_DIR": str(tmp_path / "smoke"),
        **env,
    }
    return subprocess.run(
        ["bash", str(SMOKE_SCRIPT), "preflight", "azr", measurement],
        capture_output=True, text=True, env=environment, check=False,
    )


def test_preflight_passes_ipv4_measurements_through(tmp_path: Path) -> None:
    result = _preflight(tmp_path, "trace", hops=False)
    assert result.returncode == 0, result.stderr
    assert "SCAMPER_PREFLIGHT" not in result.stdout


def test_preflight_rechecks_ipv6_and_logs_state(tmp_path: Path) -> None:
    result = _preflight(tmp_path, "trace6", hops=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SCAMPER_PREFLIGHT_OK measurement=trace6" in result.stdout
    assert "ip -6 route show table all" in result.stdout


def test_preflight_fails_and_logs_state_again_when_ipv6_is_dead(tmp_path: Path) -> None:
    result = _preflight(tmp_path, "trace6", hops=False)
    assert result.returncode == 1
    assert "SCAMPER_PREFLIGHT_FAILED measurement=trace6" in result.stdout
    after = result.stdout.split("SCAMPER_PREFLIGHT_FAILED", 1)[1]
    assert "ip -6 address show" in after and "ip -6 route show table all" in after


def test_preflight_can_be_skipped(tmp_path: Path) -> None:
    result = _preflight(tmp_path, "trace6", hops=False, SCAMPER_SKIP_PREFLIGHT="1")
    assert result.returncode == 0
    assert "SCAMPER_PREFLIGHT_SKIPPED" in result.stdout


def test_every_worker_passes_its_preflight_before_the_checkpoint_command() -> None:
    for provider, path in WORKER_SCRIPTS.items():
        script = path.read_text(encoding="utf-8")
        flag = f'--preflight-command "bash $SCRIPT_DIR/scamper-smoke.sh preflight {provider} {{measurement}}"'
        assert flag in script, path
        # --checkpoint-command consumes the rest of argv, so it must come later.
        assert script.index(flag) < script.rindex("--checkpoint-command"), path
