from __future__ import annotations

import json
from pathlib import Path

from providers.azure import driver as azr


class FakePoller:
    def __init__(self, waited: list[str], name: str) -> None:
        self.waited = waited
        self.name = name

    def wait(self) -> None:
        self.waited.append(self.name)


class FakePool:
    def __init__(self, size: int) -> None:
        self.size = size

    def __enter__(self) -> "FakePool":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def map(self, function: object, run_infos: list[tuple[str, str]]) -> list[tuple[str, str]]:
        return [(location, f"203.0.113.{index + 1}") for index, (_prefix, location) in enumerate(run_infos)]


class FakeProcess:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_run_azr_scamper_caps_instances_targets_and_cleans_up(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target_file = tmp_path / "ipv4-24"
    target_file.write_text("".join(f"192.0.2.{index}\n" for index in range(20)), encoding="utf-8")
    log_dir = tmp_path / "logs"
    waited: list[str] = []
    uploaded: list[tuple[str, str | None]] = []
    manifests: list[dict[str, object]] = []
    recorded_counts: list[int] = []
    popen_args: list[list[str]] = []

    monkeypatch.setattr(azr.settings, "SCAMPER_IP_DST", str(target_file))
    monkeypatch.setattr(azr.settings, "AZR_SCAMPER_VM_SCRIPT", str(tmp_path / "run-scamper-azr.sh"))
    monkeypatch.setattr(azr.settings, "AZR_SCAMPER_SSH_KEY", str(tmp_path / "azr-key"))
    monkeypatch.setattr(azr.settings, "AZR_SCAMPER_USER", "azureuser")
    monkeypatch.setattr(azr, "create_bucket", lambda bucket: None)
    monkeypatch.setattr(azr, "create_rg", lambda prefix: None)
    monkeypatch.setattr(azr, "delete_rg", lambda prefix: FakePoller(waited, prefix))
    monkeypatch.setattr(azr, "get_locations", lambda: [f"region-{index}" for index in range(12)])
    monkeypatch.setattr(azr, "Pool", FakePool)
    def capture_upload(file_name: str, bucket: str, object_name: str | None = None) -> None:
        uploaded.append((Path(file_name).name, object_name))
        if Path(file_name).name == "manifest.json":
            manifests.append(json.loads(Path(file_name).read_text(encoding="utf-8")))

    monkeypatch.setattr(azr, "send_to_cloud_storage", capture_upload)
    monkeypatch.setattr(azr, "record_expense_instances", recorded_counts.append)

    def popen(args: list[str], **kwargs: object) -> FakeProcess:
        popen_args.append(args)
        return FakeProcess(0)

    monkeypatch.setattr(azr.subprocess, "Popen", popen)

    bucket = azr.run_azr_scamper(
        str(log_dir),
        "azr-test",
        max_instances=3,
        max_targets=5,
    )

    assert bucket == azr.settings.SCAMPER_RESULTS_BUCKET
    assert recorded_counts == [3]
    assert waited == ["azr-test"]
    assert uploaded == [
        ("manifest.json", "runs/azr-test/manifest.json"),
        ("logs.tar.gz", "runs/azr-test/logs/logs.tar.gz"),
    ]
    assert manifests[0]["complete"] is True
    assert manifests[0]["bucket"] == azr.settings.SCAMPER_RESULTS_BUCKET
    assert manifests[0]["object_prefix"] == "runs/azr-test"
    assert manifests[0]["failed_nodes"] == []
    assert len(manifests[0]["nodes"]) == 3
    assert not log_dir.exists()

    scp_commands = [args for args in popen_args if args and args[0] == "scp"]
    ssh_commands = [args for args in popen_args if args and args[0] == "ssh"]
    assert len(scp_commands) == 3
    assert len(ssh_commands) == 3
    assert all("azr-test-targets-5.txt" in " ".join(args) for args in scp_commands)
    assert all("azr-test-targets-5.txt" in " ".join(args) for args in ssh_commands)
    assert all(".trace.warts" in " ".join(args) for args in ssh_commands)
    assert all("runs/azr-test/nodes/" in " ".join(args) for args in ssh_commands)
    assert all("region-3" not in " ".join(args) for args in [*scp_commands, *ssh_commands])


def test_launch_locations_keeps_trying_until_requested_successes(monkeypatch) -> None:
    calls: list[list[tuple[str, str]]] = []

    class RetryPool:
        def __init__(self, size: int) -> None:
            self.size = size

        def __enter__(self) -> "RetryPool":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def map(
            self,
            function: object,
            run_infos: list[tuple[str, str]],
        ) -> list[tuple[str, str] | None]:
            calls.append(run_infos)
            return [
                None if location.startswith("bad") else (location, f"203.0.113.{index}")
                for index, (_prefix, location) in enumerate(run_infos)
            ]

    monkeypatch.setattr(azr, "Pool", RetryPool)

    ips = azr.launch_locations(
        "azr-test",
        ["bad-0", "bad-1", "good-0", "good-1", "good-2"],
        max_instances=2,
    )

    assert ips == [("good-0", "203.0.113.0"), ("good-1", "203.0.113.1")]
    assert calls == [
        [("azr-test", "bad-0"), ("azr-test", "bad-1")],
        [("azr-test", "good-0"), ("azr-test", "good-1")],
    ]


def test_build_plan_uses_stable_bucket_and_per_run_prefix() -> None:
    plan = azr.build_plan("azr-test", "logs")

    assert plan["bucket"] == azr.settings.SCAMPER_RESULTS_BUCKET
    assert plan["object_prefix"] == "runs/azr-test"
    assert plan["output_name"] == "PREFIX-LOCATION-IP.trace.warts"


def test_build_plan_normalizes_custom_object_prefix() -> None:
    plan = azr.build_plan(
        "azr-test",
        "logs",
        bucket_name="custom-results",
        object_prefix="/experiments/azr-test/",
    )

    assert plan["bucket"] == "custom-results"
    assert plan["object_prefix"] == "experiments/azr-test"


def test_write_run_manifest_records_failed_nodes(tmp_path: Path) -> None:
    target_file = tmp_path / "targets.txt"
    target_file.write_text("192.0.2.1\n", encoding="utf-8")
    path = azr.write_run_manifest(
        str(tmp_path),
        prefix="azr-test",
        bucket_name="results",
        object_prefix="runs/azr-test",
        target_file=str(target_file),
        locations=("eastus",),
        nodes=[{"node": "azr-eastus", "complete": False}],
        started_at="2026-07-31T00:00:00+00:00",
        complete=False,
        failure="remote command failed",
    )

    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    assert manifest["complete"] is False
    assert manifest["failed_nodes"] == ["azr-eastus"]
    assert manifest["object_prefix"] == "runs/azr-test"


def test_driver_takes_its_vm_size_from_settings_not_a_literal() -> None:
    """The size must be overridable; hardcoding it is what cost ~$150/day."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "providers/azure/driver.py"
    text = source.read_text(encoding="utf-8")
    assert "settings.AZR_VM_SIZE" in text
    assert 'vm_size="Standard_' not in text, "VM size must not be a literal"
    assert "D2s_v5" not in text


def test_locations_can_be_restricted_for_a_single_region_canary(monkeypatch) -> None:
    """Capping instances is not region selection; a canary must name its region."""
    from providers.azure import driver

    monkeypatch.delenv("SCAMPER_AZR_LOCATIONS", raising=False)
    assert driver.locations_from_env() is None

    monkeypatch.setenv("SCAMPER_AZR_LOCATIONS", "westeurope")
    assert driver.locations_from_env() == ["westeurope"]

    monkeypatch.setenv("SCAMPER_AZR_LOCATIONS", " westeurope , japaneast ")
    assert driver.locations_from_env() == ["westeurope", "japaneast"]
