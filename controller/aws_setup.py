"""Prepare and verify the AWS regions used by the monthly controller."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from controller.aws_federation import ROLE_ARN_RE
from providers import settings
from providers.aws import driver


def _environment_errors(environment: Mapping[str, str]) -> list[str]:
    errors: list[str] = []
    role_arn = environment.get("AWS_ROLE_ARN", "").strip()
    expected_account = environment.get("AWS_EXPECTED_ACCOUNT_ID", "").strip()
    audience = environment.get("AWS_GCP_AUDIENCE", "").strip()
    if not ROLE_ARN_RE.fullmatch(role_arn):
        errors.append("AWS credentials require a valid AWS_ROLE_ARN for Google federation")
    if not re.fullmatch(r"\d{12}", expected_account):
        errors.append("AWS_EXPECTED_ACCOUNT_ID must be a 12-digit account ID")
    elif role_arn and f"::{expected_account}:role/" not in role_arn:
        errors.append("AWS_ROLE_ARN and AWS_EXPECTED_ACCOUNT_ID name different accounts")
    if not audience:
        errors.append("AWS_GCP_AUDIENCE is not configured")
    cidr = environment.get("SCAMPER_AWS_SSH_CIDR", "").strip()
    try:
        network = ipaddress.ip_network(cidr, strict=True)
        if network.version != 4 or network.prefixlen != 32:
            raise ValueError
    except ValueError:
        errors.append("SCAMPER_AWS_SSH_CIDR must be the controller's public IPv4 /32")
    public_key = Path(f"{settings.AWS_SCAMPER_SSH_KEY}.pub")
    if not public_key.is_file():
        errors.append(f"AWS controller public key does not exist: {public_key}")
    return errors


def _expected_assumed_role_arn(role_arn: str) -> str:
    match = ROLE_ARN_RE.fullmatch(role_arn)
    assert match is not None
    return f"arn:aws:sts::{match.group('account')}:assumed-role/{match.group('role')}/"


def _region_errors(region: str, cidr: str) -> list[str]:
    client = driver.ec2_client(region)
    errors: list[str] = []
    vpcs = client.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        return [f"{region}: no default VPC exists"]
    vpc_id = vpcs[0]["VpcId"]
    subnets = client.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "default-for-az", "Values": ["true"]},
        ]
    )["Subnets"]
    if not subnets or any(not subnet.get("MapPublicIpOnLaunch") for subnet in subnets):
        errors.append(f"{region}: default subnets must assign public IPv4 addresses")

    public_key = Path(f"{settings.AWS_SCAMPER_SSH_KEY}.pub").read_text(encoding="ascii")
    expected_fingerprint = driver.ssh_public_key_fingerprint(public_key)
    try:
        pair = client.describe_key_pairs(KeyNames=[driver.KEY_NAME])["KeyPairs"][0]
        if pair.get("KeyFingerprint") != expected_fingerprint:
            errors.append(f"{region}: {driver.KEY_NAME} does not match the controller SSH key")
    except driver.client_error_type() as error:
        if error.response.get("Error", {}).get("Code") == "InvalidKeyPair.NotFound":
            errors.append(f"{region}: {driver.KEY_NAME} has not been imported")
        else:
            raise

    security_groups = client.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [driver.security_group_name(region)]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )["SecurityGroups"]
    if not security_groups:
        errors.append(f"{region}: controller-only SSH security group has not been prepared")
    else:
        ssh_entries = {
            (
                permission.get("IpProtocol"),
                permission.get("FromPort"),
                permission.get("ToPort"),
                item["CidrIp"],
            )
            for permission in security_groups[0].get("IpPermissions", [])
            for item in permission.get("IpRanges", [])
            if "CidrIp" in item
        }
        ipv6_entries = [
            item
            for permission in security_groups[0].get("IpPermissions", [])
            for item in permission.get("Ipv6Ranges", [])
        ]
        if ssh_entries != {("tcp", 22, 22, cidr)} or ipv6_entries:
            errors.append(
                f"{region}: security-group ingress must be only TCP/22 from {cidr}"
            )

    if not client.describe_availability_zones(
        Filters=[{"Name": "state", "Values": ["available"]}]
    )["AvailabilityZones"]:
        errors.append(f"{region}: no available zones")
    if not client.describe_images(
        Owners=["099720109477"],
        Filters=[
            {"Name": "name", "Values": [driver.AMI_NAME + "*"]},
            {"Name": "architecture", "Values": ["x86_64"]},
            {"Name": "state", "Values": ["available"]},
        ],
    )["Images"]:
        errors.append(f"{region}: no supported Ubuntu Jammy AMI")
    available_types = {
        item["InstanceType"]
        for item in client.describe_instance_types(
            InstanceTypes=list(driver.instance_types)
        )["InstanceTypes"]
    }
    if not available_types:
        errors.append(f"{region}: none of {driver.instance_types!r} is available")
    return errors


def aws_readiness_errors(
    regions: Sequence[str],
    *,
    environment: Mapping[str, str] = os.environ,
    sts_client: Any | None = None,
) -> list[str]:
    errors = _environment_errors(environment)
    if not regions:
        errors.append("AWS monthly regions must be an explicit non-empty list")
    if errors:
        return errors
    try:
        if sts_client is None:
            import boto3

            sts_client = boto3.client("sts", region_name="us-east-1")
        identity = sts_client.get_caller_identity()
        expected_account = environment["AWS_EXPECTED_ACCOUNT_ID"]
        if identity.get("Account") != expected_account:
            errors.append(
                f"AWS identity is in account {identity.get('Account')}, expected {expected_account}"
            )
        expected_prefix = _expected_assumed_role_arn(environment["AWS_ROLE_ARN"])
        if not str(identity.get("Arn", "")).startswith(expected_prefix):
            errors.append("AWS credentials did not resolve to the configured federated role")
    except Exception as error:
        return [f"AWS credential exchange or STS identity check failed: {error}"]
    if errors:
        return errors
    for region in regions:
        try:
            errors.extend(_region_errors(region, environment["SCAMPER_AWS_SSH_CIDR"]))
        except Exception as error:
            errors.append(f"{region}: AWS regional preflight failed: {error}")
    return errors


def prepare(regions: Sequence[str]) -> dict[str, Any]:
    if not regions:
        raise ValueError("at least one explicit AWS region is required")
    basic_errors = _environment_errors(os.environ)
    if basic_errors:
        raise RuntimeError("\n".join(basic_errors))
    prepared = []
    for region in regions:
        driver.ensure_key_pair(region)
        group_id = driver.create_default_security_group(
            region, driver.security_group_name(region)
        )
        prepared.append({"region": region, "security_group_id": group_id})
    errors = aws_readiness_errors(regions)
    if errors:
        raise RuntimeError("AWS preparation did not pass readiness:\n" + "\n".join(errors))
    return {"ready": True, "prepared": prepared}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "prepare"))
    parser.add_argument("--regions", required=True, help="comma-separated AWS regions")
    args = parser.parse_args(argv)
    regions = tuple(value.strip() for value in args.regions.split(",") if value.strip())
    if args.action == "prepare":
        report = prepare(regions)
    else:
        errors = aws_readiness_errors(regions)
        report = {"ready": not errors, "errors": errors}
    print(json.dumps(report, indent=2))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
