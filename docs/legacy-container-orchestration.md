# Historical generic-container orchestration

`scamperctl` separates long-lived probe infrastructure from short-lived
measurement workloads:

1. `provision` creates Docker-ready GCP VMs and records them in a local run
   inventory.
2. `deploy` transfers an experiment's targets and starts a specified container
   image on every VM in that inventory.

The profile and run inventories are kept under `.scamper/`, which is ignored by
Git. They contain resource identifiers, not cloud credentials. Every `gcloud`
invocation explicitly supplies both the selected named configuration and the
project, so the command does not depend on whichever project happens to be the
global default later.

## Source repository versus a container registry

A Git repository and a container registry solve different parts of the
workflow:

| Location | What belongs there | What a VM must do |
|---|---|---|
| GitHub repository | Dockerfile, runner source, tests, and experiment definitions | Clone the source and build locally if no image registry is used |
| GitHub Container Registry (GHCR) | Prebuilt, versioned OCI/Docker images associated with the repository | Pull one immutable image |
| Docker Hub | Prebuilt, versioned images in a Docker-specific service | Pull one immutable image |

Using the Git repository alone is possible, but every VM has to clone the
repository and rebuild the image. Across many zones this repeats package
downloads, takes longer, increases failure points, and can produce slightly
different images at different times. A registry builds once and distributes
the identical image digest everywhere.

GHCR is the default for this project because:

- GitHub Actions can build an image from this repository using its built-in
  `GITHUB_TOKEN`.
- Source, build history, permissions, and image versions stay in one place.
- Public images can be pulled anonymously by the probe VMs.
- The image can be linked directly to the source repository with OCI labels.

Docker Hub remains a reasonable alternative when Docker-native discovery or an
existing Docker organization matters. It requires a separate account and
permission model, and unauthenticated or free personal pulls are rate-limited.
Docker Hub's own automated-build product is also being retired; GitHub Actions
can still build and push to Docker Hub normally.

The checked-in workflow publishes to:

```text
ghcr.io/burdantes/scamper-analysis
```

After its first successful run, make the package public in the GitHub package
settings before deploying it with the current CLI. Private GHCR pulling needs a
registry token on each VM; that secret-distribution path is intentionally not
part of the first version.

## Install and configure

Install the terminal command in an activated Python environment:

```bash
python -m pip install -e .
```

Inspect the accounts and projects already known to `gcloud`:

```bash
scamperctl accounts
```

Save the CloudBank-backed NSF profile without changing the active global
configuration:

```bash
scamperctl configure \
  --profile nsf \
  --configuration default \
  --project access-cis260552-540931
```

If the project requires Identity-Aware Proxy for SSH, add `--use-iap`.

## Step 1: provision VMs

List machine types in a candidate zone:

```bash
scamperctl machine-types --profile nsf --zone us-central1-a
```

Print a plan for two `e2-standard-2` probes:

```bash
scamperctl provision \
  --profile nsf \
  --run baseline-2026 \
  --zones us-central1-a,us-east1-b \
  --machine-type e2-standard-2
```

Review the project, zones, machine type, and VM count in the JSON plan. Repeat
the command with `--apply` to create the resources:

```bash
scamperctl provision \
  --profile nsf \
  --run baseline-2026 \
  --zones us-central1-a,us-east1-b \
  --machine-type e2-standard-2 \
  --apply
```

`--zones all` is supported, but the command refuses to exceed `--max-vms`
(default: 20). Increasing that limit is an explicit cost-safety decision.

Provisioning installs Docker using the VM startup script. If deployment begins
before startup finishes, wait briefly and retry; it is safe to redeploy the
same experiment name.

## Step 2: deploy an experiment

First print the deployment plan:

```bash
scamperctl deploy \
  --run baseline-2026 \
  --experiment icmp-baseline \
  --image ghcr.io/burdantes/scamper-analysis:latest \
  --targets datasets/ipv4-24
```

Repeat with `--apply` to transfer the target file, pull the image, and start one
container per probe:

```bash
scamperctl deploy \
  --run baseline-2026 \
  --experiment icmp-baseline \
  --image ghcr.io/burdantes/scamper-analysis:latest \
  --targets datasets/ipv4-24 \
  --apply
```

Use `--scamper-args` to change the measurement without rebuilding the image. To
change tools or add experiment files, update the Dockerfile, publish a new image
tag, and deploy that tag to the existing VM run.

The container receives only `NET_RAW` and `NET_ADMIN` capabilities, not full
privileged mode. Targets are mounted read-only. Results remain on each VM under
`/var/lib/scamperctl/<run>/<experiment>/results` even after the container exits
or is replaced. Each `.warts` filename embeds the VM's external IPv4 address so
it remains compatible with the repository's existing graph-processing pipeline.

Each completed measurement produces three files with the same timestamped stem:

- `.warts` contains the raw Scamper measurement.
- `.traces.jsonl` contains one decoded Scamper JSON object per line and is the
  input intended for traceroute analysis.
- `.metadata.json` records provenance, timing, commands, artifact paths, and the
  exit status of both Scamper and `sc_warts2json`.

## Inspect, collect, and destroy

```bash
scamperctl status --run baseline-2026

scamperctl collect \
  --run baseline-2026 \
  --experiment icmp-baseline \
  --output outputs/collected
```

The collected `.traces.jsonl` and `.metadata.json` files can then be staged for
BigQuery and loaded into RIPE Atlas-style tables:

```bash
scamperctl init-bigquery \
  --dataset my-gcp-project.scamper_measurements \
  --create-dataset

scamperctl load-bigquery \
  --run baseline-2026 \
  --experiment icmp-baseline \
  --dataset my-gcp-project.scamper_measurements \
  --dry-run

scamperctl load-bigquery \
  --run baseline-2026 \
  --experiment icmp-baseline \
  --dataset my-gcp-project.scamper_measurements \
  --create-dataset
```

The loader writes local newline-delimited JSON staging files under
`outputs/bigquery/<run>/<experiment>/`, creates five BigQuery tables
(`runs`, `probes`, `artifacts`, `trace_results`, and `trace_hops`), and merges
rows by stable IDs so a repeated load does not blindly append duplicates.
`trace_results` keeps nested hop records for Atlas-like trace queries, while
`trace_hops` is flattened for fast path, link, ASN, and IXP analysis. The trace
tables remain partitioned by `start_time`; cloud separation is modeled with the
`provider`, `project`, `region`, and `zone` columns and clustering starts with
`provider` so cloud-scoped queries stay efficient without fragmenting the data
into one table per provider.

By default, `scamperctl load-bigquery` uses the `bq` CLI for the warehouse
mutation so it follows the active Cloud SDK account, matching the identity used
for GCP VM operations. Use `--loader python` only when Python Application
Default Credentials are intentionally aligned with the target project.

```bash
# Plan first, then explicitly apply deletion.
scamperctl destroy --run baseline-2026
scamperctl destroy --run baseline-2026 --apply
```

Always collect required results before destroying the VMs.

## Build locally

The exact container can be built and validated locally without publishing it:

```bash
docker build -t scamper-analysis:local .
docker run --rm \
  --cap-add NET_RAW \
  --cap-add NET_ADMIN \
  --network host \
  -v "$PWD/datasets/ipv4-24:/experiment/targets.txt:ro" \
  -v "$PWD/outputs/local:/results" \
  scamper-analysis:local
```

The image contains the measurement runtime but no cloud credentials or target
dataset. This keeps one image reusable across accounts and experiments.
