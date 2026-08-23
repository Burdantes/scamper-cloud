# Direct-provider compatibility code

The GCP driver has been promoted to `providers/gcp` and is no longer here. The
controller dispatches through `providers.driver_module()`, so no production path
imports from this directory.

The AWS and Azure drivers are unsupported historical implementations. They are
kept here so useful provider logic is not lost during cleanup, but their tests
are optional and no production workflow should call them.

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
