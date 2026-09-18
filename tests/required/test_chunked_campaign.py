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
    counts = [len(c.read_text().strip().splitlines()) for c in chunks]
    assert counts == [100, 100, 50]
    # No target may be dropped or duplicated by the split.
    seen = [line for c in chunks for line in c.read_text().splitlines()]
    assert seen == source.read_text().splitlines()


def test_chunking_disabled_returns_the_original_file(tmp_path: Path) -> None:
    source = write_targets(tmp_path / "t.txt", 10)
    assert rc.chunk_target_file(source, 0, tmp_path / "out", "trace") == [source]


def test_completed_chunks_are_parsed_from_bucket_listings(tmp_path: Path) -> None:
    listing = tmp_path / "done.txt"
    listing.write_text(
        "gs://bucket/runs/x/node.trace.part-00001.jsonl.gz\n"
        "gs://bucket/runs/x/node.trace.part-00002.jsonl.gz\n"
        "\n"
        "7\n",
        encoding="utf-8",
    )
    assert rc.completed_chunk_indices(listing) == {1, 2, 7}


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
    # Three chunks, each uploading its JSONL and metadata as it finishes.
    assert uploads == [
        "out.trace.part-00001.jsonl.gz", "out.trace.part-00001.metadata.json",
        "out.trace.part-00002.jsonl.gz", "out.trace.part-00002.metadata.json",
        "out.trace.part-00003.jsonl.gz", "out.trace.part-00003.metadata.json",
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
