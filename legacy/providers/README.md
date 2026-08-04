# Direct-provider compatibility code

The GCP driver remains temporarily active because the persistent controller uses
it for sequential trace/RR campaigns. It must pass the required compatibility
tests.

The AWS and Azure drivers are unsupported historical implementations. They are
kept here so useful provider logic is not lost during cleanup, but their tests
are optional and no production workflow should call them. Promote a provider
out of this directory only after porting it to the `scamperctl` inventory,
cost-guard, experiment, collection, and teardown contracts.
