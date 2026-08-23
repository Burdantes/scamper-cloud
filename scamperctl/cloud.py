"""Provider-neutral cloud client contract.

``scamperctl.workflow`` drives instances through this protocol rather than a
concrete client, so a provider can be added by satisfying it instead of by
editing the workflow. ``scamperctl.gcloud.GCloudClient`` satisfies it
structurally; no inheritance is required.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable


@runtime_checkable
class CloudClient(Protocol):
    """Operations the campaign workflow needs from a cloud provider."""

    # --- instance lifecycle -------------------------------------------------
    def create_instance(self, *args: Any, **kwargs: Any) -> Any: ...
    def delete_instance(self, *args: Any, **kwargs: Any) -> Any: ...
    def describe_instance(self, *args: Any, **kwargs: Any) -> Any: ...

    # --- command construction, kept separate so callers can log or dry-run --
    def create_instance_args(self, *args: Any, **kwargs: Any) -> Sequence[str]: ...
    def delete_instance_args(self, *args: Any, **kwargs: Any) -> Sequence[str]: ...

    # --- placement ---------------------------------------------------------
    def list_zones(self, *args: Any, **kwargs: Any) -> Any: ...
    def list_machine_type_zones(self, *args: Any, **kwargs: Any) -> Any: ...

    # --- file transfer and shell ------------------------------------------
    def scp_to(self, *args: Any, **kwargs: Any) -> Any: ...
    def scp_from(self, *args: Any, **kwargs: Any) -> Any: ...
    def scp_to_args(self, *args: Any, **kwargs: Any) -> Sequence[str]: ...
    def ssh_args(self, *args: Any, **kwargs: Any) -> Sequence[str]: ...


@runtime_checkable
class SupportsOSLogin(Protocol):
    """GCP-specific OS Login check.

    Deliberately outside :class:`CloudClient`: OS Login has no AWS or Azure
    equivalent, so requiring it would force other providers to carry a
    meaningless stub. Callers must feature-test with ``isinstance(client,
    SupportsOSLogin)`` rather than assume it.
    """

    def project_os_login_enabled(self, *args: Any, **kwargs: Any) -> bool: ...
