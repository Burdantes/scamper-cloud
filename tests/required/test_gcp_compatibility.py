from __future__ import annotations

import json
from pathlib import Path

from legacy.providers.gcp import driver as gcp
import pytest


class FakeProcess:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code

    def poll(self) -> int:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_run_gcp_scamper_caps_instances_targets_and_cleans_up(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target_file = tmp_path / "ipv4-24"
    target_file.write_text(
        "".join(f"192.0.2.{index}\n" for index in range(20)), encoding="utf-8"
    )
    log_dir = tmp_path / "logs"
    uploaded: list[tuple[str, str | None]] = []
    manifests: list[dict[str, object]] = []
    recorded_counts: list[int] = []
    popen_args: list[list[str]] = []
    delete_calls: list[list[tuple[str, str, str]]] = []
    launch_args: list[tuple[str, list[str], int | None]] = []

    monkeypatch.setattr(gcp.settings, "SCAMPER_IP_DST", str(target_file))
    monkeypatch.setattr(
        gcp.settings, "GCP_SCAMPER_SCRIPT", str(tmp_path / "run-scamper-gcp.sh")
    )
    monkeypatch.setattr(gcp.settings, "GCP_SCAMPER_SSH_KEY", str(tmp_path / "gcp-key"))
    monkeypatch.setattr(gcp.settings, "GCP_SCAMPER_USER", "gcpuser")
    monkeypatch.setattr(gcp, "create_bucket", lambda bucket: None)
    monkeypatch.setattr(
        gcp, "get_zones", lambda: [f"zone-{index}" for index in range(12)]
    )

    def capture_upload(
        file_name: str, bucket: str, object_name: str | None = None
    ) -> None:
        uploaded.append((Path(file_name).name, object_name))
        if Path(file_name).name == "manifest.json":
            manifests.append(json.loads(Path(file_name).read_text(encoding="utf-8")))

    monkeypatch.setattr(gcp, "send_to_cloud_storage", capture_upload)
    monkeypatch.setattr(gcp, "record_expense_instances", recorded_counts.append)
    def capture_delete(instances):
        captured = list(instances)
        delete_calls.append(captured)
        return {instance[0] for instance in captured}

    monkeypatch.setattr(gcp, "delete_instances", capture_delete)
    monkeypatch.setattr(
        gcp,
        "get_instance_details",
        lambda _name, _zone: {
            "networkInterfaces": [{"accessConfigs": [{"networkTier": "STANDARD"}]}]
        },
    )
    monkeypatch.setattr(
        gcp,
        "uploaded_artifact_sizes",
        lambda bucket, names: {name: 1024 for name in names},
    )
    monkeypatch.setattr(gcp, "incomplete_uploaded_statuses", lambda bucket, names: [])

    def create_instance_zones(
        prefix: str,
        zones: list[str],
        max_instances: int | None = None,
    ) -> list[str]:
        launch_args.append((prefix, list(zones), max_instances))
        return zones[: max_instances or len(zones)]

    def collect_instances(
        prefix: str,
        zones: list[str],
        expected_count: int,
    ) -> list[tuple[str, str, str]]:
        return [
            (f"{prefix}-{zone}", f"203.0.113.{index + 1}", zone)
            for index, zone in enumerate(zones[:expected_count])
        ]

    def popen(args: list[str], **kwargs: object) -> FakeProcess:
        popen_args.append(args)
        return FakeProcess(0)

    monkeypatch.setattr(gcp, "create_instance_zones", create_instance_zones)
    monkeypatch.setattr(gcp, "collect_instances", collect_instances)
    monkeypatch.setattr(gcp.subprocess, "Popen", popen)

    bucket = gcp.run_gcp_scamper(
        str(log_dir),
        "gcp-test",
        max_instances=3,
        max_targets=5,
    )

    assert bucket == gcp.settings.SCAMPER_RESULTS_BUCKET
    assert launch_args == [("gcp-test", [f"zone-{index}" for index in range(12)], 3)]
    assert recorded_counts == [3]
    assert len(delete_calls) == 3
    assert [[instance[2] for instance in call] for call in delete_calls] == [
        ["zone-0"],
        ["zone-1"],
        ["zone-2"],
    ]
    assert uploaded == [
        ("manifest.json", "runs/gcp-test/manifest.json"),
        ("logs.tar.gz", "runs/gcp-test/logs/logs.tar.gz"),
    ]
    assert len(manifests) == 1
    assert manifests[0]["complete"] is True
    assert manifests[0]["run_id"] == "gcp-test"
    assert manifests[0]["bucket"] == gcp.settings.SCAMPER_RESULTS_BUCKET
    assert manifests[0]["object_prefix"] == "runs/gcp-test"
    assert len(manifests[0]["nodes"]) == 3
    assert manifests[0]["failed_nodes"] == []
    assert {node["network_tier"] for node in manifests[0]["nodes"]} == {"STANDARD"}
    assert not log_dir.exists()

    scp_commands = [args for args in popen_args if args and args[0] == "scp"]
    ssh_commands = [args for args in popen_args if args and args[0] == "ssh"]
    assert len(scp_commands) == 3
    assert len(ssh_commands) == 3
    assert all(
        "gcp-test-trace-targets-5.txt" in " ".join(args) for args in scp_commands
    )
    assert all("gcp-test-rr-targets-5.txt" in " ".join(args) for args in scp_commands)
    assert all(
        "gcp-test-trace-targets-5.txt" in " ".join(args) for args in ssh_commands
    )
    assert all("gcp-test-rr-targets-5.txt" in " ".join(args) for args in ssh_commands)
    assert all(
        "SCAMPER_TRACE_TARGET_COUNT=5" in " ".join(args) for args in ssh_commands
    )
    assert all("SCAMPER_RR_TARGET_COUNT=5" in " ".join(args) for args in ssh_commands)
    assert all(
        "SCAMPER_TRACE_TARGET_SHA256=" in " ".join(args) for args in ssh_commands
    )
    assert all("SCAMPER_RR_TARGET_SHA256=" in " ".join(args) for args in ssh_commands)
    assert all("runs/gcp-test/nodes/" in " ".join(args) for args in ssh_commands)
    assert all(
        "zone-3" not in " ".join(args) for args in [*scp_commands, *ssh_commands]
    )


def test_create_instance_zones_keeps_trying_until_requested_successes(
    monkeypatch,
) -> None:
    created_zones: list[str] = []

    def create_instance(project: str, zone: str, name: str) -> dict[str, str]:
        created_zones.append(zone)
        return {"name": f"operation-{zone}"}

    def wait_zone_operation(project: str, zone: str, operation: str) -> None:
        if zone.startswith("bad"):
            raise RuntimeError(f"{zone} failed")

    monkeypatch.setattr(gcp, "create_instance", create_instance)
    monkeypatch.setattr(gcp, "wait_zone_operation", wait_zone_operation)

    zones = gcp.create_instance_zones(
        "gcp-test",
        ["bad-0", "good-0", "good-1"],
        max_instances=2,
    )

    assert zones == ["good-0", "good-1"]
    assert created_zones == ["bad-0", "good-0", "good-1"]


def test_create_instance_regions_uses_one_zone_per_region_and_retries(
    monkeypatch,
) -> None:
    attempted_zones: list[str] = []

    def create_instance(project: str, zone: str, name: str) -> dict[str, str]:
        attempted_zones.append(zone)
        return {"name": f"operation-{zone}"}

    def wait_zone_operation(project: str, zone: str, operation: str) -> None:
        if zone == "us-east1-a":
            raise RuntimeError("first zone unavailable")

    monkeypatch.setattr(gcp, "create_instance", create_instance)
    monkeypatch.setattr(gcp, "wait_zone_operation", wait_zone_operation)

    zones = gcp.create_instance_regions(
        "gcp-test",
        ["us-east1-a", "us-east1-b", "us-west1-a", "us-west1-b"],
        ["us-east1", "us-west1"],
        max_instances=2,
    )

    assert zones == ["us-east1-b", "us-west1-a"]
    assert attempted_zones == ["us-east1-a", "us-east1-b", "us-west1-a"]


def test_zones_in_regions_selects_only_requested_region() -> None:
    zones = ["us-central1-a", "us-central1-b", "us-east1-b"]

    assert gcp.zones_in_regions(zones, ("us-central1",)) == [
        "us-central1-a",
        "us-central1-b",
    ]


def test_verify_standard_network_tier_rejects_premium(monkeypatch) -> None:
    monkeypatch.setattr(
        gcp,
        "get_instance_details",
        lambda _name, _zone: {
            "networkInterfaces": [{"accessConfigs": [{"networkTier": "PREMIUM"}]}]
        },
    )

    with pytest.raises(RuntimeError, match="not STANDARD"):
        gcp.verify_standard_network_tier(
            [("gcp-test-us-central1-a", "203.0.113.1", "us-central1-a")]
        )


def test_verify_standard_network_tier_rejects_premium_configuration(
    monkeypatch,
) -> None:
    monkeypatch.setattr(gcp.settings, "GCP_NETWORK_TIER", "PREMIUM")

    with pytest.raises(RuntimeError, match="must be STANDARD"):
        gcp.verify_standard_network_tier([])


def test_build_target_file_extracts_tsv_and_full_validation(tmp_path: Path) -> None:
    source = tmp_path / "targets.tsv"
    source.write_text(
        "192.0.2.1\tresponsive\t2026-05-11\n"
        "198.51.100.2\tresponsive\t2026-05-11\n"
        "203.0.113.\ttruncated\t2026-05-11",
        encoding="utf-8",
    )
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    capped = gcp.build_target_file(
        str(log_dir),
        "gcp-test",
        max_targets=2,
        target_source=str(source),
    )
    assert Path(capped).read_text(encoding="utf-8") == "192.0.2.1\n198.51.100.2\n"

    try:
        gcp.build_target_file(
            str(log_dir),
            "gcp-full",
            target_source=str(source),
        )
    except ValueError as error:
        assert "source line 3" in str(error)
    else:
        raise AssertionError("full target validation should reject the truncated row")


def test_build_target_file_enforces_do_not_probe_prefixes(tmp_path: Path) -> None:
    source = tmp_path / "targets.tsv"
    source.write_text(
        "192.0.2.1\tresponsive\n198.51.100.2\tresponsive\n203.0.113.3\tresponsive\n",
        encoding="utf-8",
    )
    exclusions = tmp_path / "do-not-probe.txt"
    exclusions.write_text(
        "# opt-out request\n198.51.100.0/24\n203.0.113.3\n",
        encoding="utf-8",
    )
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    networks = gcp.load_do_not_probe_prefixes(exclusions)
    output = gcp.build_target_file(
        str(log_dir),
        "gcp-filtered",
        target_source=str(source),
        do_not_probe_networks=networks,
    )

    assert Path(output).read_text(encoding="utf-8") == "192.0.2.1\n"


def test_run_gcp_scamper_deletes_created_vm_when_ip_collection_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target_file = tmp_path / "targets.txt"
    target_file.write_text("192.0.2.1\n", encoding="utf-8")
    deleted: list[tuple[str, str, str]] = []
    manifests: list[dict[str, object]] = []

    monkeypatch.setattr(gcp.settings, "SCAMPER_IP_DST", str(target_file))
    monkeypatch.setattr(gcp, "get_zones", lambda: ["us-central1-a"])
    monkeypatch.setattr(gcp, "create_bucket", lambda _bucket: None)
    monkeypatch.setattr(
        gcp,
        "create_instance_regions",
        lambda _prefix, _zones, _regions, max_instances=None: ["us-central1-a"],
    )
    monkeypatch.setattr(
        gcp,
        "collect_instances",
        lambda _prefix, _zones, _count: (_ for _ in ()).throw(
            TimeoutError("IP collection failed")
        ),
    )
    monkeypatch.setattr(gcp, "delete_instances", lambda items: deleted.extend(items))

    def capture_upload(
        file_name: str, _bucket: str, _object_name: str | None = None
    ) -> None:
        if Path(file_name).name == "manifest.json":
            manifests.append(json.loads(Path(file_name).read_text(encoding="utf-8")))

    monkeypatch.setattr(gcp, "send_to_cloud_storage", capture_upload)

    with pytest.raises(TimeoutError, match="IP collection failed"):
        gcp.run_gcp_scamper(
            str(tmp_path / "logs"),
            "gcp-test",
            max_instances=1,
            regions=("us-central1",),
        )

    assert deleted == [("gcp-test-us-central1-a", "", "us-central1-a")]
    assert manifests[0]["complete"] is False
    assert manifests[0]["failed_nodes"] == ["gcp-test-us-central1-a"]


def test_build_plan_uses_stable_bucket_and_per_run_prefix() -> None:
    plan = gcp.build_plan("gcp-test", "logs")

    assert plan["bucket"] == gcp.settings.SCAMPER_RESULTS_BUCKET
    assert plan["object_prefix"] == "runs/gcp-test"
    assert plan["network_tier"] == "STANDARD"
    assert plan["smoke_test"]["enabled"] is True


def test_build_plan_can_skip_smoke_probe() -> None:
    plan = gcp.build_plan("gcp-test", "logs", skip_smoke=True)

    assert plan["smoke_test"]["enabled"] is False


def test_gcp_campaign_timeout_defaults_to_48_hours(monkeypatch) -> None:
    class RunningProcess(FakeProcess):
        def poll(self) -> None:
            return None

    process = RunningProcess()
    clock = iter((100.0, 172900.0))
    monkeypatch.delenv("SCAMPER_GCP_SCAMPER_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(gcp, "missing_uploaded_artifacts", lambda *_args: ["missing"])
    monkeypatch.setattr(gcp.time, "monotonic", lambda: next(clock))

    with pytest.raises(TimeoutError, match="172800.0 seconds"):
        gcp.wait_for_campaign_processes(
            [(process, {"name": "node", "artifact_names": ["missing"]})],
            "bucket",
            lambda _info, _complete: None,
        )

    assert process.terminated is True


def test_wait_for_campaign_processes_cleans_each_worker_when_ready(
    monkeypatch,
) -> None:
    class RunningProcess(FakeProcess):
        def poll(self) -> None:
            return None

    first = RunningProcess()
    second = RunningProcess()
    second_checks = 0
    terminal_events: list[tuple[str, bool, bool]] = []

    def missing_artifacts(_bucket: str, names: list[str]) -> list[str]:
        nonlocal second_checks
        if names[0].startswith("node-a"):
            return []
        second_checks += 1
        return names if second_checks == 1 else []

    def on_terminal(info: dict[str, object], complete: bool) -> None:
        terminal_events.append((str(info["name"]), complete, second.terminated))

    monkeypatch.setattr(gcp, "missing_uploaded_artifacts", missing_artifacts)
    monkeypatch.setattr(gcp, "incomplete_uploaded_statuses", lambda *_args: [])
    monkeypatch.setattr(gcp.time, "sleep", lambda _seconds: None)

    exits = gcp.wait_for_campaign_processes(
        [
            (
                first,
                {
                    "name": "node-a",
                    "artifact_names": ["node-a.status.json"],
                },
            ),
            (
                second,
                {
                    "name": "node-b",
                    "artifact_names": ["node-b.status.json"],
                },
            ),
        ],
        "bucket",
        on_terminal,
    )

    assert exits == [0, 0]
    assert terminal_events == [
        ("node-a", True, False),
        ("node-b", True, True),
    ]
    assert first.terminated is True
    assert second.terminated is True


def test_wait_for_campaign_processes_isolates_failed_worker(
    monkeypatch,
) -> None:
    class RunningProcess(FakeProcess):
        def poll(self) -> None:
            return None

    failed = RunningProcess()
    healthy = RunningProcess()
    healthy_checks = 0
    terminal_events: list[tuple[str, bool, bool]] = []

    def missing_artifacts(_bucket: str, names: list[str]) -> list[str]:
        nonlocal healthy_checks
        if names[0].startswith("failed"):
            return []
        healthy_checks += 1
        return names if healthy_checks == 1 else []

    def incomplete_statuses(_bucket: str, names: list[str]) -> list[str]:
        return names if names[0].startswith("failed") else []

    def on_terminal(info: dict[str, object], complete: bool) -> None:
        terminal_events.append((str(info["name"]), complete, healthy.terminated))

    monkeypatch.setattr(gcp, "missing_uploaded_artifacts", missing_artifacts)
    monkeypatch.setattr(gcp, "incomplete_uploaded_statuses", incomplete_statuses)
    monkeypatch.setattr(gcp.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="incomplete GCP campaign status"):
        gcp.wait_for_campaign_processes(
            [
                (
                    failed,
                    {
                        "name": "failed-node",
                        "artifact_names": ["failed.status.json"],
                    },
                ),
                (
                    healthy,
                    {
                        "name": "healthy-node",
                        "artifact_names": ["healthy.status.json"],
                    },
                ),
            ],
            "bucket",
            on_terminal,
        )

    assert terminal_events == [
        ("failed-node", False, False),
        ("healthy-node", True, True),
    ]
    assert failed.terminated is True
    assert healthy.terminated is True


def test_build_plan_normalizes_custom_object_prefix() -> None:
    plan = gcp.build_plan(
        "gcp-test",
        "logs",
        bucket_name="custom-results",
        object_prefix="/experiments/gcp-test/",
    )

    assert plan["bucket"] == "custom-results"
    assert plan["object_prefix"] == "experiments/gcp-test"


def test_normalized_object_prefix_rejects_parent_traversal() -> None:
    with pytest.raises(gcp.argparse.ArgumentTypeError, match="object prefix"):
        gcp.normalized_object_prefix("runs/../elsewhere")
