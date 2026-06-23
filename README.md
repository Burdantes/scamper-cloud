# scamper-cloud

`scamper-cloud` is the public control plane for disposable, Docker-ready
measurement VMs on Google Cloud. It deliberately separates infrastructure from
experiment payloads:

```text
scamper-cloud (public)       private experiment repository
  provision VMs               build experiment image
  deploy an image       <---  publish to private Artifact Registry
  collect results              keep targets and experiment files private
  destroy VMs
```

This repository contains no container image, experiment implementation, target
list, cloud credential, project-specific configuration, or measurement result.
The CLI accepts those inputs at deployment time.

## Requirements

- Python 3.10 or newer
- [Google Cloud CLI](https://cloud.google.com/sdk/docs/install), authenticated
  with each account or named configuration you plan to use
- Permission to create and delete Compute Engine instances in the selected
  project

Install the terminal command:

```bash
python -m pip install -e .
```

## 1. Configure an account and project

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

## 2. Provision VMs

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

## 3. Deploy a private experiment image

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

## Collect and destroy

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
- placeholder configuration examples;
- unit tests using documentation-only IP ranges.

Keep elsewhere:

- cloud credentials, access tokens, and service-account keys;
- real project/account profiles and VM inventories from `.scamper/`;
- experiment Dockerfiles, source, targets, and measurement results.

See [Private Artifact Registry setup](docs/private-artifact-registry.md) for the
recommended identity and IAM arrangement.
