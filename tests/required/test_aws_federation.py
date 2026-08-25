from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from controller import aws_federation, aws_setup


def _token(claims: dict[str, object]) -> str:
    encode = lambda value: base64.urlsafe_b64encode(
        json.dumps(value).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{encode({'alg': 'RS256'})}.{encode(claims)}.signature"


class Response:
    def __init__(self, value: str) -> None:
        self.value = value

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.value.encode("utf-8")


class FakeSTS:
    def __init__(self) -> None:
        self.request: dict[str, object] = {}

    def assume_role_with_web_identity(self, **kwargs: object) -> dict[str, object]:
        self.request = kwargs
        return {
            "Credentials": {
                "AccessKeyId": "temporary-access",
                "SecretAccessKey": "temporary-secret",
                "SessionToken": "temporary-session",
                "Expiration": datetime(2026, 9, 1, tzinfo=timezone.utc),
            }
        }


def test_credential_process_exchanges_fresh_metadata_token_and_matches_aws_contract() -> None:
    now = datetime.now(timezone.utc)
    token = _token(
        {
            "iss": "https://accounts.google.com",
            "aud": "scamper-controller-aws",
            "azp": "114833131536840371975",
            "sub": "114833131536840371975",
            "exp": int((now + timedelta(minutes=10)).timestamp()),
        }
    )
    sts = FakeSTS()
    observed_headers: dict[str, str] = {}

    def opener(request: object, *, timeout: int) -> Response:
        assert timeout == 10
        observed_headers.update(dict(request.header_items()))  # type: ignore[attr-defined]
        assert "audience=scamper-controller-aws" in request.full_url  # type: ignore[attr-defined]
        return Response(token)

    payload = aws_federation.credential_process_payload(
        environment={
            "AWS_ROLE_ARN": "arn:aws:iam::627275104670:role/ScamperCloudController",
            "AWS_GCP_AUDIENCE": "scamper-controller-aws",
        },
        opener=opener,
        sts_client=sts,
    )

    # These names and types are the external AWS credential_process contract.
    assert payload == {
        "Version": 1,
        "AccessKeyId": "temporary-access",
        "SecretAccessKey": "temporary-secret",
        "SessionToken": "temporary-session",
        "Expiration": "2026-09-01T00:00:00Z",
    }
    assert json.loads(json.dumps(payload))["Version"] == 1
    assert observed_headers["Metadata-flavor"] == "Google"
    assert sts.request["WebIdentityToken"] == token
    assert sts.request["DurationSeconds"] == 3600


def test_federation_rejects_wrong_audience_before_contacting_sts() -> None:
    token = _token(
        {
            "iss": "https://accounts.google.com",
            "aud": "wrong",
            "azp": "1",
            "sub": "1",
            "exp": int(datetime.now(timezone.utc).timestamp()) + 600,
        }
    )
    with pytest.raises(ValueError, match="unexpected audience"):
        aws_federation.credential_process_payload(
            environment={
                "AWS_ROLE_ARN": "arn:aws:iam::627275104670:role/ScamperCloudController",
                "AWS_GCP_AUDIENCE": "scamper-controller-aws",
            },
            opener=lambda *_args, **_kwargs: Response(token),
            sts_client=FakeSTS(),
        )


def test_aws_readiness_requires_federation_explicit_regions_and_controller_only_ssh(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(aws_setup.settings, "AWS_SCAMPER_SSH_KEY", str(tmp_path / "id_ed25519"))
    errors = aws_setup.aws_readiness_errors((), environment={})

    assert any("AWS credentials" in error for error in errors)
    assert any("explicit non-empty" in error for error in errors)
    assert any("public IPv4 /32" in error for error in errors)


def test_bootstrap_configures_credential_process_at_the_aws_consumer_boundary() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    bootstrap = (root / "controller/bootstrap.sh").read_text(encoding="utf-8")
    wrapper = (root / "controller/run-aws-credentials").read_text(encoding="utf-8")

    assert "credential_process = /usr/local/bin/scamper-controller-aws-credentials" in bootstrap
    assert "AWS_EC2_METADATA_DISABLED=true" in bootstrap
    assert '-m controller.aws_federation "${1:-credentials}"' in wrapper
    assert "run-aws-setup" in bootstrap
    assert "AWS_ACCESS_KEY_ID=" not in bootstrap
    assert "AWS_SECRET_ACCESS_KEY=" not in bootstrap
