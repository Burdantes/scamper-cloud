from __future__ import annotations

import argparse
from pathlib import Path

from controller import manage, submit
from providers.gcp.driver import expected_campaign_artifacts


def test_controller_provision_is_us_and_standard() -> None:
    args = argparse.Namespace(
        project="project",
        name="controller",
        zone="us-central1-c",
        machine_type="e2-small",
        boot_disk_size="20GB",
        service_account="controller@example.com",
    )
    command = manage.provision_command(args)

    assert command[command.index("--zone") + 1] == "us-central1-c"
    assert command[command.index("--network-tier") + 1] == "STANDARD"
    assert command[command.index("--scopes") + 1] == "cloud-platform"


def test_campaign_command_has_distinct_inputs_and_both_measurements(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.txt"
    rr = tmp_path / "rr.tsv"
    dnp = tmp_path / "dnp.txt"
    for path in (trace, rr, dnp):
        path.write_text("8.8.8.8\n", encoding="utf-8")
    args = submit.build_parser().parse_args(
        [
            "--run-id",
            "test-run",
            "--trace-targets",
            str(trace),
            "--rr-targets",
            str(rr),
            "--bucket",
            "bucket",
            "--regions",
            "us-central1",
            "--worker-machine-type",
            "e2-medium",
            "--do-not-probe-file",
            str(dnp),
            "--skip-smoke",
        ]
    )

    command = submit.campaign_command(args, tmp_path / "job")

    assert command[command.index("--trace-target-source") + 1] == str(trace)
    assert command[command.index("--rr-target-source") + 1] == str(rr)
    assert command[command.index("--measurements") + 1] == "trace,rr"
    assert command[command.index("--trace-rate") + 1] == "1000"
    assert command[command.index("--rr-rate") + 1] == "1000"
    assert "--skip-smoke" in command
    assert args.worker_machine_type == "e2-medium"


def test_systemd_unit_is_durable_and_runs_as_controller(tmp_path: Path) -> None:
    args = argparse.Namespace(run_id="test-run", worker_machine_type="e2-medium")
    command = submit.systemd_command(args, ["python", "gcp.py"])

    assert "--unit=scamper-campaign-test-run" in command
    assert "--setenv=GCP_MACHINE_TYPE=e2-medium" in command
    assert "--uid=scamper-controller" in command
    assert "/usr/local/bin/scamper-controller-run" in command
    assert command[-2:] == ["python", "gcp.py"]


def test_systemd_unit_can_select_prebuilt_worker_image() -> None:
    args = argparse.Namespace(
        run_id="test-run",
        worker_machine_type="e2-micro",
        worker_image_project="measurement-project",
        worker_image_family="scamper-worker-debian12",
    )

    command = submit.systemd_command(args, ["python", "gcp.py"])

    assert "--setenv=GCP_IMAGE_PROJECT=measurement-project" in command
    assert "--setenv=GCP_IMAGE_FAMILY=scamper-worker-debian12" in command


def test_manage_submit_accepts_registered_target_ids() -> None:
    digest = "a" * 64

    args = manage.build_parser().parse_args(
        [
            "submit",
            "--run-id",
            "test-run",
            "--trace-target-id",
            f"sha256:{digest}",
            "--rr-target-id",
            f"sha256:{digest}",
            "--regions",
            "us-central1",
        ]
    )

    assert args.trace_targets is None
    assert args.rr_targets is None
    assert args.trace_target_id == f"sha256:{digest}"
    assert args.rr_target_id == f"sha256:{digest}"


def test_gcp_artifact_contract_keeps_raw_output_without_full_jsonl() -> None:
    artifacts = expected_campaign_artifacts(
        "runs/test/nodes/us-east1/node", "node-192.0.2.1", ("trace", "rr")
    )

    assert len(artifacts) == 7
    assert any(artifact.endswith(".trace.warts") for artifact in artifacts)
    assert any(artifact.endswith(".rr.warts") for artifact in artifacts)
    assert not any(artifact.endswith(".jsonl") for artifact in artifacts)
