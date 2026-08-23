"""Supported cloud providers.

Production workflows must import from this package. Anything still under
``legacy/`` is unsupported historical code and must not be called by the
controller, the ``scamperctl`` workflow, or ``tests/required``.

Two registries, deliberately separate, because a provider can be usable by the
scamperctl workflow before it has a full campaign driver:

``CLIENT_FACTORIES``
    Providers with a :class:`scamperctl.cloud.CloudClient` implementation. This
    is what makes a provider valid in a profile or run inventory.

``DRIVER_MODULES``
    Providers with a supported end-to-end campaign driver, invoked as
    ``python -m <module>`` by the controller.

A provider needs both to be launchable: a client for the scamperctl workflow and
a campaign driver for the controller. Registering in only one is the honest state
mid-port - :func:`driver_module` refuses any provider without an orchestrator
rather than silently resolving a legacy path.
"""

from __future__ import annotations

from typing import Any, Callable

# provider -> importable driver module, used instead of hardcoding paths.
DRIVER_MODULES: dict[str, str] = {
    "gcp": "providers.gcp.driver",
    "aws": "providers.aws.driver",
    "azure": "providers.azure.driver",
}


def _gcp_client(**kwargs: Any) -> Any:
    from scamperctl.gcloud import GCloudClient

    return GCloudClient(**kwargs)


def _aws_client(**kwargs: Any) -> Any:
    from providers.aws.client import AWSClient

    return AWSClient(**kwargs)


def _azure_client(**kwargs: Any) -> Any:
    from providers.azure.client import AzureClient

    return AzureClient(**kwargs)


# provider -> factory returning a CloudClient. Imported lazily so that a missing
# optional SDK for one provider cannot break the others.
CLIENT_FACTORIES: dict[str, Callable[..., Any]] = {
    "gcp": _gcp_client,
    "aws": _aws_client,
    "azure": _azure_client,
}


def supported_providers() -> tuple[str, ...]:
    """Providers valid in a profile or inventory, i.e. those with a client."""
    return tuple(sorted(CLIENT_FACTORIES))


def client_for(provider: str, **kwargs: Any) -> Any:
    """Build the :class:`CloudClient` for ``provider``."""
    try:
        factory = CLIENT_FACTORIES[provider]
    except KeyError:
        supported = ", ".join(supported_providers()) or "none"
        raise ValueError(
            f"provider {provider!r} has no supported client (supported: {supported}). "
            "Legacy drivers under legacy/providers are not production paths."
        ) from None
    return factory(**kwargs)


def driver_module(provider: str) -> str:
    """Return the campaign driver module for ``provider``, or raise."""
    try:
        return DRIVER_MODULES[provider]
    except KeyError:
        supported = ", ".join(sorted(DRIVER_MODULES)) or "none"
        raise ValueError(
            f"provider {provider!r} has no supported campaign driver "
            f"(supported: {supported}). Legacy drivers under legacy/providers are "
            "not production paths."
        ) from None
