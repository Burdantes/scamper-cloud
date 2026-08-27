# scamper-cloud

`scamper-cloud` is a multi-cloud control plane and experiment-definition
repository for disposable measurement VMs. A persistent controller hosted on
GCP launches short-lived workers on **GCP, AWS, and Azure**, collects immutable
artifacts in a shared GCS bucket, and tears the workers down. It deliberately
separates measurement execution from scientific analysis:

```text
scamper-cloud                         sibling VM-analysis repository
  provision/controller/teardown        decode immutable artifacts
  scamper_v4_scanning                  map hops to AS/IXP/geolocation
  RR_v4_scanning                       analyze RR address sequences
  target generation and contracts      produce derived datasets
```

This repository contains generic experiment implementations and contracts, but
no committed target list, credential, project-specific inventory, or result.
Those inputs are supplied at submission time.

See [Architecture](docs/architecture.md) for the ownership boundary and
[Testing policy](docs/testing.md) for the required/optional split.

## Supported clouds

| Provider | Campaign workers | Controller authentication | Typical worker size |
|----------|------------------|---------------------------|---------------------|
| GCP | Supported | Controller VM service account | `e2-micro` |
| AWS | Supported | Google workload identity exchanged for a short-lived AWS role session | `t3.micro`, with `t2.micro` fallback |
| Azure | Supported | Azure service principal configured on the controller | `Standard_B2ts_v2` |

The controller VM is hosted on GCP, but the measurement workers are not limited
to GCP. All three providers implement the same campaign contract: distinct
IPv4 traceroute, IPv6 traceroute, and IPv4 Record Route target sets,
provider-native regions and VM sizes, instance and target caps, smoke tests,
artifact verification, and cleanup.

Results from every provider currently go to the same configured GCS bucket.
AWS and Azure are worker platforms, not alternate artifact-storage backends.

IPv6 workers are dual-stack and remain controller-reachable over IPv4. `trace6`
uses native provider IPv6, not a tunnel: GCP creates a managed external
dual-stack VPC/subnet and uses Premium network tier for external IPv6; AWS adds
IPv6 CIDRs and a public `::/0` route to the selected default VPC subnet and
assigns IPv6 only to the measurement ENI; Azure creates a dual-stack VNet, NIC,
and public IP inside the run's disposable resource group. GCP's IPv4 traffic
remains on the configured tier, so record the IPv6 Premium-tier difference when
comparing provider paths. The GCP network/subnets and AWS VPC/subnet IPv6
associations persist for reuse; Azure networking is deleted with the run.
The GCP managed VPC's SSH rule is limited to the controller's public IPv4 `/32`,
read from VM metadata or overridden with `SCAMPER_GCP_SSH_CIDR`.

## Supported experiments

- [`experiments/scamper_v4_scanning`](experiments/scamper_v4_scanning): ICMP
  traceroute to one address per BGP-announced `/24`-equivalent.
- [`experiments/RR_v4_scanning`](experiments/RR_v4_scanning): one ICMP Record
  Route probe to each independently versioned RR-responsive target.
- [`experiments/scamper_v6_scanning`](experiments/scamper_v6_scanning): native
  IPv6 ICMP traceroute to a separately versioned responsive Hitlist population.

The persistent US controller under [`controller`](controller) owns campaigns
after submission, so the submitting laptop may disconnect. Generate the
traceroute population once with
[`target_generation/ipv4_bgp`](target_generation/ipv4_bgp).
Import the public responsive, non-aliased TUM IPv6 Hitlist with
[`target_generation/ipv6_hitlist`](target_generation/ipv6_hitlist).

## Requirements

- Python 3.10 or newer
- [Google Cloud CLI](https://cloud.google.com/sdk/docs/install), authenticated
  to provision and manage the controller VM
- A GCP service account for the controller and workers, with the least-privilege
  Compute Engine and GCS permissions described in
  [`controller/README.md`](controller/README.md)
- For AWS campaigns, an AWS IAM role that trusts the controller's Google
  identity; no long-lived AWS access key is stored on the controller
- For Azure campaigns, an Azure service principal supplied through the
  controller's root-managed secrets environment file

Install the terminal command:

```bash
python -m pip install -e .
```

## Recommended: persistent monthly multi-cloud controller

The controller is the production path for durable and scheduled campaigns. Its
monthly timer dispatches one independent campaign for each of GCP, AWS, and
Azure on the first day of every month at 06:00 UTC, with up to 30 minutes of
randomized delay. `Persistent=true` causes a missed event to run after the
controller recovers.

### 1. Provision and deploy the controller

Commands are dry runs unless `--apply` is present:

```bash
python -m controller.manage provision
python -m controller.manage provision --apply
python -m controller.manage deploy --apply
```

Every deployment creates a versioned release on the controller VM, installs its
dependencies, and runs the complete required test suite there before activating
the release. A failed test leaves the previous release active. This ensures the
exact uploaded code is tested in the orchestrator's Debian/Python environment,
not only on a developer workstation.

### 2. Configure provider identities

- **GCP:** attach the controller service account; do not copy a JSON private key
  to the VM.
- **AWS:** create the controller role using
  [`controller/aws-role-trust.example.json`](controller/aws-role-trust.example.json)
  and [`controller/aws-controller-policy.json`](controller/aws-controller-policy.json),
  then set `AWS_ROLE_ARN`, `AWS_EXPECTED_ACCOUNT_ID`, `AWS_GCP_AUDIENCE`, and
  `SCAMPER_AWS_SSH_CIDR` in `/etc/scamper-controller-secrets.env`.
- **Azure:** set `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
  `AZURE_CLIENT_SECRET`, and `AZURE_SUBSCRIPTION_ID` in the same root-managed
  secrets file.

The detailed AWS federation and regional preparation procedure is in
[`controller/README.md`](controller/README.md#aws-controller-identity-and-regional-preparation).

### 3. Import and register immutable targets once

Start IPv6 with a deterministic canary population. Omit `--max-targets` only
after the canary is validated:

```bash
python -m target_generation.ipv6_hitlist.import_hitlist \
  --download-responsive \
  --max-targets 1000 \
  --output datasets/ipv6-hitlist-responsive-1000.txt
```

```bash
python -m controller.manage register-targets --apply \
  --trace-targets /path/to/ipv4-bgp-one-per-24.txt \
  --rr-targets /path/to/rr-responsive-targets.tsv \
  --trace6-targets datasets/ipv6-hitlist-responsive-1000.txt
```

Any subset can be registered independently when only one population changes;
for example, pass only `--trace6-targets` to version a refreshed IPv6 Hitlist
without re-registering the IPv4 inputs.

Save the three returned `sha256:` target IDs. The registry records address
family and rejects mixed-family input. Later campaigns reuse the registered
content without uploading or normalizing it again.

### 4. Run a capped one-provider canary

Use a distinct run ID and an explicit cost/scale cap before enabling the monthly
schedule. Change `--provider`, `--regions`, and `--worker-machine-type` together:

```bash
# AWS example. Use --provider gcp or --provider azure for the other clouds.
python -m controller.manage submit --apply \
  --provider aws \
  --run-id aws-canary-YYYYMMDDa \
  --regions us-east-1 \
  --worker-machine-type t3.micro,t2.micro \
  --max-instances 1 \
  --max-targets 10 \
  --max-trace6-targets 10 \
  --trace-target-id sha256:TRACE_SOURCE_SHA256 \
  --rr-target-id sha256:RR_SOURCE_SHA256 \
  --trace6-target-id sha256:TRACE6_SOURCE_SHA256 \
  --measurements trace,trace6,rr
```

Monitor an individual run from any workstation with access to the controller:

```bash
python -m controller.manage status --apply --run-id aws-canary-YYYYMMDDa
```

Verify the terminal manifest, expected artifacts, and worker deletion before
expanding the cap or enabling unattended runs.

### 5. Configure and enable the monthly schedule

Start from [`controller/monthly-config.example.json`](controller/monthly-config.example.json).
The controller-owned `/etc/scamper-controller-monthly.json` must contain the
registered target IDs, result bucket, measurement policy, and entries for
exactly `gcp`, `aws`, and `azure`. Every provider needs a positive
`max_instances`; use `max_targets` for IPv4 and `max_trace6_targets` for an
independently bounded IPv6 rollout. Set `campaign_timeout_seconds` for every
provider to create a hard remote-campaign deadline. Monthly readiness estimates
the capped workload from the configured packet rates, a maximum of 40 probes
per traceroute destination, one probe per RR destination, a 25% safety margin,
and 15 minutes of fixed setup/upload time. Dispatch fails closed when that
estimate cannot fit inside the provider deadline.

The dispatcher validates all three providers before submitting any of them. It
refuses to run when targets, worker assets, credentials, exclusions, regions,
or provider entries are incomplete.

```bash
python -m controller.manage schedule-status --apply
python -m controller.manage schedule-enable --apply
```

Monthly run IDs have the form `monthly-PROVIDER-YYYYMM`. Existing job records
are skipped, and a controller lock prevents overlapping dispatches. Disable the
timer without deleting configuration or prior job records with:

```bash
python -m controller.manage schedule-disable --apply
```

See [`controller/README.md`](controller/README.md) for controller state paths,
target-registry behavior, timeout handling, and operational recovery details.

## One-off multi-cloud submissions

The controller also supports unscheduled campaigns on any one provider. The
provider names accepted by `--provider` are `gcp`, `aws`, and `azure`:

```bash
# GCP
python -m controller.manage submit --apply \
  --provider gcp --run-id gcp-uscentral1-YYYYMMDDa \
  --regions us-central1 --worker-machine-type e2-micro \
  --max-instances 1 \
  --trace-target-id sha256:TRACE_SOURCE_SHA256 \
  --rr-target-id sha256:RR_SOURCE_SHA256

# AWS
python -m controller.manage submit --apply \
  --provider aws --run-id aws-useast1-YYYYMMDDa \
  --regions us-east-1 --worker-machine-type t3.micro,t2.micro \
  --max-instances 1 \
  --trace-target-id sha256:TRACE_SOURCE_SHA256 \
  --rr-target-id sha256:RR_SOURCE_SHA256

# Azure
python -m controller.manage submit --apply \
  --provider azure --run-id azure-eastus-YYYYMMDDa \
  --regions eastus --worker-machine-type Standard_B2ts_v2 \
  --max-instances 1 \
  --trace-target-id sha256:TRACE_SOURCE_SHA256 \
  --rr-target-id sha256:RR_SOURCE_SHA256
```

## Lower-level GCP workflow without the controller

The `scamperctl` commands below are a workstation-driven GCP workflow. They are
useful for interactive provisioning and collection, but they do not provide the
durable monthly multi-cloud scheduling described above.

### 1. Configure an account and project

See the named configurations already available through `gcloud`:

```bash
scamperctl accounts
```

Save a local profile. The project and configuration are examples; local profile
state is written under `.scamper/` and ignored by Git.

```bash
scamperctl configure \
  --profile lab \
  --configuration default \
  --project YOUR_GCP_PROJECT
```

Every generated `gcloud` command explicitly pins both the configuration and
project, so changing the global `gcloud` default does not redirect an existing
run.

### 2. Provision VMs

The default behavior is a dry run. Review the JSON plan before adding `--apply`:

```bash
scamperctl provision \
  --profile lab \
  --run validation-run \
  --zones us-central1-a \
  --machine-type e2-small \
  --disk-size-gb 10 \
  --count-per-zone 1 \
  --max-vms 1

# Create the reviewed resources.
scamperctl provision \
  --profile lab \
  --run validation-run \
  --zones us-central1-a \
  --machine-type e2-small \
  --disk-size-gb 10 \
  --count-per-zone 1 \
  --max-vms 1 \
  --service-account measurement-vm@YOUR_GCP_PROJECT.iam.gserviceaccount.com \
  --apply
```

Provisioning installs Docker but does not copy experiment code or credentials.
`--max-vms` is a cost-safety ceiling, including when `--zones all` is used.
When a service account is attached, the VM receives only the read-only storage
OAuth scope needed to pull Artifact Registry images; it does not receive a broad
`cloud-platform` token.

### One VM per GCP region

Use `--zones one-per-region` to discover active zones where the selected machine
type is available and choose one deterministic zone from each region. Regional
fan-out requires an explicit cost guard when `--apply` is used:

```bash
scamperctl provision \
  --profile lab \
  --run global-validation \
  --zones one-per-region \
  --machine-type e2-small \
  --disk-size-gb 10 \
  --max-vms 50 \
  --estimated-vm-hourly-usd 0.05 \
  --estimated-disk-gb-monthly-usd 0.05 \
  --max-runtime-hours 2 \
  --max-estimated-cost-usd 6
```

The rates above are illustrative conservative inputs, not a pricing quote. The
dry-run plan reports the discovered region and VM counts, selected zones, and
the estimated maximum. Review that JSON before repeating the command with
`--apply`.

Every cost-guarded VM is created with a server-side maximum run duration and a
`DELETE` termination action. This limits runtime even if the controlling laptop
disconnects. You can inspect the local elapsed-time estimate once or continuously:

```bash
scamperctl cost --run global-validation

scamperctl monitor \
  --run global-validation \
  --interval-seconds 60 \
  --auto-destroy \
  --auto-destroy-at-percent 90
```

The monitor is an immediate estimate based on the conservative rates supplied
at provisioning time. It excludes network egress, taxes, discounts, and other
services. Cloud Billing is authoritative but delayed, and Google Cloud budgets
alert rather than automatically cap spending. See
[Regional fan-out and cost controls](docs/regional-fanout-and-costs.md) for the
full safety model and command flow.

### Give a collaborator SSH access

Ask the collaborator for their OpenSSH public key (`*.pub`) only. Add these two
options to the provision command above to attach it to every VM in the run:

```bash
--ssh-user collaborator \
--ssh-public-key /path/to/collaborator-id-ed25519.pub
```

The dry-run plan reports the username and key fingerprint without printing the
key body. With `--apply`, the public key is added to each VM's instance metadata;
it is not added project-wide. The collaborator connects with their private key:

```bash
ssh -i ~/.ssh/id_ed25519 collaborator@VM_EXTERNAL_IP
```

The collaborator effectively controls the disposable VM and may receive sudo
access from the Compute Engine guest environment. Keep the VM service account
least-privileged and restrict TCP/22 ingress to trusted source addresses. If OS
Login is enabled for the project, metadata keys are ignored and the plan fails
with guidance instead of creating inaccessible VMs. See
[Collaborator SSH access](docs/collaborator-ssh.md) for details.

### 3. Deploy a private experiment image

Grant the VM service account Artifact Registry Reader access to the private
repository. Then provide the full image URI and a local target file:

```bash
scamperctl deploy \
  --run validation-run \
  --experiment icmp-validation \
  --image us-central1-docker.pkg.dev/YOUR_GCP_PROJECT/experiments/scamper:v1 \
  --registry-auth artifact-registry \
  --targets /path/to/private-targets.txt

# Pull the image and start the container on the provisioned VM.
scamperctl deploy \
  --run validation-run \
  --experiment icmp-validation \
  --image us-central1-docker.pkg.dev/YOUR_GCP_PROJECT/experiments/scamper:v1 \
  --registry-auth artifact-registry \
  --targets /path/to/private-targets.txt \
  --apply
```

For `*.pkg.dev` image hosts, the default `--registry-auth auto` selects Artifact
Registry authentication automatically. The VM obtains a short-lived access
token from its metadata service, uses a temporary Docker configuration to pull
the image, and deletes that configuration. No registry token is stored in this
repository or passed through the CLI.

The experiment container receives:

- the target file mounted read-only at `/experiment/targets.txt`;
- a persistent result directory mounted at `/results`;
- `PROBE_NAME`, `PROBE_IP`, `EXPERIMENT_NAME`, and `SCAMPER_ARGS` environment
  variables;
- `NET_RAW` and `NET_ADMIN`, without full privileged mode.

### 4. Collect and destroy

```bash
scamperctl status --run validation-run

scamperctl collect \
  --run validation-run \
  --experiment icmp-validation \
  --output outputs/measurements/gcp

# Plan first, then delete explicitly.
scamperctl destroy --run validation-run
scamperctl destroy --run validation-run --apply
```

Always collect required results before destroying the VMs.

## Security boundary

Safe to publish here:

- generic provisioning and teardown code;
- startup scripts that install Docker;
- generic experiment code and placeholder configuration examples;
- unit tests using documentation-only IP ranges.

Keep uncommitted or in configured private storage:

- cloud credentials, access tokens, and service-account keys;
- real project/account profiles and VM inventories from `.scamper/`;
- real targets and measurement results.

See [Private Artifact Registry setup](docs/private-artifact-registry.md) for the
recommended identity and IAM arrangement.
