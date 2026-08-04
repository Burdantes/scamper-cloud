from __future__ import annotations

import importlib.util
import io
import json
import hashlib
from pathlib import Path

import pytest


def load_runner():
    path = Path(__file__).parents[2] / "experiments" / "common" / "run_campaign.py"
    spec = importlib.util.spec_from_file_location("vm_campaign_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Completed:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


def perform_fake_shuffle(command: list[str], *, reverse: bool = False) -> None:
    output_arg = next(arg for arg in command if arg.startswith("--output="))
    output_path = Path(output_arg.split("=", 1)[1])
    source_path = Path(command[-1])
    targets = source_path.read_text(encoding="utf-8").splitlines()
    if reverse:
        targets.reverse()
    output_path.write_text("\n".join(targets) + "\n", encoding="utf-8")


def parsed_summary(
    measurement: str,
    destination_count: int,
    *,
    rr_requested: bool = True,
) -> dict:
    record_type = "ping" if measurement == "rr" else "trace"
    return {
        "measurement": measurement,
        "record_count": destination_count,
        "destination_count": destination_count,
        "record_types": {record_type: destination_count},
        "record_route_requested_records": (
            destination_count if measurement == "rr" and rr_requested else 0
        ),
        "record_route_responses_with_data": (
            destination_count if measurement == "rr" and rr_requested else 0
        ),
        "sample": {"type": record_type, "dst": "192.0.2.1"},
    }


def test_registered_target_contract_uses_hash_without_reparsing(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_runner()
    targets = tmp_path / "targets.txt"
    targets.write_text("192.0.2.1\n198.51.100.2\n", encoding="utf-8")
    digest = hashlib.sha256(targets.read_bytes()).hexdigest()
    monkeypatch.setattr(
        module,
        "validate_and_count_targets",
        lambda _path: (_ for _ in ()).throw(AssertionError("unexpected strict parse")),
    )

    count, actual_digest, method = module.verify_target_contract(
        targets,
        expected_count=2,
        expected_sha256=digest,
    )

    assert count == 2
    assert actual_digest == digest
    assert method == "registered-sha256"


def test_registered_target_contract_rejects_hash_mismatch(tmp_path: Path) -> None:
    module = load_runner()
    targets = tmp_path / "targets.txt"
    targets.write_text("192.0.2.1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        module.verify_target_contract(
            targets,
            expected_count=1,
            expected_sha256="0" * 64,
        )


def test_converter_streams_json_without_retaining_decoded_file(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_runner()
    records = [
        {"type": "ping", "dst": "192.0.2.1", "flags": ["v4rr"]},
        {"type": "ping", "dst": "192.0.2.2", "flags": ["v4rr"]},
    ]

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.StringIO(
                "".join(json.dumps(record) + "\n" for record in records)
            )

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(
        module.subprocess, "Popen", lambda *args, **kwargs: FakeProcess()
    )
    stderr_path = tmp_path / "converter.stderr"

    return_code, stderr, summary = module.convert_and_summarize(
        tmp_path / "results.warts", "rr", stderr_path
    )

    assert return_code == 0
    assert stderr == ""
    assert summary is not None
    assert summary["destination_count"] == 2
    assert summary["record_route_requested_records"] == 2
    assert not stderr_path.exists()


def test_runner_executes_trace_and_rr_with_independent_shuffles(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_runner()
    targets = tmp_path / "targets.txt"
    targets.write_text(
        "192.0.2.1\n192.0.2.2\n192.0.2.3\n192.0.2.4\n",
        encoding="utf-8",
    )
    commands: list[list[str]] = []
    seeds = iter((1001, 2002))
    shuffle_count = 0

    def fake_run(command, check, **kwargs):
        nonlocal shuffle_count
        commands.append(command)
        if command[0] == "sort":
            perform_fake_shuffle(command, reverse=shuffle_count == 1)
            shuffle_count += 1
            return Completed()
        if command[0] == "scamper":
            Path(command[command.index("-o") + 1]).write_bytes(b"warts")
            return Completed()
        if command[0] == "checkpoint-upload":
            return Completed()
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(module.secrets, "randbits", lambda _bits: next(seeds))
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        module,
        "convert_and_summarize",
        lambda _warts, measurement, _stderr: (
            0,
            "",
            parsed_summary(measurement, 4),
        ),
    )

    output_prefix = tmp_path / "results" / "aws-node-203.0.113.10"
    result = module.main(
        [
            "--targets",
            str(targets),
            "--output-prefix",
            str(output_prefix),
            "--provider",
            "aws",
            "--region",
            "ap-south-2",
            "--node",
            "node-a",
            "--target-source",
            "/source/targets.tsv",
            "--target-version",
            "targets.tsv@sha256:abc",
            "--trace-rate",
            "100",
            "--rr-rate",
            "10",
            "--rr-timeout",
            "2",
            "--probe-payload",
            "Academic probing. Opt out: research@example.edu",
            "--measurement-contact",
            "research@example.edu",
            "--do-not-probe-version",
            "do-not-probe.txt@sha256:def",
            "--checkpoint-command",
            "checkpoint-upload",
            "{measurement}",
            "{artifact_name}",
        ]
    )

    assert result == 0
    scamper_commands = [command for command in commands if command[0] == "scamper"]
    payload_hex = "Academic probing. Opt out: research@example.edu".encode(
        "ascii"
    ).hex()
    assert scamper_commands[0][2] == (
        f"trace -m 20 -g 8 -w 3 -q 2 -P ICMP -p {payload_hex}"
    )
    assert scamper_commands[0][4] == "100"
    assert scamper_commands[1][2] == (
        f"ping -P icmp-echo -R -c 1 -W 2 -B {payload_hex}"
    )
    assert scamper_commands[1][4] == "10"
    checkpoint_commands = [
        command for command in commands if command[0] == "checkpoint-upload"
    ]
    assert [command[1] for command in checkpoint_commands] == [
        "trace",
        "trace",
        "trace",
        "rr",
        "rr",
        "rr",
    ]
    assert [Path(command[2]).name for command in checkpoint_commands] == [
        f"{output_prefix.name}.trace.warts",
        f"{output_prefix.name}.trace.metadata.json",
        f"{output_prefix.name}.trace.targets.txt",
        f"{output_prefix.name}.rr.warts",
        f"{output_prefix.name}.rr.metadata.json",
        f"{output_prefix.name}.rr.targets.txt",
    ]
    trace_checkpoint_end = commands.index(checkpoint_commands[2])
    rr_scamper_index = commands.index(scamper_commands[1])
    assert trace_checkpoint_end < rr_scamper_index

    trace_metadata = json.loads(
        Path(f"{output_prefix}.trace.metadata.json").read_text(encoding="utf-8")
    )
    rr_metadata = json.loads(
        Path(f"{output_prefix}.rr.metadata.json").read_text(encoding="utf-8")
    )
    status = json.loads(
        Path(f"{output_prefix}.status.json").read_text(encoding="utf-8")
    )
    assert trace_metadata["shuffle_seed"] == 1001
    assert rr_metadata["shuffle_seed"] == 2002
    assert trace_metadata["shuffle_method"] == "gnu-sort-random-external"
    assert trace_metadata["shuffle_memory_limit"] == "128M"
    assert (
        trace_metadata["shuffled_target_sha256"]
        != rr_metadata["shuffled_target_sha256"]
    )
    assert rr_metadata["record_route_requested"] is True
    assert rr_metadata["probe_count_per_destination"] == 1
    assert rr_metadata["probe_payload_hex"] == payload_hex
    assert rr_metadata["measurement_contact"] == "research@example.edu"
    assert rr_metadata["do_not_probe_version"] == "do-not-probe.txt@sha256:def"
    assert rr_metadata["parsed_summary"]["record_route_requested_records"] == 4
    assert rr_metadata["parsed_summary"]["record_route_responses_with_data"] == 4
    assert rr_metadata["decoded_output_retained"] is False
    assert not Path(f"{output_prefix}.rr.jsonl").exists()
    assert status["complete"] is True


def test_runner_rejects_rr_output_without_v4rr_flag(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_runner()
    targets = tmp_path / "targets.txt"
    targets.write_text("192.0.2.1\n", encoding="utf-8")

    def fake_run(command, check, **kwargs):
        if command[0] == "sort":
            perform_fake_shuffle(command)
            return Completed()
        if command[0] == "scamper":
            Path(command[command.index("-o") + 1]).write_bytes(b"warts")
            return Completed()
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        module,
        "convert_and_summarize",
        lambda _warts, measurement, _stderr: (
            0,
            "",
            parsed_summary(measurement, 1, rr_requested=False),
        ),
    )
    output_prefix = tmp_path / "results" / "node"
    result = module.main(
        [
            "--targets",
            str(targets),
            "--output-prefix",
            str(output_prefix),
            "--provider",
            "aws",
            "--region",
            "test-region",
            "--node",
            "node",
            "--target-source",
            "targets.tsv",
            "--target-version",
            "sha256:test",
            "--measurements",
            "rr",
        ]
    )

    assert result == 65
    metadata = json.loads(
        Path(f"{output_prefix}.rr.metadata.json").read_text(encoding="utf-8")
    )
    assert "v4rr" in metadata["converter_stderr"]
    assert metadata["return_code"] == 65


def test_runner_uses_distinct_trace_and_rr_target_sets(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_runner()
    trace_targets = tmp_path / "trace.txt"
    trace_targets.write_text("192.0.2.1\n198.51.100.1\n", encoding="utf-8")
    rr_targets = tmp_path / "rr.txt"
    rr_targets.write_text("203.0.113.1\n", encoding="utf-8")

    def fake_run(command, check, **kwargs):
        if command[0] == "sort":
            perform_fake_shuffle(command)
            return Completed()
        if command[0] == "scamper":
            Path(command[command.index("-o") + 1]).write_bytes(b"warts")
            return Completed()
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        module,
        "convert_and_summarize",
        lambda _warts, measurement, _stderr: (
            0,
            "",
            parsed_summary(measurement, 2 if measurement == "trace" else 1),
        ),
    )
    output_prefix = tmp_path / "results" / "node"

    result = module.main(
        [
            "--trace-targets",
            str(trace_targets),
            "--rr-targets",
            str(rr_targets),
            "--output-prefix",
            str(output_prefix),
            "--provider",
            "gcp",
            "--region",
            "us-central1",
            "--node",
            "node",
            "--trace-target-source",
            "trace-source",
            "--trace-target-version",
            "trace-version",
            "--rr-target-source",
            "rr-source",
            "--rr-target-version",
            "rr-version",
        ]
    )

    assert result == 0
    status = json.loads(Path(f"{output_prefix}.status.json").read_text())
    assert status["target_sets"]["trace"]["target_count"] == 2
    assert status["target_sets"]["rr"]["target_count"] == 1
