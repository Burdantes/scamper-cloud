from __future__ import annotations

import json
from pathlib import Path

import pytest

from providers.aws import driver as aws


class FakeInstance:
    def __init__(self, name: str, terminated: list[str]) -> None:
        self.name = name
        self.public_ip_address = "203.0.113.10"
        self._terminated = terminated

    def wait_until_running(self) -> None:
        return None

    def reload(self) -> None:
        return None

    def terminate(self) -> None:
        self._terminated.append(self.name)


class FakeProcess:
    def __init__(self, exit_code: int = 0, *, running: bool = False) -> None:
        self.exit_code = exit_code
        self.running = running
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        if self.running:
            return None
        return self.exit_code

    def wait(self, timeout: float | None = None) -> int:
        if self.running and timeout is not None:
            raise aws.subprocess.TimeoutExpired("fake-process", timeout)
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.running = False
        self.exit_code = -15

    def kill(self) -> None:
        self.killed = True
        self.running = False
        self.exit_code = -9


def patch_common_aws_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    ssh_exit_code: int = 0,
    popen_args: list[list[str]] | None = None,
) -> tuple[list[str], list[int], list[tuple[str, str | None]]]:
    terminated: list[str] = []
    recorded_counts: list[int] = []
    uploaded: list[tuple[str, str | None]] = []

    monkeypatch.setattr(aws, "create_bucket", lambda bucket: None)
    monkeypatch.setattr(aws, "get_regions", lambda: ["ok-region", "bad-region"])
    monkeypatch.setattr(aws, "get_zones", lambda region: [f"{region}a"])
    monkeypatch.setattr(aws, "record_expense_instances", recorded_counts.append)
    monkeypatch.setattr(aws, "get_ssh_ready_ip", lambda instance: instance.public_ip_address)
    def capture_upload(file_name: str, bucket: str, object_name: str | None = None) -> None:
        uploaded.append((Path(file_name).name, object_name))

    monkeypatch.setattr(aws, "send_to_cloud_storage", capture_upload)
    monkeypatch.setattr(
        aws,
        "uploaded_artifact_sizes",
        lambda bucket, names: {name: 1024 for name in names},
    )
    monkeypatch.setattr(aws, "incomplete_uploaded_statuses", lambda bucket, names: [])
    monkeypatch.setattr(aws.settings, "AWS_SCAMPER_VM_SCRIPT", str(tmp_path / "run-scamper-aws.sh"))
    target_file = tmp_path / "ipv4-24"
    target_file.write_text("192.0.2.1\n", encoding="utf-8")
    monkeypatch.setattr(aws.settings, "SCAMPER_IP_DST", str(target_file))
    monkeypatch.setattr(aws.settings, "AWS_SCAMPER_SSH_KEY", str(tmp_path / "key.pem"))

    def create_default_security_group(region: str, sg_name: str) -> str:
        if region == "bad-region":
            raise TimeoutError("endpoint timed out")
        return "sg-123"

    def create_instance(region: str, zone: str, sg_id: str, name: str) -> FakeInstance:
        return FakeInstance(name, terminated)

    def popen(args: list[str], **kwargs: object) -> FakeProcess:
        if popen_args is not None:
            popen_args.append(args)
        if args and args[0] == "ssh":
            return FakeProcess(ssh_exit_code)
        return FakeProcess(0)

    monkeypatch.setattr(aws, "create_default_security_group", create_default_security_group)
    monkeypatch.setattr(aws, "create_instance", create_instance)
    monkeypatch.setattr(aws.subprocess, "Popen", popen)

    return terminated, recorded_counts, uploaded


def test_run_aws_scamper_skips_failed_region_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated, recorded_counts, uploaded = patch_common_aws_flow(monkeypatch, tmp_path)

    aws.run_aws_scamper(str(tmp_path / "logs"), "aws-test")

    assert terminated == ["aws-test-ok-regiona"]
    assert recorded_counts == [1, 1]
    assert uploaded == [
        ("manifest.json", "runs/aws-test/manifest.json"),
        ("logs.tar.gz", "runs/aws-test/logs/logs.tar.gz"),
    ]
    assert not (tmp_path / "logs").exists()


def test_run_aws_scamper_cleans_up_when_remote_scamper_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated, recorded_counts, uploaded = patch_common_aws_flow(
        monkeypatch,
        tmp_path,
        ssh_exit_code=7,
    )
    monkeypatch.setattr(aws, "uploaded_artifact_sizes", lambda bucket, names: {})

    with pytest.raises(RuntimeError, match="missing"):
        aws.run_aws_scamper(str(tmp_path / "logs"), "aws-test")

    assert terminated == ["aws-test-ok-regiona"]
    assert recorded_counts == [1, 1]
    assert uploaded == [
        ("manifest.json", "runs/aws-test/manifest.json"),
        ("logs.tar.gz", "runs/aws-test/logs/logs.tar.gz"),
    ]
    assert not (tmp_path / "logs").exists()


def test_run_aws_scamper_accepts_uploaded_artifacts_when_ssh_lingers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_file = tmp_path / "ipv4-24"
    target_file.write_text("192.0.2.1\n", encoding="utf-8")
    monkeypatch.setattr(aws.settings, "SCAMPER_IP_DST", str(target_file))
    ssh_processes: list[FakeProcess] = []

    terminated, recorded_counts, uploaded = patch_common_aws_flow(monkeypatch, tmp_path)

    def popen(args: list[str], **kwargs: object) -> FakeProcess:
        if args and args[0] == "ssh":
            process = FakeProcess(running=True)
            ssh_processes.append(process)
            return process
        return FakeProcess(0)

    monkeypatch.setattr(aws.subprocess, "Popen", popen)

    aws.run_aws_scamper(str(tmp_path / "logs"), "aws-test")

    assert terminated == ["aws-test-ok-regiona"]
    assert recorded_counts == [1, 1]
    assert uploaded == [
        ("manifest.json", "runs/aws-test/manifest.json"),
        ("logs.tar.gz", "runs/aws-test/logs/logs.tar.gz"),
    ]
    assert [process.terminated for process in ssh_processes] == [True]
    assert not (tmp_path / "logs").exists()


def test_run_aws_scamper_times_out_missing_artifacts_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_file = tmp_path / "ipv4-24"
    target_file.write_text("192.0.2.1\n", encoding="utf-8")
    monkeypatch.setattr(aws.settings, "SCAMPER_IP_DST", str(target_file))
    monkeypatch.setenv("SCAMPER_AWS_SCAMPER_TIMEOUT_SECONDS", "0")
    monkeypatch.setattr(aws, "uploaded_artifact_sizes", lambda bucket, names: {})
    ssh_processes: list[FakeProcess] = []

    terminated, recorded_counts, uploaded = patch_common_aws_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(aws, "uploaded_artifact_sizes", lambda bucket, names: {})

    def popen(args: list[str], **kwargs: object) -> FakeProcess:
        if args and args[0] == "ssh":
            process = FakeProcess(running=True)
            ssh_processes.append(process)
            return process
        return FakeProcess(0)

    monkeypatch.setattr(aws.subprocess, "Popen", popen)

    with pytest.raises(TimeoutError, match="timed out"):
        aws.run_aws_scamper(str(tmp_path / "logs"), "aws-test")

    assert terminated == ["aws-test-ok-regiona"]
    assert recorded_counts == [1, 1]
    assert uploaded == [
        ("manifest.json", "runs/aws-test/manifest.json"),
        ("logs.tar.gz", "runs/aws-test/logs/logs.tar.gz"),
    ]
    assert [process.terminated for process in ssh_processes] == [True]
    assert not (tmp_path / "logs").exists()


def test_run_aws_scamper_stops_after_max_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated: list[str] = []
    recorded_counts: list[int] = []
    setup_regions: list[str] = []

    monkeypatch.setattr(aws, "create_bucket", lambda bucket: None)
    monkeypatch.setattr(aws, "get_regions", lambda: ["region-one", "region-two"])
    monkeypatch.setattr(aws, "get_zones", lambda region: [f"{region}a", f"{region}b"])
    monkeypatch.setattr(aws, "record_expense_instances", recorded_counts.append)
    monkeypatch.setattr(aws, "get_ssh_ready_ip", lambda instance: instance.public_ip_address)
    monkeypatch.setattr(
        aws,
        "send_to_cloud_storage",
        lambda _file, _bucket, _object_name=None: None,
    )
    monkeypatch.setattr(
        aws,
        "uploaded_artifact_sizes",
        lambda bucket, names: {name: 1024 for name in names},
    )
    monkeypatch.setattr(aws, "incomplete_uploaded_statuses", lambda bucket, names: [])
    monkeypatch.setattr(aws.settings, "AWS_SCAMPER_VM_SCRIPT", str(tmp_path / "run-scamper-aws.sh"))
    target_file = tmp_path / "ipv4-24"
    target_file.write_text("192.0.2.1\n", encoding="utf-8")
    monkeypatch.setattr(aws.settings, "SCAMPER_IP_DST", str(target_file))
    monkeypatch.setattr(aws.settings, "AWS_SCAMPER_SSH_KEY", str(tmp_path / "key.pem"))

    def create_default_security_group(region: str, sg_name: str) -> str:
        setup_regions.append(region)
        return "sg-123"

    def create_instance(region: str, zone: str, sg_id: str, name: str) -> FakeInstance:
        return FakeInstance(name, terminated)

    monkeypatch.setattr(aws, "create_default_security_group", create_default_security_group)
    monkeypatch.setattr(aws, "create_instance", create_instance)
    monkeypatch.setattr(aws.subprocess, "Popen", lambda *args, **kwargs: FakeProcess(0))

    aws.run_aws_scamper(str(tmp_path / "logs"), "aws-test", max_instances=2)

    assert setup_regions == ["region-one"]
    assert terminated == ["aws-test-region-onea", "aws-test-region-oneb"]
    assert recorded_counts == [1, 2, 2]


def test_run_aws_scamper_caps_targets_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_file = tmp_path / "ipv4-24"
    target_file.write_text(
        "".join(f"192.0.2.{index}\n" for index in range(20)),
        encoding="utf-8",
    )
    popen_args: list[list[str]] = []
    terminated, _recorded_counts, uploaded = patch_common_aws_flow(
        monkeypatch,
        tmp_path,
        popen_args=popen_args,
    )
    monkeypatch.setattr(aws.settings, "SCAMPER_IP_DST", str(target_file))

    aws.run_aws_scamper(str(tmp_path / "logs"), "aws-test", max_targets=5)

    assert terminated == ["aws-test-ok-regiona"]
    assert uploaded == [
        ("manifest.json", "runs/aws-test/manifest.json"),
        ("logs.tar.gz", "runs/aws-test/logs/logs.tar.gz"),
    ]
    assert not (tmp_path / "logs").exists()

    scp_commands = [args for args in popen_args if args and args[0] == "scp"]
    ssh_commands = [args for args in popen_args if args and args[0] == "ssh"]
    assert len(scp_commands) == 1
    assert len(ssh_commands) == 1
    assert "aws-test-targets-5.txt" in " ".join(scp_commands[0])
    assert "aws-test-targets-5.txt" in " ".join(ssh_commands[0])
    assert str(target_file) not in scp_commands[0]


def test_build_target_file_extracts_tsv_destinations_and_validates_full_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "targets.tsv"
    source.write_text(
        "192.0.2.1\tresponsive\t2026-05-11\n"
        "198.51.100.2\tresponsive\t2026-05-11\n"
        "203.0.113.\ttruncated\t2026-05-11",
        encoding="utf-8",
    )
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    capped = aws.build_target_file(
        str(log_dir),
        "aws-test",
        max_targets=2,
        target_source=str(source),
    )
    assert Path(capped).read_text(encoding="utf-8") == "192.0.2.1\n198.51.100.2\n"

    with pytest.raises(ValueError, match="source line 3"):
        aws.build_target_file(
            str(log_dir),
            "aws-full",
            target_source=str(source),
        )


def test_run_aws_scamper_uses_only_requested_region(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_file = tmp_path / "targets.txt"
    target_file.write_text("192.0.2.1\n", encoding="utf-8")
    setup_regions: list[str] = []
    patch_common_aws_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(aws.settings, "SCAMPER_IP_DST", str(target_file))
    monkeypatch.setattr(
        aws,
        "create_default_security_group",
        lambda region, _name: setup_regions.append(region) or "sg-123",
    )

    aws.run_aws_scamper(
        str(tmp_path / "logs"),
        "aws-test",
        max_instances=1,
        regions=("ap-south-2",),
    )

    assert setup_regions == ["ap-south-2"]


def test_wait_for_scamper_rejects_incomplete_uploaded_status(monkeypatch) -> None:
    process = FakeProcess(0)
    artifacts = ["node.trace.warts", "node.status.json"]
    monkeypatch.setattr(
        aws,
        "uploaded_artifact_sizes",
        lambda bucket, names: {name: 1024 for name in names},
    )
    monkeypatch.setattr(
        aws,
        "incomplete_uploaded_statuses",
        lambda bucket, names: ["node.status.json"],
    )

    with pytest.raises(RuntimeError, match="incomplete AWS campaign status"):
        aws.wait_for_scamper_processes(
            [(process, {"name": "node"}, "node")],
            "bucket",
            artifacts,
        )


def test_build_plan_uses_stable_bucket_and_per_run_prefix() -> None:
    plan = aws.build_plan("aws-test", "logs")

    assert plan["bucket"] == aws.settings.SCAMPER_RESULTS_BUCKET
    assert plan["object_prefix"] == "runs/aws-test"


def test_write_run_manifest_records_failed_nodes(tmp_path: Path) -> None:
    target_file = tmp_path / "targets.txt"
    target_file.write_text("192.0.2.1\n", encoding="utf-8")
    path = aws.write_run_manifest(
        str(tmp_path),
        prefix="aws-test",
        bucket_name="results",
        object_prefix="runs/aws-test",
        target_source="source.tsv",
        target_version="source.tsv@sha256:abc",
        normalized_target_file=str(target_file),
        regions=("us-east-1",),
        measurements=("trace", "rr"),
        trace_rate=100,
        rr_rate=10,
        rr_timeout=2.0,
        nodes=[{"node": "node-a", "complete": False}],
        started_at="2026-07-31T00:00:00+00:00",
        complete=False,
        failure="timeout",
    )

    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    assert manifest["complete"] is False
    assert manifest["failed_nodes"] == ["node-a"]
    assert manifest["object_prefix"] == "runs/aws-test"


def test_driver_takes_its_instance_types_from_settings_not_a_literal() -> None:
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "providers/aws/driver.py"
    text = source.read_text(encoding="utf-8")
    assert "settings.AWS_INSTANCE_TYPES" in text
    assert "instance_types = ['t3.micro','t2.micro']" not in text
    # The describe filter must follow the configured list, not a second literal.
    assert '"Values":["t2.micro","t3.micro"]' not in text


def test_all_providers_resolve_google_credentials_the_same_way() -> None:
    """AWS and Azure hard-required a key file; GCP already fell back to ADC.

    On the controller, WARTS_STORAGE_CREDENTIALS names a path that was never
    created, so GCP campaigns ran while AWS and Azure died with FileNotFoundError
    before provisioning anything. One shared resolver stops the untested paths
    drifting from the tested one.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for rel in ("providers/aws/driver.py", "providers/azure/driver.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "from providers.gcs_credentials import" in text, rel
        assert "from_service_account_json" not in text, f"{rel} bypasses the resolver"
        assert "from_service_account_file" not in text, f"{rel} bypasses the resolver"


def test_google_credentials_fall_back_to_adc(monkeypatch, tmp_path) -> None:
    """A GCE controller has an attached service account; no key file needed."""
    import providers.gcs_credentials as gc
    from providers import settings

    monkeypatch.setattr(gc, "_credentials", None)
    monkeypatch.setattr(settings, "WARTS_STORAGE_CREDENTIALS", str(tmp_path / "absent.json"))
    called = {}

    def fake_default(scopes=None):
        called["scopes"] = scopes
        return ("adc-credentials", "some-project")

    import google.auth

    monkeypatch.setattr(google.auth, "default", fake_default)
    assert gc.google_credentials() == "adc-credentials"
    assert called["scopes"] == gc.SCOPES
