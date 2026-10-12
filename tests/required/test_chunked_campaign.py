from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "experiments/common"))
import run_campaign as rc


def write_targets(path: Path, count: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"10.0.0.{i}\n" for i in range(count)), encoding="utf-8")
    return path


def test_chunking_splits_targets_and_keeps_every_one(tmp_path: Path) -> None:
    source = write_targets(tmp_path / "t.txt", 250)
    chunks = rc.chunk_target_file(source, 100, tmp_path / "out", "trace")
    assert [c.name for c in chunks] == [
        "out.trace.part-00001.targets.txt",
        "out.trace.part-00002.targets.txt",
        "out.trace.part-00003.targets.txt",
    ]
    named = rc.chunk_target_file(source, 100, tmp_path / "out", "trace", ".plan-abc123def456")
    assert named[0].name == "out.trace.plan-abc123def456.part-00001.targets.txt"
    counts = [len(c.read_text().strip().splitlines()) for c in chunks]
    assert counts == [100, 100, 50]
    # No target may be dropped or duplicated by the split.
    seen = [line for c in chunks for line in c.read_text().splitlines()]
    assert seen == source.read_text().splitlines()


def test_chunking_disabled_returns_the_original_file(tmp_path: Path) -> None:
    source = write_targets(tmp_path / "t.txt", 10)
    assert rc.chunk_target_file(source, 0, tmp_path / "out", "trace") == [source]


def test_only_a_committed_chunk_counts_as_done(tmp_path: Path) -> None:
    """Metadata uploads last, so it is the commit marker.

    A lone .jsonl.gz means the upload was cut short; that chunk must be redone
    or its tail would be silently missing from the results.
    """
    listing = tmp_path / "done.txt"
    listing.write_text(
        "gs://b/x/node.trace.plan-aaaaaaaaaaaa.part-00001.jsonl.gz\n"
        "gs://b/x/node.trace.plan-aaaaaaaaaaaa.part-00001.metadata.json\n"
        "gs://b/x/node.trace.plan-aaaaaaaaaaaa.part-00002.jsonl.gz\n"
        "gs://b/x/node.trace.plan-aaaaaaaaaaaa.part-00005.metadata.json\n",
        encoding="utf-8",
    )
    assert rc.completed_chunk_indices(listing, "aaaaaaaaaaaa") == {1, 5}


def test_chunks_from_a_different_plan_are_never_skipped(tmp_path: Path) -> None:
    """A different target file or chunk size reorders everything.

    Chunk 3 of another plan covers different addresses, so trusting its index
    would leave real gaps. Nothing is skipped unless the plan matches.
    """
    listing = tmp_path / "done.txt"
    listing.write_text(
        "gs://b/x/node.trace.plan-bbbbbbbbbbbb.part-00003.metadata.json\n", encoding="utf-8"
    )
    assert rc.completed_chunk_indices(listing, "aaaaaaaaaaaa") == set()
    assert rc.completed_chunk_indices(listing, "bbbbbbbbbbbb") == {3}


def test_missing_completed_file_means_start_from_scratch(tmp_path: Path) -> None:
    assert rc.completed_chunk_indices(None) == set()
    assert rc.completed_chunk_indices(tmp_path / "absent.txt") == set()


def test_each_chunk_is_uploaded_as_it_completes(tmp_path: Path, monkeypatch) -> None:
    """The point of chunking: value lands in the bucket during the run."""
    shuffled = write_targets(tmp_path / "s.txt", 5)
    uploads: list[str] = []
    order: list[str] = []

    def fake_run(command, check=False, **kwargs):
        if command and command[0] == "upload":
            uploads.append(Path(command[1]).name)
            order.append("upload")
        else:
            order.append("measure")
            Path(command[command.index("-o") + 1]).write_text("warts", encoding="utf-8")
        class Result:
            returncode = 0
        return Result()

    def fake_convert(warts_path, jsonl_path, measurement, stderr_path):
        with gzip.open(jsonl_path, "wt", encoding="utf-8") as handle:
            handle.write('{"type":"trace"}\n')
        return 0, "", {"records": 1}

    monkeypatch.setattr(rc.subprocess, "run", fake_run)
    monkeypatch.setattr(rc, "convert_chunk_to_jsonl", fake_convert)

    result = rc.run_measurement_in_chunks(
        "trace", shuffled_path=shuffled, output_prefix=tmp_path / "out",
        rate_pps=1000, rr_timeout_seconds=2.0, payload_text=None,
        chunk_size=2, completed=set(), checkpoint_command=["upload", "{artifact}"],
    )
    assert result["chunk_count"] == 3
    # Three chunks, each uploading its raw warts (what mapping reads) and then
    # its metadata, the commit marker, as it finishes. JSONL stays local: it is
    # only the validation step, and uploading it would double the egress.
    assert uploads == [
        "out.trace.part-00001.warts", "out.trace.part-00001.metadata.json",
        "out.trace.part-00002.warts", "out.trace.part-00002.metadata.json",
        "out.trace.part-00003.warts", "out.trace.part-00003.metadata.json",
    ]
    # Uploads are interleaved with measurement, not batched at the end.
    assert order[:3] == ["measure", "upload", "upload"]


def test_a_resumed_run_skips_chunks_already_in_the_bucket(tmp_path: Path, monkeypatch) -> None:
    shuffled = write_targets(tmp_path / "s.txt", 6)
    measured: list[str] = []

    def fake_run(command, check=False, **kwargs):
        if command and command[0] != "upload":
            measured.append(Path(command[command.index("-f") + 1]).name)
            Path(command[command.index("-o") + 1]).write_text("warts", encoding="utf-8")
        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr(rc.subprocess, "run", fake_run)
    monkeypatch.setattr(rc, "convert_chunk_to_jsonl",
                        lambda w, j, m, s: (gzip.open(j, "wt").close(), (0, "", None))[1])

    result = rc.run_measurement_in_chunks(
        "trace", shuffled_path=shuffled, output_prefix=tmp_path / "out",
        rate_pps=1000, rr_timeout_seconds=2.0, payload_text=None,
        chunk_size=2, completed={1, 3}, checkpoint_command=None,
    )
    # Only chunk 2 is re-measured; 1 and 3 were already uploaded.
    assert measured == ["out.trace.part-00002.targets.txt"]
    assert [c.get("skipped") for c in result["chunks"]] == [True, None, True]


def test_chunk_metadata_records_what_the_chunk_covered(tmp_path: Path, monkeypatch) -> None:
    shuffled = write_targets(tmp_path / "s.txt", 3)

    def fake_run(command, check=False, **kwargs):
        Path(command[command.index("-o") + 1]).write_text("warts", encoding="utf-8")
        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr(rc.subprocess, "run", fake_run)
    monkeypatch.setattr(rc, "convert_chunk_to_jsonl",
                        lambda w, j, m, s: (gzip.open(j, "wt").close(), (0, "", None))[1])
    rc.run_measurement_in_chunks(
        "trace", shuffled_path=shuffled, output_prefix=tmp_path / "out",
        rate_pps=1000, rr_timeout_seconds=2.0, payload_text=None,
        chunk_size=2, completed=set(), checkpoint_command=None,
    )
    record = json.loads((tmp_path / "out.trace.part-00001.metadata.json").read_text())
    assert record["chunk"] == 1 and record["chunk_count"] == 2
    assert record["targets"] == 2
    assert record["jsonl_sha256"]


def test_drivers_keep_campaign_sessions_alive() -> None:
    root = Path(__file__).resolve().parents[2]
    from providers import settings
    assert "-oServerAliveInterval=60" in settings.SSH_KEEPALIVE_OPTIONS
    for provider in ("aws", "gcp", "azure"):
        text = (root / f"providers/{provider}/driver.py").read_text(encoding="utf-8")
        assert "SSH_KEEPALIVE_OPTIONS" in text, provider


def test_aws_driver_waits_while_workers_still_measure() -> None:
    root = Path(__file__).resolve().parents[2]
    text = (root / "providers/aws/driver.py").read_text(encoding="utf-8")
    assert "def workers_still_measuring" in text
    # Losing every control channel must not be treated as campaign failure.
    assert "waiting for their own uploads rather than" in text
    assert "still connected" in text


def test_the_same_inputs_always_produce_the_same_plan() -> None:
    """Resume by index is only sound if the ordering is reproducible."""
    a = rc.chunk_plan("abc123", 200_000, "trace")
    b = rc.chunk_plan("abc123", 200_000, "trace")
    assert a == b
    # Any change to the inputs must produce a different plan, so stale chunks
    # are not mistaken for current ones.
    assert rc.chunk_plan("abc123", 100_000, "trace") != a
    assert rc.chunk_plan("def456", 200_000, "trace") != a
    assert rc.chunk_plan("abc123", 200_000, "rr") != a


def test_a_pinned_seed_reproduces_the_shuffle(tmp_path: Path, monkeypatch) -> None:
    source = write_targets(tmp_path / "t.txt", 5)
    seeds = []

    def fake_run(command, check=False, env=None, **kwargs):
        seeds.append(Path(command[2].split("=", 1)[1]).read_bytes()[:8])
        Path(command[5].split("=", 1)[1]).write_text(source.read_text(), encoding="utf-8")
        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr(rc.subprocess, "run", fake_run)
    first, _ = rc.shuffle_targets(source, tmp_path / "a.txt", seed=12345)
    second, _ = rc.shuffle_targets(source, tmp_path / "b.txt", seed=12345)
    assert first == second == 12345
    # Same seed must mean the same random source, hence the same ordering.
    assert seeds[0] == seeds[1]
    third, _ = rc.shuffle_targets(source, tmp_path / "c.txt", seed=99999)
    assert seeds[2] != seeds[0]


def test_a_failed_chunk_is_not_committed(tmp_path: Path, monkeypatch) -> None:
    """A chunk whose scamper failed must stay eligible for re-measurement."""
    shuffled = write_targets(tmp_path / "s.txt", 2)
    uploads: list[str] = []

    def fake_run(command, check=False, **kwargs):
        if command and command[0] == "upload":
            uploads.append(Path(command[1]).name)
            class Ok:
                returncode = 0
            return Ok()
        Path(command[command.index("-o") + 1]).write_text("warts", encoding="utf-8")
        class Failed:
            returncode = 1
        return Failed()

    monkeypatch.setattr(rc.subprocess, "run", fake_run)
    monkeypatch.setattr(rc, "convert_chunk_to_jsonl",
                        lambda w, j, m, s: (gzip.open(j, "wt").close(), (0, "", None))[1])
    rc.run_measurement_in_chunks(
        "trace", shuffled_path=shuffled, output_prefix=tmp_path / "out",
        rate_pps=1000, rr_timeout_seconds=2.0, payload_text=None,
        chunk_size=2, completed=set(), checkpoint_command=["upload", "{artifact}"],
        fingerprint="aaaaaaaaaaaa",
    )
    # The raw warts is uploaded for inspection, but no metadata means not committed.
    assert any(name.endswith(".warts") for name in uploads)
    assert not any(name.endswith(".metadata.json") for name in uploads)



def _ok_run(command, check=False, **kwargs):
    if command and command[0] != "upload":
        Path(command[command.index("-o") + 1]).write_text("warts", encoding="utf-8")
    class Result:
        returncode = 0
    return Result()


def _ok_convert(warts_path, jsonl_path, measurement, stderr_path):
    with gzip.open(jsonl_path, "wt", encoding="utf-8") as handle:
        handle.write('{"type":"trace"}\n')
    return 0, "", {"records": 1}


def test_committed_chunks_leave_nothing_behind_to_reupload(tmp_path: Path, monkeypatch) -> None:
    """The worker's final step uploads every <prefix>.* file; committed chunks must not be among them."""
    shuffled = write_targets(tmp_path / "s.txt", 5)
    monkeypatch.setattr(rc.subprocess, "run", _ok_run)
    monkeypatch.setattr(rc, "convert_chunk_to_jsonl", _ok_convert)
    rc.run_measurement_in_chunks(
        "trace", shuffled_path=shuffled, output_prefix=tmp_path / "out",
        rate_pps=1000, rr_timeout_seconds=2.0, payload_text=None,
        chunk_size=2, completed=set(), checkpoint_command=["upload", "{artifact}"],
    )
    assert sorted(p.name for p in tmp_path.glob("out.trace.part-*")) == []


def test_chunk_metadata_carries_the_fields_mapping_validates(tmp_path: Path, monkeypatch) -> None:
    """scamper-analysis checks measurement, address_family, return_code and target_count."""
    shuffled = write_targets(tmp_path / "s.txt", 3)
    monkeypatch.setattr(rc.subprocess, "run", _ok_run)
    monkeypatch.setattr(rc, "convert_chunk_to_jsonl", _ok_convert)
    rc.run_measurement_in_chunks(
        "trace", shuffled_path=shuffled, output_prefix=tmp_path / "out",
        rate_pps=1000, rr_timeout_seconds=2.0, payload_text=None,
        chunk_size=2, completed=set(), checkpoint_command=None,
    )
    record = json.loads((tmp_path / "out.trace.part-00001.metadata.json").read_text())
    assert record["measurement"] == "trace"
    assert record["address_family"] == 4
    assert record["return_code"] == 0
    assert record["target_count"] == 2


def test_a_chunk_whose_conversion_failed_records_a_failing_return_code(tmp_path: Path, monkeypatch) -> None:
    shuffled = write_targets(tmp_path / "s.txt", 2)
    monkeypatch.setattr(rc.subprocess, "run", _ok_run)
    monkeypatch.setattr(rc, "convert_chunk_to_jsonl",
                        lambda w, j, m, s: (gzip.open(j, "wt").close(), (2, "truncated", None))[1])
    result = rc.run_measurement_in_chunks(
        "trace", shuffled_path=shuffled, output_prefix=tmp_path / "out",
        rate_pps=1000, rr_timeout_seconds=2.0, payload_text=None,
        chunk_size=2, completed=set(), checkpoint_command=None,
    )
    assert result["chunks"][0]["return_code"] != 0
    assert result["committed"] == 0 and result["chunk_count"] == 1


def test_a_measurement_is_complete_only_when_every_chunk_is_committed() -> None:
    done = {"chunk_count": 3, "committed": 2, "chunks": [{"skipped": True}, {"chunk": 2}, {"chunk": 3}]}
    assert rc.chunked_measurement_status({**done, "committed": 3}) == 0
    assert rc.chunked_measurement_status(done) != 0


def test_chunk_mode_writes_the_measurement_summary_drivers_expect(tmp_path: Path) -> None:
    # A real loop result also carries "return_code" (highest scamper exit code).
    result = {"chunked": True, "chunk_size": 2, "chunk_count": 2, "committed": 2, "return_code": 0,
              "chunks": [{"chunk": 1, "return_code": 0}, {"chunk": 2, "return_code": 0}]}
    path = rc.write_chunked_measurement_metadata(
        tmp_path / "out", "trace6", result, {"node": "n1"}, target_count=3,
        plan_fingerprint="aaaaaaaaaaaa",
    )
    assert path.name == "out.trace6.metadata.json"
    summary = json.loads(path.read_text())
    assert summary["chunked"] is True and summary["plan_fingerprint"] == "aaaaaaaaaaaa"
    assert summary["measurement"] == "trace6" and summary["address_family"] == 6
    assert summary["target_count"] == 3 and summary["return_code"] == 0 and summary["node"] == "n1"



def test_an_uncommitted_chunk_fails_the_summary_even_if_scamper_exited_cleanly(tmp_path: Path) -> None:
    result = {"chunked": True, "chunk_size": 2, "chunk_count": 2, "committed": 1, "return_code": 0,
              "chunks": [{"chunk": 1, "return_code": 0}, {"chunk": 2, "return_code": 2}]}
    path = rc.write_chunked_measurement_metadata(
        tmp_path / "out", "trace", result, {}, target_count=3, plan_fingerprint="aaaaaaaaaaaa",
    )
    assert json.loads(path.read_text())["return_code"] != 0


def test_a_chunked_run_checkpoints_its_measurement_summary_and_target_list(tmp_path: Path, monkeypatch) -> None:
    """GCP uploads only status.json at the end, relying on the per-measurement
    checkpoint. In chunk mode that checkpoint must still send the summary and
    the shuffled target list, or the driver waits for them forever."""
    targets = write_targets(tmp_path / "in.txt", 5)
    uploads: list[str] = []

    def fake_run(command, check=False, **kwargs):
        if command and command[0] == "upload":
            uploads.append(Path(command[1]).name)
        else:
            Path(command[command.index("-o") + 1]).write_text("warts", encoding="utf-8")
        class Result:
            returncode = 0
        return Result()

    def fake_shuffle(source, destination, seed=None):
        Path(destination).write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")
        return seed or 1, ["shuf"]

    monkeypatch.setattr(rc.subprocess, "run", fake_run)
    monkeypatch.setattr(rc, "convert_chunk_to_jsonl", _ok_convert)
    monkeypatch.setattr(rc, "shuffle_targets", fake_shuffle)
    code = rc.main([
        "--trace-targets", str(targets), "--rr-targets", str(targets),
        "--output-prefix", str(tmp_path / "out"), "--provider", "gcp", "--region", "r", "--node", "n",
        "--trace-target-source", "test", "--trace-target-version", "v1",
        "--rr-target-source", "test", "--rr-target-version", "v1",
        "--measurements", "trace", "--chunk-targets", "2",
        "--checkpoint-command", "upload", "{artifact}",
    ])
    assert code == 0
    assert "out.trace.metadata.json" in uploads and "out.trace.targets.txt" in uploads
    assert "out.trace.warts" not in uploads  # the chunks carry the raw data
    assert sum(name.endswith(".metadata.json") and ".part-" in name for name in uploads) == 3
    status = json.loads((tmp_path / "out.status.json").read_text())
    assert status["complete"] is True


def test_committed_chunks_are_recognised_from_bucket_object_names() -> None:
    names = [
        "runs/x/nodes/r/old-node/old.trace.plan-aaaaaaaaaaaa.part-00001.metadata.json",
        "runs/x/nodes/r/old-node/old.trace.plan-aaaaaaaaaaaa.part-00002.warts",  # no marker
        "runs/x/nodes/r/new-node/new.trace.plan-aaaaaaaaaaaa.part-00004.metadata.json",
        "runs/x/nodes/r/old-node/old.trace.plan-bbbbbbbbbbbb.part-00003.metadata.json",  # other plan
    ]
    assert rc.completed_chunk_indices_from_names(names, "aaaaaaaaaaaa") == {1, 4}


def test_a_chunked_run_asks_the_bucket_which_chunks_are_done(tmp_path: Path, monkeypatch) -> None:
    """Resume: chunks committed by an earlier worker in this region are skipped."""
    targets = write_targets(tmp_path / "in.txt", 6)
    asked: list[str] = []
    measured: list[str] = []

    def fake_run(command, check=False, **kwargs):
        if command and command[0] != "upload":
            measured.append(Path(command[command.index("-f") + 1]).name)
            Path(command[command.index("-o") + 1]).write_text("warts", encoding="utf-8")
        class Result:
            returncode = 0
        return Result()

    def fake_shuffle(source, destination, seed=None):
        Path(destination).write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")
        return seed or 1, ["shuf"]

    monkeypatch.setattr(rc.subprocess, "run", fake_run)
    monkeypatch.setattr(rc, "convert_chunk_to_jsonl", _ok_convert)
    monkeypatch.setattr(rc, "shuffle_targets", fake_shuffle)
    plan: dict[str, str] = {}
    real_plan = rc.chunk_plan

    def spy_plan(sha, size, measurement):
        fingerprint, seed = real_plan(sha, size, measurement)
        plan["fingerprint"] = fingerprint
        return fingerprint, seed

    monkeypatch.setattr(rc, "chunk_plan", spy_plan)
    # Chunk 2 of this exact plan is already committed in the bucket.
    monkeypatch.setattr(rc, "list_committed_chunk_objects", lambda uri: (asked.append(uri), [
        f"runs/x/nodes/r/old/old.trace.plan-{plan['fingerprint']}.part-00002.metadata.json"])[1])
    rc.main([
        "--trace-targets", str(targets), "--rr-targets", str(targets),
        "--trace-target-source", "test", "--trace-target-version", "v1",
        "--rr-target-source", "test", "--rr-target-version", "v1",
        "--output-prefix", str(tmp_path / "out"), "--provider", "gcp", "--region", "r", "--node", "n",
        "--measurements", "trace", "--chunk-targets", "2",
        "--completed-chunks-prefix", "gs://b/runs/x/nodes/r/",
        "--checkpoint-command", "upload", "{artifact}",
    ])
    assert asked == ["gs://b/runs/x/nodes/r/"]
    assert [name for name in measured if ".part-" in name] == [
        f"out.trace.plan-{plan['fingerprint']}.part-00001.targets.txt",
        f"out.trace.plan-{plan['fingerprint']}.part-00003.targets.txt",
    ]


def test_an_unreadable_bucket_means_measuring_everything(monkeypatch) -> None:
    def broken(prefix_uri: str):
        raise PermissionError("403")
    monkeypatch.setattr(rc, "list_committed_chunk_objects", broken)
    assert rc.bucket_completed_chunks("gs://b/p/", "aaaaaaaaaaaa") == set()


def test_chunk_metadata_carries_the_worker_identity(tmp_path: Path, monkeypatch) -> None:
    """The mapper names the vantage point from metadata (probe, node, probe_ip)."""
    shuffled = write_targets(tmp_path / "s.txt", 2)
    monkeypatch.setattr(rc.subprocess, "run", _ok_run)
    monkeypatch.setattr(rc, "convert_chunk_to_jsonl", _ok_convert)
    rc.run_measurement_in_chunks(
        "trace", shuffled_path=shuffled, output_prefix=tmp_path / "out",
        rate_pps=1000, rr_timeout_seconds=2.0, payload_text=None,
        chunk_size=2, completed=set(), checkpoint_command=None,
        record_extra={"provider": "gcp", "region": "us-west1", "node": "n1", "probe_ip": "34.1.2.3"},
    )
    record = json.loads((tmp_path / "out.trace.part-00001.metadata.json").read_text())
    assert (record["provider"], record["node"], record["probe_ip"]) == ("gcp", "n1", "34.1.2.3")
    assert record["measurement"] == "trace"  # the chunk's own fields still win


def test_chunk_metadata_carries_the_target_provenance_mapping_requires(tmp_path: Path, monkeypatch) -> None:
    """scamper-analysis refuses a warts input whose metadata lacks target_version,
    normalized_target_sha256 or shuffled_target_sha256 (found on real chunks
    from canary-chunk-gcp-20261012a). For a chunk, the shuffled hash is that of
    the targets this chunk actually probed."""
    import hashlib

    shuffled = write_targets(tmp_path / "s.txt", 3)
    monkeypatch.setattr(rc.subprocess, "run", _ok_run)
    monkeypatch.setattr(rc, "convert_chunk_to_jsonl", _ok_convert)
    rc.run_measurement_in_chunks(
        "trace", shuffled_path=shuffled, output_prefix=tmp_path / "out",
        rate_pps=1000, rr_timeout_seconds=2.0, payload_text=None,
        chunk_size=2, completed=set(), checkpoint_command=None,
        record_extra={"target_source": "src", "target_version": "v@sha256:" + "a" * 64,
                      "normalized_target_sha256": "a" * 64},
    )
    record = json.loads((tmp_path / "out.trace.part-00001.metadata.json").read_text())
    chunk_targets = (tmp_path / "out.trace.part-00001.targets.txt").read_bytes()
    assert record["target_version"] == "v@sha256:" + "a" * 64
    assert record["normalized_target_sha256"] == "a" * 64
    assert record["shuffled_target_sha256"] == hashlib.sha256(chunk_targets).hexdigest()


def test_a_chunked_run_passes_the_measurement_provenance_to_every_chunk(tmp_path: Path, monkeypatch) -> None:
    targets = write_targets(tmp_path / "in.txt", 3)
    seen: dict = {}
    real = rc.run_measurement_in_chunks

    def spy(*args, **kwargs):
        seen.update(kwargs.get("record_extra") or {})
        return real(*args, **kwargs)

    def fake_shuffle(source, destination, seed=None):
        Path(destination).write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")
        return seed or 1, ["shuf"]

    monkeypatch.setattr(rc, "run_measurement_in_chunks", spy)
    monkeypatch.setattr(rc.subprocess, "run", _ok_run)
    monkeypatch.setattr(rc, "convert_chunk_to_jsonl", _ok_convert)
    monkeypatch.setattr(rc, "shuffle_targets", fake_shuffle)
    rc.main([
        "--trace-targets", str(targets), "--rr-targets", str(targets),
        "--trace-target-source", "src", "--trace-target-version", "v1",
        "--rr-target-source", "src", "--rr-target-version", "v1",
        "--output-prefix", str(tmp_path / "out"), "--provider", "gcp", "--region", "r", "--node", "n",
        "--measurements", "trace", "--chunk-targets", "2",
    ])
    assert seen["target_version"] == "v1" and seen["target_source"] == "src"
    assert len(seen["normalized_target_sha256"]) == 64


def _fake_converter(tmp_path: Path, monkeypatch, lines: int, bad_line_at: int | None = None) -> Path:
    """A stand-in sc_warts2json on PATH that prints `lines` trace records (~1 KB each)."""
    import os
    import stat
    import sys as _sys

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "sc_warts2json"
    bad = "" if bad_line_at is None else f"\n    if i == {bad_line_at}: print('{{not json')"
    script.write_text(
        f"#!{_sys.executable}\n"
        "pad = 'x' * 1000\n"
        f"for i in range({lines}):{bad}\n"
        "    print('{\"type\": \"trace\", \"dst\": \"10.0.%d.%d\", \"pad\": \"%s\"}' % (i // 250 % 250, i % 250, pad))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    warts = tmp_path / "c.warts"
    warts.write_text("warts", encoding="utf-8")
    return warts


def test_chunk_conversion_streams_instead_of_holding_every_line(tmp_path: Path, monkeypatch) -> None:
    """Holding a 200k-traceroute chunk's JSON in memory OOM-killed every 1 GiB
    Azure worker and AWS workers on 2026-10-12. Peak memory must not grow with
    the chunk: 60k lines of ~1 KB would be ~60 MB if retained."""
    import tracemalloc

    warts = _fake_converter(tmp_path, monkeypatch, lines=60000)
    tracemalloc.start()
    code, _stderr, summary = rc.convert_chunk_to_jsonl(
        warts, tmp_path / "c.jsonl.gz", "trace", tmp_path / "c.err")
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert code == 0
    assert summary is not None
    assert peak < 15_000_000, f"peak {peak / 1e6:.1f} MB: conversion is holding the chunk in memory"
    with gzip.open(tmp_path / "c.jsonl.gz", "rt", encoding="utf-8") as handle:
        assert sum(1 for _ in handle) == 60000


def test_a_bad_line_still_writes_the_whole_jsonl_and_drops_only_the_summary(tmp_path: Path, monkeypatch) -> None:
    warts = _fake_converter(tmp_path, monkeypatch, lines=100, bad_line_at=50)
    code, _stderr, summary = rc.convert_chunk_to_jsonl(
        warts, tmp_path / "c.jsonl.gz", "trace", tmp_path / "c.err")
    assert code == 0 and summary is None
    with gzip.open(tmp_path / "c.jsonl.gz", "rt", encoding="utf-8") as handle:
        assert sum(1 for _ in handle) == 101
