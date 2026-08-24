"""Shared Google credential resolution for warts upload.

Every provider uploads its artifacts to the same GCS bucket, so every provider
needs Google credentials regardless of which cloud it provisions.

The GCP driver already resolved these correctly - an explicit service-account
key if one is configured, otherwise Application Default Credentials - but the AWS
and Azure drivers hard-required the key file. On the controller, whose
WARTS_STORAGE_CREDENTIALS points at a path that was never created, that meant GCP
campaigns ran and the other two died with FileNotFoundError before provisioning
anything. Sharing one implementation is what stops the untested paths drifting
from the tested one.

Preferring ADC when no key file exists is also the safer default: a GCE
controller has an attached service account, so no long-lived key needs to sit on
disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from providers import settings

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

_credentials: Any = None


def google_credentials() -> Any:
    """Return credentials from the configured key file, else from ADC."""
    global _credentials
    if _credentials is not None:
        return _credentials

    configured = str(settings.WARTS_STORAGE_CREDENTIALS or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            from google.oauth2 import service_account

            _credentials = service_account.Credentials.from_service_account_file(
                path, scopes=SCOPES
            )
            return _credentials

    import google.auth

    _credentials, _project = google.auth.default(scopes=SCOPES)
    return _credentials


def storage_client(project: str | None = None) -> Any:
    """A GCS client authenticated the same way for every provider."""
    from google.cloud import storage

    return storage.Client(
        project=project or settings.GCP_PROJECT, credentials=google_credentials()
    )
