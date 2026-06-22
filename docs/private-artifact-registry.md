# Private Artifact Registry setup

The recommended design uses two separate service identities:

1. The private experiment repository's CI identity has Artifact Registry Writer
   on one repository. GitHub Actions should reach this identity through Workload
   Identity Federation rather than a downloaded service-account key.
2. Measurement VMs use a dedicated service account with Artifact Registry Reader
   on that same repository.

This keeps image publishing separate from image execution and gives VMs no write
permission to the registry.

## Create the VM identity

Substitute your own project, region, and repository names:

```bash
PROJECT_ID=YOUR_GCP_PROJECT
REGION=us-central1
REPOSITORY=experiments
VM_SERVICE_ACCOUNT=measurement-vm

gcloud iam service-accounts create "$VM_SERVICE_ACCOUNT" \
  --project "$PROJECT_ID" \
  --display-name "Measurement VM image reader"

gcloud artifacts repositories add-iam-policy-binding "$REPOSITORY" \
  --project "$PROJECT_ID" \
  --location "$REGION" \
  --member "serviceAccount:${VM_SERVICE_ACCOUNT}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role roles/artifactregistry.reader
```

The operator creating VMs also needs permission to attach this service account,
normally `iam.serviceAccounts.actAs` through Service Account User on the chosen
identity.

`scamperctl` attaches the identity with the read-only storage OAuth scope used
for Artifact Registry pulls. IAM and access scopes are both enforced, so the
service account still needs `roles/artifactregistry.reader` on the repository.

## How image pulls authenticate

When `--registry-auth artifact-registry` is selected, `scamperctl` runs the
following sequence on each VM:

1. Request a short-lived OAuth access token from the Compute Engine metadata
   service.
2. Log Docker into the image's `LOCATION-docker.pkg.dev` host using a temporary
   Docker configuration directory.
3. Pull the requested immutable tag or digest.
4. Remove the temporary Docker configuration.

The token is never written to the local run inventory, embedded in VM metadata,
or supplied as a command-line argument.

## Prefer immutable image digests

Tags are convenient for smoke tests. Repeatable measurements should deploy the
digest emitted by the private image build:

```text
us-central1-docker.pkg.dev/YOUR_GCP_PROJECT/experiments/scamper@sha256:...
```

Recording the digest with the measurement metadata makes it possible to identify
the exact experiment runtime later, even if a mutable tag is updated.
