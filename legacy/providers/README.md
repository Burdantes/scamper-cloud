# Direct-provider compatibility code

The GCP driver has been promoted to `providers/gcp` and is no longer here. The
controller dispatches through `providers.driver_module()`, so no production path
imports from this directory.

The AWS and Azure drivers remain unsupported historical implementations and no
production workflow should call them. Everything else has been promoted:

- `providers/aws/client.py` and `providers/azure/client.py` implement the
  `scamperctl.cloud.CloudClient` contract and are registered in
  `providers.CLIENT_FACTORIES`;
- the AWS and Azure worker scripts now live under `providers/*/worker/` and
  delegate to the shared runner, and `providers/settings.py` no longer names any
  path in this directory.

What remains here is campaign orchestration only - target sharding, artifact
upload, log packaging, resume - roughly 1,000 lines per provider. Until that is
ported, `providers.driver_module()` refuses to launch an AWS or Azure campaign,
which is why neither appears in `DRIVER_MODULES`.

Retired probe configurations are recorded in `docs/probe-configurations.md`
rather than only surviving in this directory, so promoting the rest of a driver
does not lose the measurement choices it encoded.

This was violated in practice: a 27-region AWS campaign ran on 2026-08-06 and a
44-region Azure campaign on 2026-08-13, neither through a supported path. The
Azure campaign produced unusable data - 43 of 44 nodes observed no topology at
all, because the VMs lacked the inbound ICMP allow rule that `create_nsg` in
this directory already creates. They also ran Standard_D2s_v5 where this driver
hardcodes Standard_B1s, and were stopped rather than deallocated, which billed
for eight idle days. Promoting these providers properly is what prevents a
repeat. Promote a provider
out of this directory only after porting it to the `scamperctl` inventory,
cost-guard, experiment, collection, and teardown contracts.
