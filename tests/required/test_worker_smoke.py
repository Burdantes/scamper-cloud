from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).parents[2]
SMOKE_SCRIPT = REPO_ROOT / "providers/common/worker/scamper-smoke.sh"
VM_SCRIPTS = [
    REPO_ROOT / "providers/gcp/worker/run-scamper-gcp.sh",
    REPO_ROOT / "providers/aws/worker/run-scamper-aws.sh",
    REPO_ROOT / "legacy/providers/azure/run-scamper-azr.sh",
]
# Worker scripts that a supported driver deploys. Azure is excluded because it
# has no supported driver yet, and its committed script does not invoke a
# campaign runner at all.
SUPPORTED_VM_SCRIPTS = [
    REPO_ROOT / "providers/gcp/worker/run-scamper-gcp.sh",
    REPO_ROOT / "providers/aws/worker/run-scamper-aws.sh",
]


def run_bash(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-lc", command],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def test_smoke_targets_use_gcp_specific_canary() -> None:
    result = run_bash(
        f"source {SMOKE_SCRIPT}; "
        "smoke_target_for_provider gcp; "
        "smoke_target_for_provider aws; "
        "smoke_target_for_provider azr"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["1.1.1.1", "8.8.8.8", "8.8.8.8"]


def test_validate_scamper_smoke_text_accepts_reasonable_trace(tmp_path: Path) -> None:
    trace_text = tmp_path / "trace.txt"
    trace_text.write_text(
        "\n".join(
            [
                "traceroute from 10.0.0.1 to 8.8.8.8",
                " 1  10.0.0.1  0.123 ms",
                " 2  192.0.2.1  1.234 ms",
                " 3  8.8.8.8  8.765 ms",
            ]
        ),
        encoding="utf-8",
    )

    result = run_bash(
        f"source {SMOKE_SCRIPT}; "
        f"validate_scamper_smoke_text {trace_text} 8.8.8.8 2"
    )

    assert result.returncode == 0, result.stderr
    assert "SCAMPER_SMOKE_OK target=8.8.8.8 hops=3" in result.stdout


def test_validate_scamper_smoke_text_rejects_incomplete_trace(tmp_path: Path) -> None:
    trace_text = tmp_path / "trace.txt"
    trace_text.write_text(
        "\n".join(
            [
                "traceroute from 10.0.0.1 to 8.8.8.8",
                " 1  10.0.0.1  0.123 ms",
            ]
        ),
        encoding="utf-8",
    )

    result = run_bash(
        f"source {SMOKE_SCRIPT}; "
        f"validate_scamper_smoke_text {trace_text} 8.8.8.8 2"
    )

    assert result.returncode == 1
    assert "expected at least 2" in result.stderr


def test_vm_scripts_are_valid_bash() -> None:
    for script in [SMOKE_SCRIPT, *VM_SCRIPTS]:
        result = subprocess.run(
            ["bash", "-n", str(script)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_gcp_worker_checkpoints_measurement_artifacts_before_final_status() -> None:
    script = (REPO_ROOT / "providers/gcp/worker/run-scamper-gcp.sh").read_text(
        encoding="utf-8"
    )

    assert "--checkpoint-command" in script
    assert '"$OBJECT_PREFIX/{artifact_name}"' in script
    assert 'if [[ $campaign_status -eq 0 ]]; then' in script
    assert 'artifacts=("$OUTPUT_PREFIX.status.json")' in script


def test_gcp_worker_can_skip_smoke_probe() -> None:
    script = (REPO_ROOT / "providers/gcp/worker/run-scamper-gcp.sh").read_text(
        encoding="utf-8"
    )

    assert '"${SCAMPER_SKIP_SMOKE:-0}" == "1"' in script
    assert "Skipping Scamper smoke test by request" in script


def test_worker_scripts_only_reference_files_that_are_deployed() -> None:
    """A worker script must not invoke a helper that does not exist.

    Drivers scp the files named in settings onto the VM and flatten them, so a
    script referring to ./something.py is referring to one of those basenames.
    The committed AWS script invoked ./run-scamper-campaign.py, which exists
    nowhere in this repository - the only runner is
    experiments/common/run_campaign.py. Bash syntax checks pass happily on that,
    so nothing caught it and the deployed worker had to be patched by hand.
    """
    import re

    from providers import settings

    deployed = {
        Path(value).name: REPO_ROOT / Path(value)
        for value in (
            settings.SCAMPER_UPLOAD_SCRIPT,
            settings.SCAMPER_SMOKE_SCRIPT,
            settings.SCAMPER_CAMPAIGN_RUNNER,
            settings.GCP_SCAMPER_SCRIPT,
            settings.AWS_SCAMPER_VM_SCRIPT,
        )
    }
    for name, path in deployed.items():
        assert path.is_file(), f"settings names {name} but {path} does not exist"

    for script in SUPPORTED_VM_SCRIPTS:
        text = script.read_text(encoding="utf-8")
        referenced = set(re.findall(r"\./([A-Za-z0-9_.-]+\.py)", text))
        referenced |= set(re.findall(r'\$SCRIPT_DIR/([A-Za-z0-9_.-]+\.sh)', text))
        missing = sorted(referenced - deployed.keys())
        assert not missing, (
            f"{script.name} references {missing}, which no driver deploys; "
            f"deployed basenames are {sorted(deployed)}"
        )


def test_aws_worker_invokes_the_shared_campaign_runner() -> None:
    """AWS must use the same provider-neutral runner as GCP, by its real name."""
    from providers import settings

    text = (REPO_ROOT / "providers/aws/worker/run-scamper-aws.sh").read_text(
        encoding="utf-8"
    )
    runner = Path(settings.SCAMPER_CAMPAIGN_RUNNER).name
    assert runner == "run_campaign.py"
    assert f"./{runner}" in text
    assert "run-scamper-campaign.py" not in text
    # The runner's required arguments must all be supplied.
    for required in ("--output-prefix", "--provider", "--region", "--node"):
        assert required in text, f"AWS worker omits required {required}"
