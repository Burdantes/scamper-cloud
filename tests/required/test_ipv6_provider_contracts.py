from __future__ import annotations

from providers.aws import driver as aws
from providers.azure import driver as azure
from providers.common.targets import PreparedTarget, PreparedTargets
from providers.gcp import driver as gcp


def prepared_targets() -> PreparedTargets:
    trace = PreparedTarget("v4-trace", "v4-trace@sha256:a", "/tmp/trace.txt", "a" * 64, 2)
    rr = PreparedTarget("v4-rr", "v4-rr@sha256:b", "/tmp/rr.txt", "b" * 64, 1)
    trace6 = PreparedTarget("tum-hitlist", "tum@sha256:c", "/tmp/trace6.txt", "c" * 64, 3)
    return PreparedTargets(trace, rr, trace6, None, None)


def test_aws_and_azure_worker_commands_forward_trace6_contract() -> None:
    targets = prepared_targets()
    common = {
        "output_prefix": "node",
        "bucket_name": "results",
        "object_prefix": "runs/test",
        "targets": targets,
        "trace_rate": 100,
        "rr_rate": 10,
        "rr_timeout": 2,
        "measurements": ("trace", "trace6", "rr"),
        "trace6_target_file": targets.trace6.normalized_file,
        "trace6_rate": 25,
    }

    aws_command = aws.remote_campaign_command(
        targets.trace.normalized_file,
        targets.rr.normalized_file,
        region="us-east-1",
        node="aws-node",
        **common,
    )
    azure_command = azure.remote_scamper_command(
        targets.trace.normalized_file,
        targets.rr.normalized_file,
        location="eastus",
        node="azure-node",
        **common,
    )

    for command in (aws_command, azure_command):
        assert "SCAMPER_TRACE6_TARGET_COUNT=3" in command
        assert "SCAMPER_TRACE6_TARGET_SHA256=" + "c" * 64 in command
        assert "SCAMPER_TRACE6_RATE_PPS=25" in command
        assert "trace6.txt node results runs/test" in command


def test_gcp_worker_command_forwards_trace6_contract() -> None:
    command = gcp.remote_campaign_command(
        "/tmp/trace.txt",
        "/tmp/rr.txt",
        "node",
        "results",
        "runs/test",
        region="us-central1",
        node="gcp-node",
        trace_target_source="v4-trace",
        trace_target_version="v4@sha256:a",
        trace_target_count=2,
        trace_target_sha256="a" * 64,
        rr_target_source="v4-rr",
        rr_target_version="rr@sha256:b",
        rr_target_count=1,
        rr_target_sha256="b" * 64,
        trace_rate=100,
        rr_rate=10,
        rr_timeout=2,
        measurements=("trace", "trace6", "rr"),
        trace6_target_file="/tmp/trace6.txt",
        trace6_target_source="tum-hitlist",
        trace6_target_version="tum@sha256:c",
        trace6_target_count=3,
        trace6_target_sha256="c" * 64,
        trace6_rate=25,
        probe_payload=None,
        measurement_contact=None,
        do_not_probe_version=None,
    )

    assert "SCAMPER_TRACE6_TARGET_COUNT=3" in command
    assert "SCAMPER_TRACE6_RATE_PPS=25" in command
    assert "trace.txt rr.txt trace6.txt node results runs/test" in command


def test_provider_network_paths_request_native_dual_stack() -> None:
    assert "Ipv6AddressCount" in open(aws.__file__, encoding="utf-8").read()
    assert "DIRECT_IPV6" in open(gcp.__file__, encoding="utf-8").read()
    assert 'address_family="IPv6"' in open(azure.__file__, encoding="utf-8").read()
