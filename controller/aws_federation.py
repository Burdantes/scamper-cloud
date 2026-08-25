"""Exchange the controller VM's Google identity for short-lived AWS credentials."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/identity"
)
ROLE_ARN_RE = re.compile(r"^arn:aws:iam::(?P<account>\d{12}):role/(?P<role>[^\s]+)$")


def _decode_segment(segment: str) -> dict[str, Any]:
    padded = segment + "=" * (-len(segment) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Google metadata returned a malformed identity token") from error
    if not isinstance(value, dict):
        raise ValueError("Google identity token payload is not an object")
    return value


def token_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Google metadata returned a malformed identity token")
    return _decode_segment(parts[1])


def validate_claims(
    claims: Mapping[str, Any], audience: str, *, now: datetime | None = None
) -> None:
    current = now or datetime.now(timezone.utc)
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise ValueError("Google identity token has an unexpected issuer")
    if claims.get("aud") != audience:
        raise ValueError("Google identity token has an unexpected audience")
    if not str(claims.get("sub", "")).isdigit() or claims.get("azp") != claims.get("sub"):
        raise ValueError("Google identity token is missing its stable service-account subject")
    try:
        expires = int(claims["exp"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Google identity token has no valid expiration") from error
    if expires <= int(current.timestamp()) + 60:
        raise ValueError("Google identity token is expired or too close to expiration")


def metadata_identity_token(
    audience: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> tuple[str, dict[str, Any]]:
    query = urllib.parse.urlencode({"audience": audience, "format": "full"})
    request = urllib.request.Request(
        f"{METADATA_IDENTITY_URL}?{query}",
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with opener(request, timeout=10) as response:
            token = response.read().decode("utf-8").strip()
    except Exception as error:
        raise RuntimeError("could not obtain the controller's Google identity token") from error
    claims = token_claims(token)
    validate_claims(claims, audience)
    return token, claims


def _required_environment(environment: Mapping[str, str]) -> tuple[str, str]:
    role_arn = environment.get("AWS_ROLE_ARN", "").strip()
    audience = environment.get("AWS_GCP_AUDIENCE", "").strip()
    if not ROLE_ARN_RE.fullmatch(role_arn):
        raise ValueError("AWS_ROLE_ARN must be an IAM role ARN")
    if not audience:
        raise ValueError("AWS_GCP_AUDIENCE must be configured")
    return role_arn, audience


def credential_process_payload(
    *,
    environment: Mapping[str, str] = os.environ,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sts_client: Any | None = None,
) -> dict[str, str | int]:
    """Return the exact JSON payload consumed by botocore credential_process."""
    role_arn, audience = _required_environment(environment)
    token, claims = metadata_identity_token(audience, opener=opener)
    if sts_client is None:
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config

        sts_client = boto3.client(
            "sts", region_name="us-east-1", config=Config(signature_version=UNSIGNED)
        )
    session_name = f"scamper-controller-{claims['sub']}"[:64]
    response = sts_client.assume_role_with_web_identity(
        RoleArn=role_arn,
        RoleSessionName=session_name,
        WebIdentityToken=token,
        DurationSeconds=3600,
    )
    credentials = response["Credentials"]
    expiration = credentials["Expiration"]
    if isinstance(expiration, datetime):
        expiration_text = expiration.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    else:
        expiration_text = str(expiration)
    return {
        "Version": 1,
        "AccessKeyId": credentials["AccessKeyId"],
        "SecretAccessKey": credentials["SecretAccessKey"],
        "SessionToken": credentials["SessionToken"],
        "Expiration": expiration_text,
    }


def safe_claims(*, environment: Mapping[str, str] = os.environ) -> dict[str, Any]:
    _role_arn, audience = _required_environment(environment)
    _token, claims = metadata_identity_token(audience)
    google = claims.get("google", {})
    return {
        key: claims.get(key) for key in ("iss", "aud", "azp", "sub", "email", "exp")
    } | {"google": google}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("credentials", "claims"), nargs="?", default="credentials")
    args = parser.parse_args(argv)
    try:
        payload = credential_process_payload() if args.action == "credentials" else safe_claims()
        print(json.dumps(payload, separators=(",", ":") if args.action == "credentials" else None))
        return 0
    except Exception as error:
        # Credential material and the Google token are intentionally never logged.
        print(f"AWS federation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
