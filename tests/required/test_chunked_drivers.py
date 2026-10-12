"""Drivers run campaigns in chunks: every worker is told the chunk size, and a
chunked node is expected to leave a measurement summary instead of one warts."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from providers import settings
from providers.aws import driver as aws
from providers.azure import driver as azure
from providers.common.targets import PreparedTarget, PreparedTargets
from providers.gcp import driver as gcp

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVERS = {"aws": aws, "azure": azure, "gcp": gcp}


def test_chunk_size_defaults_on_and_rejects_negative_values(monkeypatch) -> None:
    try:
        monkeypatch.delenv("SCAMPER_CHUNK_TARGETS", raising=False)
        assert importlib.reload(settings).SCAMPER_CHUNK_TARGETS == 200000
        monkeypatch.setenv("SCAMPER_CHUNK_TARGETS", "0")
        assert importlib.reload(settings).SCAMPER_CHUNK_TARGETS == 0
        monkeypatch.setenv("SCAMPER_CHUNK_TARGETS", "-5")
        with pytest.raises(ValueError):
            importlib.reload(settings)
    finally:
        monkeypatch.delenv("SCAMPER_CHUNK_TARGETS", raising=False)
        importlib.reload(settings)


def _targets() -> PreparedTargets:
    trace = PreparedTarget("v4", "v4@sha256:a", "/tmp/trace.txt", "a" * 64, 2)
    rr = PreparedTarget("rr", "rr@sha256:b", "/tmp/rr.txt", "b" * 64, 1)
    return PreparedTargets(trace, rr, None, None, None)


def test_every_driver_tells_its_workers_the_chunk_size(monkeypatch) -> None:
    monkeypatch.setattr(settings, "SCAMPER_CHUNK_TARGETS", 1234)
    targets = _targets()
    common = {"output_prefix": "node", "bucket_name": "results", "object_prefix": "runs/t",
              "targets": targets, "trace_rate": 100, "rr_rate": 10, "rr_timeout": 2,
              "measurements": ("trace",)}
    commands = [
        aws.remote_campaign_command("/tmp/trace.txt", "/tmp/rr.txt", region="r", node="n", **common),
        azure.remote_scamper_command("/tmp/trace.txt", "/tmp/rr.txt", location="r", node="n", **common),
        gcp.remote_campaign_command(
            "/tmp/trace.txt", "/tmp/rr.txt", "node", "results", "runs/t", region="r", node="n",
            trace_target_source="v4", trace_target_version="v4@sha256:a", trace_target_count=2,
            trace_target_sha256="a" * 64, rr_target_source="rr", rr_target_version="rr@sha256:b",
            rr_target_count=1, rr_target_sha256="b" * 64, trace_rate=100, rr_rate=10, rr_timeout=2,
            measurements=("trace",), probe_payload=None, measurement_contact=None,
            do_not_probe_version=None,
        ),
    ]
    for command in commands:
        assert "SCAMPER_CHUNK_TARGETS=1234" in command


@pytest.mark.parametrize("name", sorted(DRIVERS))
def test_a_chunked_node_is_expected_to_leave_a_summary_not_one_warts(name: str) -> None:
    expected = DRIVERS[name].expected_campaign_artifacts
    single = expected("runs/t/nodes/r/n", "out", ("trace", "trace6"))
    chunked = expected("runs/t/nodes/r/n", "out", ("trace", "trace6"), chunked=True)
    assert "runs/t/nodes/r/n/out.trace.warts" in single
    assert not any(item.endswith(".warts") for item in chunked)
    for measurement in ("trace", "trace6"):
        assert f"runs/t/nodes/r/n/out.{measurement}.metadata.json" in chunked
        assert f"runs/t/nodes/r/n/out.{measurement}.targets.txt" in chunked
    assert "runs/t/nodes/r/n/out.status.json" in chunked


@pytest.mark.parametrize("name", sorted(DRIVERS))
def test_each_driver_decides_chunked_mode_from_the_setting(name: str) -> None:
    source = (REPO_ROOT / f"providers/{name}/driver.py").read_text(encoding="utf-8")
    call_sites = source.count("expected_campaign_artifacts(") - 1  # minus the definition
    assert call_sites >= 1
    assert source.count("chunked=settings.SCAMPER_CHUNK_TARGETS > 0") == call_sites


def test_a_run_can_set_its_own_chunk_size_at_submission() -> None:
    import argparse

    from controller import submit

    args = argparse.Namespace(provider="aws", run_id="r", worker_machine_type="t3.micro", chunk_targets=300)
    assert "--setenv=SCAMPER_CHUNK_TARGETS=300" in submit.systemd_command(args, ["python", "aws.py"])
    args.chunk_targets = None  # the controller default applies
    assert not any("SCAMPER_CHUNK_TARGETS" in item for item in submit.systemd_command(args, ["python"]))


def test_submit_accepts_a_chunk_size_option() -> None:
    from controller import submit

    source = (REPO_ROOT / "controller/submit.py").read_text(encoding="utf-8")
    assert '"--chunk-targets"' in source
    manage = (REPO_ROOT / "controller/manage.py").read_text(encoding="utf-8")
    assert '"--chunk-targets"' in manage
