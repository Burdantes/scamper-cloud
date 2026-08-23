# Direct-provider compatibility code

The GCP driver has been promoted to `providers/gcp` and is no longer here. The
controller dispatches through `providers.driver_module()`, so no production path
imports from this directory.

The AWS and Azure drivers remain unsupported historical implementations and no
production workflow should call them. Their provider logic now has a supported
home: `providers/aws/client.py` and `providers/azure/client.py` implement the
`scamperctl.cloud.CloudClient` contract, and both are registered in
`providers.CLIENT_FACTORIES`. Neither has a campaign driver yet, so
`providers.driver_module()` still refuses to launch a campaign with them - that
gap is what remains of the port.

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
