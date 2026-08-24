"""Fail fast when a worker asset is missing, instead of retrying forever.

Every driver scps a fixed set of files onto each worker VM. If one is absent on
the launching host the scp fails permanently, but the drivers treat scp failure
as transient and retry - so a missing file becomes an infinite loop that
provisions a VM, never uses it, and reports nothing useful.

That happened three times bringing the controller up:

* ``WARTS_STORAGE_CREDENTIALS`` pointed at ``adc-only.json``, a path bootstrap
  writes into the environment but never creates;
* the SSH keys defaulted to ``./credentials/*.pem``, which exist only in a
  developer checkout;
* the AWS worker script referenced a campaign runner filename that exists
  nowhere in the repository.

Each was a one-line cause behind a silent hang. Checking before provisioning
turns all of them into a message naming the missing file.
"""

from __future__ import annotations

from pathlib import Path

from providers import settings

# provider -> setting names whose values must exist as files before launching.
WORKER_ASSETS: dict[str, tuple[str, ...]] = {
    "gcp": (
        "WARTS_STORAGE_CREDENTIALS",
        "GCP_SCAMPER_SCRIPT",
        "SCAMPER_SMOKE_SCRIPT",
        "SCAMPER_UPLOAD_SCRIPT",
        "SCAMPER_CAMPAIGN_RUNNER",
    ),
    "aws": (
        "WARTS_STORAGE_CREDENTIALS",
        "AWS_SCAMPER_VM_SCRIPT",
        "SCAMPER_SMOKE_SCRIPT",
        "SCAMPER_UPLOAD_SCRIPT",
        "SCAMPER_CAMPAIGN_RUNNER",
        "AWS_SCAMPER_SSH_KEY",
    ),
    "azr": (
        "WARTS_STORAGE_CREDENTIALS",
        "AZR_SCAMPER_VM_SCRIPT",
        "SCAMPER_SMOKE_SCRIPT",
        "SCAMPER_UPLOAD_SCRIPT",
        "SCAMPER_CAMPAIGN_RUNNER",
        "AZR_SCAMPER_SSH_KEY",
    ),
}
WORKER_ASSETS["azure"] = WORKER_ASSETS["azr"]


def missing_worker_assets(provider: str) -> list[tuple[str, str]]:
    """Return ``(setting_name, path)`` for each configured file that is absent."""
    try:
        names = WORKER_ASSETS[provider]
    except KeyError:
        raise ValueError(
            f"no worker asset list for provider {provider!r} "
            f"(known: {', '.join(sorted(WORKER_ASSETS))})"
        ) from None

    missing = []
    for name in names:
        value = str(getattr(settings, name, "") or "").strip()
        if not value or not Path(value).expanduser().is_file():
            missing.append((name, value or "<unset>"))
    return missing


def assert_worker_assets(provider: str) -> None:
    """Raise before provisioning if any worker asset is missing."""
    missing = missing_worker_assets(provider)
    if not missing:
        return
    detail = "\n".join(f"  {name} -> {path}" for name, path in missing)
    raise SystemExit(
        f"cannot launch a {provider} campaign: {len(missing)} worker file(s) the "
        f"driver must copy to each VM are missing on this host:\n{detail}\n"
        "Provisioning was not started. Fix the paths, or run from the controller "
        "where bootstrap.sh provides them."
    )
