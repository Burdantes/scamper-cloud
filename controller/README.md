# Persistent multi-cloud campaign controller

The controller is a small, long-lived GCP VM in `us-central1-c`. It provisions
short-lived GCP, AWS, and Azure measurement workers, sends each worker its
distinct IPv4 traceroute, IPv6 traceroute, and RR inputs, waits for uploaded artifacts, verifies
completion, and deletes the workers. Campaigns run as named `systemd` services,
so closing the laptop or losing the local network does not stop them.

Commands are dry runs unless `--apply` is supplied.

```bash
python -m controller.manage provision
python -m controller.manage provision --apply
python -m controller.manage deploy --apply
```

Deployment tests the staged release on the controller before activation. A
failed test keeps the previous `/opt/scamper-cloud/current` release in place.

Register each complete target population once from the laptop. Registration
normalizes the first TSV column to canonical IPv4 or IPv6, rejects duplicates
and mixed-family files, records
the source and normalized SHA-256 values and target count, and uploads it into
the controller's persistent content-addressed registry:

```bash
python -m controller.manage register-targets --apply \
  --trace-targets datasets/ipv4-bgp-one-per-24-20260801.txt \
  --rr-targets /path/to/rr-responsive-targets.tsv \
  --trace6-targets datasets/ipv6-hitlist-responsive-1000.txt
```

Keep the three IDs printed by that command. Submit one US-region worker running
all three measurements without uploading or re-normalizing any population:

```bash
python -m controller.manage submit --apply \
  --provider gcp \
  --run-id gcp-uscentral1-20260801a \
  --regions us-central1 \
  --max-instances 1 \
  --trace-target-id sha256:TRACE_SOURCE_SHA256 \
  --trace6-target-id sha256:TRACE6_SOURCE_SHA256 \
  --rr-target-id sha256:RR_SOURCE_SHA256 \
  --measurements trace,trace6,rr \
  --max-trace6-targets 10 \
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
Controller and worker external IPv4 addresses remain explicitly `STANDARD`
network tier. GCP external IPv6 is necessarily `PREMIUM`; the run manifest must
be used when interpreting this path-selection difference.
The managed dual-stack VPC allows SSH only from the controller public IPv4
`/32`, derived from VM metadata or explicitly set as `SCAMPER_GCP_SSH_CIDR`.

The registry lives under
`/var/lib/scamper-controller/target-registry/sha256/<source-sha256>/`. Deploying
new controller code does not replace that state. Do not delete it until no
future run needs the corresponding IDs.

## Monthly multi-cloud schedule

The installed `scamper-monthly.timer` fires on the first day of each month at
06:00 UTC, with a bounded randomized delay and `Persistent=true` so a missed
boot-time event runs after recovery. It remains disabled until the configuration
passes all readiness checks.

The operator-owned `/etc/scamper-controller-monthly.json` must contain:

- registered, immutable IPv4 traceroute, IPv6 traceroute, and RR target IDs;
- the stable result bucket and measurement/contact policy;
- entries for exactly GCP, AWS, and Azure;
- provider-native regions and worker sizes; and
- a positive `max_instances` safety cap for every provider.

Use `max_trace6_targets` independently of the IPv4 `max_targets` cap. The
checked-in schema-2 example starts at 1,000 IPv6 targets per worker. Each
provider also has a `campaign_timeout_seconds` lease. The controller passes it
through the durable systemd unit to the provider driver and refuses monthly
dispatch when the capped target counts and packet rates cannot fit within the
lease after the built-in workload safety margin. Azure uses the same deadline
to terminate lingering SSH processes before deleting the run's resource group.

Validate and enable it from a workstation:

```bash
python -m controller.manage schedule-status --apply
python -m controller.manage schedule-enable --apply
```

The dispatcher validates every provider before submitting any job. It refuses
to run with missing targets, worker assets, AWS/Azure credentials, exclusions,
or an incomplete provider set. Run IDs use `monthly-PROVIDER-YYYYMM`; existing
job records are skipped, and a controller lock prevents overlapping dispatches.

### AWS controller identity and regional preparation

AWS uses no access key on the controller. The AWS SDK invokes
`/usr/local/bin/scamper-controller-aws-credentials`, which requests a fresh
Google identity token from the VM metadata service and exchanges it for a
one-hour AWS role session. Configure these values in
`/etc/scamper-controller-secrets.env`:

```bash
AWS_ROLE_ARN=arn:aws:iam::ACCOUNT_ID:role/ScamperCloudController
AWS_EXPECTED_ACCOUNT_ID=ACCOUNT_ID
AWS_GCP_AUDIENCE=scamper-controller-aws
SCAMPER_AWS_SSH_CIDR=CONTROLLER_STATIC_IPV4/32
```

An AWS administrator creates the role from
`controller/aws-role-trust.example.json`. The checked-in `aud` and `sub` are
the controller service account's non-secret Google claims; regenerate them if
the controller identity changes by running:

```bash
sudo -u scamper-controller /usr/local/bin/scamper-controller-aws-credentials claims
```

Create `ScamperCloudController`, attach
`controller/aws-controller-policy.json` as an inline policy, and adjust the
`aws:RequestedRegion` list if the schedule uses more than `us-east-1`. The trust
relationship must retain all three `accounts.google.com` conditions; omitting
one lets a different Google workload or audience assume the role.

After the role exists, prepare each explicitly configured region and verify the
same checks the monthly timer uses:

```bash
sudo -u scamper-controller /usr/local/bin/scamper-controller-aws \
  prepare --regions us-east-1
sudo /usr/local/bin/scamper-controller-monthly check
```

Preparation imports only the controller public key, reconciles TCP/22 ingress
to the controller's exact `/32`, and refuses to create a default VPC. Readiness
also verifies the expected AWS account and assumed role, matching key
fingerprint, public default subnets, supported AMI and worker type, and available
zones. Keep the timer disabled until a one-instance, capped-target AWS canary
uploads a complete manifest and the worker has been terminated.

For `trace6`, the checked-in policy additionally permits associating an
Amazon-provided IPv6 range with the existing default VPC/subnet, installing the
public `::/0` route, and adding IPv6 egress to the worker security group. The
driver never enables automatic IPv6 assignment for unrelated instances; it
requests one IPv6 address only on each measurement ENI. These regional network
associations persist after workers terminate.

To register target files already present on the controller, run as root on that
VM:

```bash
/usr/local/bin/scamper-controller-monthly register-targets \
  --trace-targets /path/to/trace-targets.txt \
  --rr-targets /path/to/rr-targets.tsv \
  --trace6-targets /path/to/ipv6-hitlist-targets.txt
```
