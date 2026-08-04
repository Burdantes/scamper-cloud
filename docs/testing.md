# Testing policy

Tests are grouped by when they must run, not by historical implementation.

## Required on every change

- control-plane unit tests: plans, naming, limits, identity, cost and network
  safeguards;
- experiment contracts: exact Scamper command, separate target population,
  independent shuffle, metadata fields, and expected/decoded cardinality;
- local CLI smoke tests using documentation-only addresses and fake cloud
  commands.

These tests must be offline, deterministic, and fast enough for every commit.

## Required before an Internet-wide campaign

Run the documented live canary on one disposable VM with a small capped target
set. Verify both raw outputs, RR `v4rr` parsing, payload/contact metadata,
STANDARD network tier, uploads, cardinality, failure reporting, and worker
teardown. This is deliberately not a normal CI job because it creates billable
resources and sends packets.

## Optional regression suites

- legacy AWS/Azure provider regressions;
- live cloud integration and IAM diagnostics;
- expensive artifact-registry or storage integration tests.

Optional suites must use explicit pytest markers and cannot be prerequisites for
understanding whether the supported GCP workflow is healthy.
