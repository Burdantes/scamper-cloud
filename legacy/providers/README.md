# Retired provider code

This directory no longer holds any provider implementation. The AWS, Azure and
GCP campaign drivers, worker scripts and clients were all promoted to
`providers/`, and `providers/settings.py` names no path here.

No production module imports anything under `legacy/` any more. That is enforced
in spirit by `providers.driver_module()`, which refuses a provider without a
supported driver rather than resolving a quarantined path.

## What went wrong while these were quarantined

Both non-GCP campaigns ran outside the supported path, and the drivers kept here
would have prevented most of it:

- The Azure driver already set `Standard_B1s`, already created an inbound
  `AllowICMP` NSG rule, and already deleted its resource group in a `finally`
  block. The 2026-08-13 campaign used none of that: it ran `Standard_D2s_v5`,
  had no inbound ICMP rule - so 43 of 44 regions observed destinations and no
  intermediate hops at all - and left VMs merely stopped, which on Azure still
  bills compute. Roughly $150/day for eight days, and unusable data.
- The AWS worker script invoked `./run-scamper-campaign.py`, a filename that
  exists nowhere, so it could not run as committed and was patched by hand at
  deploy time.

The lesson is not that the quarantined code was bad. It is that shelving working
code as "unsupported" pushes people to run something unmaintained instead.

Retired probe configurations are recorded in `docs/probe-configurations.md`.
