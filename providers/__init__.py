"""Supported cloud providers.

Production workflows must import drivers from this package. Anything still under
``legacy/`` is unsupported historical code and must not be called by the
controller, the ``scamperctl`` workflow, or ``tests/required``.

A provider is promoted into this package only once it satisfies the scamperctl
inventory, cost-guard, experiment, collection, and teardown contracts. Moving a
directory here without that work removes the warning label without earning it.
"""

from __future__ import annotations

# provider name -> importable driver module, used instead of hardcoding paths.
DRIVER_MODULES: dict[str, str] = {
    "gcp": "providers.gcp.driver",
}


def driver_module(provider: str) -> str:
    """Return the driver module for ``provider``, or raise if unsupported."""
    try:
        return DRIVER_MODULES[provider]
    except KeyError:
        supported = ", ".join(sorted(DRIVER_MODULES)) or "none"
        raise ValueError(
            f"provider {provider!r} has no supported driver (supported: {supported}). "
            "Legacy drivers under legacy/providers are not production paths."
        ) from None
