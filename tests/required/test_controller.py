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


def test_submit_dispatches_the_requested_provider() -> None:
    """The controller must be able to launch every provider that has a driver."""
    from controller import submit
    from providers import DRIVER_MODULES

    for provider, module in sorted(DRIVER_MODULES.items()):
        args = argparse.Namespace(
            provider=provider, run_id="r", log_dir=None,
            trace_targets=Path("/t"), rr_targets=Path("/r"),
            bucket="b", object_prefix=None, regions="us-east1",
            worker_machine_type="e2-micro", worker_image_project=None,
            worker_image_family=None, measurements="trace,rr",
            max_instances=None, max_targets=None, trace_rate=1000,
            rr_rate=1000, rr_timeout=2.0, probe_payload="p",
            measurement_contact="c", do_not_probe_file=Path("/d"),
            skip_smoke=False,
        )
        command = submit.campaign_command(args, Path("/tmp/job"))
        assert module in command, f"{provider} must dispatch {module}"
    # No provider name is hardcoded any more.
    source = Path(submit.__file__).read_text(encoding="utf-8")
    assert 'driver_module("gcp")' not in source


def test_submit_refuses_to_originate_off_the_controller(tmp_path, monkeypatch) -> None:
    """Measurements must come from the controller, which owns the job record.

    A campaign launched from a workstation leaves no durable record of what ran,
    with which code, or whether teardown completed.
    """
    import pytest

    from controller import submit

    monkeypatch.setattr(submit, "INSTALL_ROOT", tmp_path / "absent")
    monkeypatch.setattr(submit, "STATE_ROOT", tmp_path / "also-absent")

    with pytest.raises(SystemExit, match="not the scamper controller"):
        submit.assert_controller_origin()

    # The escape hatch is explicit and labels the submission.
    assert submit.assert_controller_origin(allow_foreign=True) == "foreign"

    # On a real controller both roots exist.
    install, state = tmp_path / "opt", tmp_path / "state"
    install.mkdir(); state.mkdir()
    monkeypatch.setattr(submit, "INSTALL_ROOT", install)
    monkeypatch.setattr(submit, "STATE_ROOT", state)
    assert submit.assert_controller_origin() == "controller"


def test_worker_machine_type_reaches_every_provider() -> None:
    """--worker-machine-type used to set GCP_MACHINE_TYPE only.

    For AWS and Azure it was therefore silently ignored: their sizes came from
    literals hardcoded in the drivers that no operator could override. That is
    how a network-bound campaign ended up on Standard_D2s_v5.
    """
    from controller import submit

    expected = {"gcp": "GCP_MACHINE_TYPE", "aws": "AWS_INSTANCE_TYPES", "azure": "AZR_VM_SIZE"}
    for provider, variable in expected.items():
        args = argparse.Namespace(
            provider=provider, run_id="r", worker_machine_type="size-x",
            worker_image_project=None, worker_image_family=None,
        )
        joined = " ".join(submit.systemd_command(args, ["cmd"]))
        assert f"--setenv={variable}=size-x" in joined, (
            f"{provider} must receive its size via {variable}: {joined}"
        )
    # Every launchable provider must have a route; none may be silently dropped.
    from providers import DRIVER_MODULES

    assert set(DRIVER_MODULES) <= set(submit.MACHINE_TYPE_ENV)


def test_default_worker_size_is_the_providers_own_cheap_default() -> None:
    """A GCP-shaped default must not leak into an AWS or Azure campaign."""
    from controller import submit

    assert submit.default_worker_machine_type("gcp").startswith("e2-")
    assert submit.default_worker_machine_type("aws").startswith("t3.")
    azure_default = submit.default_worker_machine_type("azure")
    assert azure_default == "Standard_B2ts_v2"
    assert "D2s_v5" not in azure_default


def test_controller_installs_every_providers_sdk() -> None:
    """The controller launches all providers, so it must be able to import them.

    Requirements previously listed GCP libraries only, so an AWS or Azure
    campaign would have failed at import on the controller - one of the reasons
    those campaigns ran from unmanaged hosts instead.
    """
    requirements = (
        Path(__file__).resolve().parents[2] / "controller/requirements.txt"
    ).read_text(encoding="utf-8")
    for package in ("boto3", "azure-identity", "azure-mgmt-compute", "azure-mgmt-network"):
        assert package in requirements, f"controller cannot import {package}"


def test_provider_credentials_are_not_generated_or_committed() -> None:
    """Secrets stay in an operator-provisioned file, outside the release."""
    root = Path(__file__).resolve().parents[2]
    bootstrap = (root / "controller/bootstrap.sh").read_text(encoding="utf-8")
    runner = (root / "controller/run-campaign").read_text(encoding="utf-8")

    # The runner loads them if present, so a campaign can authenticate...
    assert "/etc/scamper-controller-secrets.env" in runner
    # ...and bootstrap only ever leaves a commented template, locked down.
    assert "chmod 0600 /etc/scamper-controller-secrets.env" in bootstrap
    for secret in ("AZURE_CLIENT_SECRET=", "AWS_SECRET_ACCESS_KEY="):
        # Present only as a commented placeholder, never assigned a value.
        for line in bootstrap.splitlines():
            if secret in line:
                assert line.strip().startswith("#"), f"secret assigned in bootstrap: {line}"


def test_secrets_file_is_readable_by_the_controller_user() -> None:
    """run-campaign executes as scamper-controller via systemd --uid.

    A root-owned 0600 secrets file is unreadable to it, so every non-GCP campaign
    fails with CredentialUnavailableError - which is exactly what happened on the
    first attempt. Ownership must be set, not just the mode.
    """
    bootstrap = (
        Path(__file__).resolve().parents[2] / "controller/bootstrap.sh"
    ).read_text(encoding="utf-8")

    chown_line = next(
        (l for l in bootstrap.splitlines()
         if "chown" in l and "secrets.env" in l), None
    )
    assert chown_line, "secrets file must be chown'd to the controller user"
    assert "${controller_user}" in chown_line
    assert "chmod 0600 /etc/scamper-controller-secrets.env" in bootstrap
