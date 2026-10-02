# Testing policy

Tests are grouped by when they must run, not by historical implementation.

## Required on every change

- control-plane unit tests: plans, naming, limits, identity, cost and network
  safeguards;
- experiment contracts: exact Scamper command, separate target population,
  independent shuffle, metadata fields, and expected/decoded cardinality;
- IPv4/IPv6 family rejection, Hitlist filtering and deterministic sampling,
  native dual-stack provider request contracts, and an IPv6 worker smoke probe;
- local CLI smoke tests using documentation-only addresses and fake cloud
  commands.

These tests must be offline, deterministic, and fast enough for every commit.

## Required on every controller deployment

`controller.bootstrap` creates a versioned release and virtual environment,
installs runtime and test dependencies, and runs `pytest tests/required` on the
controller VM before changing `/opt/scamper-cloud/current`. A failed test leaves
the previous release active. Each activated release records its source revision
and uploaded bundle SHA-256 in `release.json`.

This gate complements CI: it verifies the exact tarball using the controller's
Debian/Python environment and prevents code that was never tested on the
orchestrator from becoming active.

AWS federation tests mock both the GCP metadata endpoint and unsigned STS
exchange, then assert the exact JSON contract consumed by the AWS SDK's
`credential_process`. Regional preparation and monthly readiness remain mocked
in local tests; a release is activated only after the same required suite passes
inside the controller VM's Debian/Python environment. A live AWS canary is still
required after IAM trust or regional policy changes.

## Required before an Internet-wide campaign

Run the documented live canary on one disposable VM with a small capped target
set. Verify both raw outputs, RR `v4rr` parsing, payload/contact metadata,
the recorded IPv4 and IPv6 network tiers, native IPv6 source/address family,
uploads, cardinality, failure reporting, and worker
teardown. This is deliberately not a normal CI job because it creates billable
resources and sends packets.

## Optional regression suites

- historical implementations under `legacy/`;
- live cloud integration and IAM diagnostics;
- expensive artifact-registry or storage integration tests.

Optional suites must use explicit pytest markers and cannot be prerequisites for
understanding whether the supported GCP workflow is healthy.
