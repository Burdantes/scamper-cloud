# Persistent GCP campaign controller

The controller is a small, long-lived GCP VM in `us-central1-c`. It provisions
short-lived measurement workers, sends each worker its distinct traceroute and
RR inputs, waits for uploaded artifacts, verifies completion, and deletes the
workers. Campaigns run as named `systemd` services, so closing the laptop or
losing the local network does not stop them.

Commands are dry runs unless `--apply` is supplied.

```bash
python -m controller.manage provision
python -m controller.manage provision --apply
python -m controller.manage deploy --apply
```

Register each complete target population once from the laptop. Registration
normalizes the first TSV column to canonical IPv4, rejects duplicates, records
the source and normalized SHA-256 values and target count, and uploads it into
the controller's persistent content-addressed registry:

```bash
python -m controller.manage register-targets --apply \
  --trace-targets datasets/ipv4-bgp-one-per-24-20260801.txt \
  --rr-targets /Users/loqmansalamatian/Downloads/rr_responsive_targets_2026-05-11.tsv
```

Keep the two IDs printed by that command. Submit one US-region worker running
both measurements without uploading or re-normalizing either population:

```bash
python -m controller.manage submit --apply \
  --run-id gcp-uscentral1-20260801a \
  --regions us-central1 \
  --max-instances 1 \
  --trace-target-id sha256:TRACE_SOURCE_SHA256 \
  --rr-target-id sha256:RR_SOURCE_SHA256 \
  --trace-rate 1000 \
  --rr-rate 1000
```

Passing `--trace-targets` and `--rr-targets` directly remains available as a
compatibility shortcut; it registers those files before submitting. Target IDs
are the fast path for repeat campaigns.

Build the reusable worker image once using
[`images/gcp-worker/README.md`](../images/gcp-worker/README.md), then select it
for a submission:

```text
--worker-image-project nsf-2148275-66720 \
--worker-image-family scamper-worker-debian12
```

The same defaults can be set with `GCP_WORKER_IMAGE_PROJECT` and
`GCP_WORKER_IMAGE_FAMILY` on the laptop. The controller passes that selection
to the durable campaign service. Workers created from the image skip runtime
package installation and verify each transferred target file with the
registered normalized SHA-256 and count before independently shuffling it.

Monitor it from any machine with `gcloud` access:

```bash
python -m controller.manage status --apply --run-id gcp-uscentral1-20260801a
```

The GCP controller waits up to 48 hours for a full campaign by default. Override
that lease with `SCAMPER_GCP_SCAMPER_TIMEOUT_SECONDS` when a run needs a
different limit. Each worker uploads the traceroute `.warts`, metadata, and
shuffled target list before it starts RR; RR artifacts are checkpointed the
same way, and the final status file is uploaded only after the requested stages
finish. The controller validates each worker's expected objects and terminal
status independently, then deletes that worker immediately while the remaining
regions continue. A worker with a terminal failure is also deleted after its
recoverable artifacts are uploaded, without cancelling healthy workers.

The controller uses its attached service account via Application Default
Credentials; no JSON private key is copied to it. That service account must be
able to create/delete Compute Engine instances, act as the worker service
account, create/read/write the configured GCS bucket, and read zones/images.
Both controller and worker external IPs are explicitly `STANDARD` network tier.

The registry lives under
`/var/lib/scamper-controller/target-registry/sha256/<source-sha256>/`. Deploying
new controller code does not replace that state. Do not delete it until no
future run needs the corresponding IDs.
