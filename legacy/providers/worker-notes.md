# Historical VM setup and configuration

This document covers the one-time setup required to run the scamper pipeline on GCP, AWS, and Azure.

> All scripts must be run inside the `scamper-analysis` conda environment:
> ```bash
> conda activate scamper-analysis
> ```

---

## GCP Setup

GCP is used both as a compute provider and as the **central storage backend** for warts files and logs from all clouds.

### 1. Service account credentials

1. GCP Console → IAM & Admin → Service Accounts
2. Create a service account with roles: **Compute Admin**, **Storage Admin**
3. Create a JSON key → download it
4. Place it at `./credentials/<filename>.json`
5. Update `settings.WARTS_STORAGE_CREDENTIALS` to `"./credentials/<filename>.json"`
6. `VMs/upload.py` auto-detects the copied JSON key on each VM. You can also set `SCAMPER_GCS_CREDENTIALS` or `GOOGLE_APPLICATION_CREDENTIALS` explicitly on the VM if needed.

### 2. SSH key

```bash
ssh-keygen -t ed25519 -f ~/.ssh/nsf -C "scamper-gcp"
```

Add the contents of `~/.ssh/nsf.pub` to:
GCP Console → Compute Engine → Metadata → SSH Keys

The key path is already set in `settings.GCP_SCAMPER_SSH_KEY`.

### 3. Run

```bash
python gcp.py
python gcp.py --apply
```

### Results bucket and run layout

GCP, AWS, and Azure campaigns reuse one GCS bucket and isolate each campaign under a
run prefix. The default bucket is
`nsf-2148275-66720-scamper-measurements`; override it with
`SCAMPER_RESULTS_BUCKET` or `--bucket-name`. A run is stored as:

```text
gs://BUCKET/runs/RUN_ID/
  manifest.json
  logs/LOG_ARCHIVE.tar.gz
  nodes/REGION/NODE/
    NODE-IP.status.json
    NODE-IP.trace.warts
    NODE-IP.trace.jsonl
    NODE-IP.trace.metadata.json
    NODE-IP.trace.targets.txt
    NODE-IP.rr.warts              # GCP/AWS trace+RR campaigns
    NODE-IP.rr.jsonl
    NODE-IP.rr.metadata.json
    NODE-IP.rr.targets.txt
```

The manifest records target provenance and checksum, requested regions or
locations, measurement commands, expected objects, and failed/incomplete
nodes. GCP and AWS per-measurement metadata records the exact Scamper command
executed on that node. Azure currently stores its existing traceroute output
using the same hierarchy and the `.trace.warts` suffix. `--object-prefix` can
replace `runs/RUN_ID` when a different hierarchy is needed.

For a one-VM GCP validation of both traceroute and IPv4 Record Route, first
print the plan without creating resources:

```bash
python gcp.py \
  --prefix gcp-trace-rr-canary \
  --log-dir gcp-trace-rr-canary-logs \
  --target-source /Users/loqmansalamatian/Downloads/rr_responsive_targets_2026-05-11.tsv \
  --regions us-central1 \
  --max-instances 1 \
  --max-targets 100 \
  --measurements trace,rr \
  --trace-rate 100 \
  --rr-rate 10 \
  --rr-timeout 2
```

Review the displayed bucket, object prefix, regions, and exact trace/RR
commands, then add `--apply` to launch the canary. After validation, omit
`--max-targets` to use the complete target file. The controller independently
shuffles the normalized target list for each measurement on every VM.

---

## AWS Setup

### 1. IAM user and credentials

1. AWS Console → IAM → Users → your user → **Security credentials** → **Create access key**
2. Download the CSV containing `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`
3. Set up a named profile in `~/.aws/credentials`:

```ini
[nsf-scamper]
aws_access_key_id = AKIA...
aws_secret_access_key = ...
region = us-east-1
```

4. Add to `.env` in the project root so `settings.py` picks it up automatically:

```
AWS_PROFILE=nsf-scamper
```

To verify the right account is active:
```bash
python -c "import boto3; print(boto3.client('sts').get_caller_identity()['Account'])"
```

### 2. IAM permissions

Attach the following policy to your IAM user (AWS Console → IAM → Policies → Create policy → JSON tab):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeRegions",
        "ec2:DescribeAvailabilityZones",
        "ec2:DescribeImages",
        "ec2:DescribeInstanceTypes",
        "ec2:DescribeInstances",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeKeyPairs",
        "ec2:RunInstances",
        "ec2:TerminateInstances",
        "ec2:CreateSecurityGroup",
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:CreateTags",
        "ec2:ImportKeyPair"
      ],
      "Resource": "*"
    }
  ]
}
```

### 3. SSH key pair

AWS key pairs are region-scoped, but you only need one private key. Generate it locally and import the public key into all regions programmatically:

```bash
ssh-keygen -t rsa -b 4096 -f credentials/aws-scamper-key-pair.pem -N ""
chmod 400 credentials/aws-scamper-key-pair.pem
```

Then import into all regions (run once before the first pipeline run):

```python
import boto3

KEY_NAME = "aws-scamper-key-pair"
pub_key = open("credentials/aws-scamper-key-pair.pem.pub", "rb").read()

client = boto3.client("ec2", region_name="us-east-1")
for region in [r["RegionName"] for r in client.describe_regions()["Regions"]]:
    ec2 = boto3.client("ec2", region_name=region)
    try:
        ec2.import_key_pair(KeyName=KEY_NAME, PublicKeyMaterial=pub_key)
        print(f"Imported into {region}")
    except ec2.exceptions.ClientError:
        print(f"Already exists in {region}")
```

The key path `./credentials/aws-scamper-key-pair.pem` is already set in `settings.AWS_SCAMPER_SSH_KEY`.

### 4. Run

The script prints a JSON plan by default. Use `--apply` to create instances and
run the measurement:

```bash
python aws.py
python aws.py --apply
```

---

## Azure Setup

### 1. Authentication

Azure uses `DefaultAzureCredential` from `azure-identity`, which automatically picks up credentials from the environment. The recommended approach is a service principal:

1. Azure Portal → Azure Active Directory → App registrations → New registration
2. After creating, go to **Certificates & secrets** → New client secret → copy the value
3. Go to your subscription → **Access control (IAM)** → Add role assignment → assign **Contributor** to your app registration
4. Add the following to `.env` in the project root:

```
AZURE_TENANT_ID=...
AZURE_CLIENT_ID=...
AZURE_CLIENT_SECRET=...
AZURE_SUBSCRIPTION_ID=...
```

`AZURE_SUBSCRIPTION_ID` is also read directly in `azr.py` via `os.environ["AZURE_SUBSCRIPTION_ID"]`.

### 2. SSH key pair

```bash
ssh-keygen -t rsa -b 4096 -f credentials/azr-scamper-key-pair.pem -N ""
chmod 400 credentials/azr-scamper-key-pair.pem
```

`azr.py` reads the public key from `credentials/azr-scamper-key-pair.pem.pub`
when creating VMs. If you regenerate the key, keep the `.pem.pub` file next to
the private key.

The key path `./credentials/azr-scamper-key-pair.pem` is already set in `settings.AZR_SCAMPER_SSH_KEY`.

### 3. Run

```bash
python azr.py
python azr.py --apply
```

## Safer all-cloud rerun wrapper

For a full GCP/AWS/Azure rerun, prefer the wrapper from the repository root:

```bash
python -m pip install -e .

scamper-legacy preflight \
  --providers gcp,aws,azr \
  --run-id rerun-20260712

scamper-legacy run \
  --providers gcp,aws,azr \
  --run-id rerun-20260712

scamper-legacy run \
  --providers gcp,aws,azr \
  --run-id rerun-20260712 \
  --apply

scamper-legacy expenses \
  --run-id rerun-20260712
```

`preflight` reports missing local credentials, target files, SSH keys, and Azure
environment variables before any provider script is executed.

When `run --apply` is used, the wrapper writes `.scamper/legacy-expenses.json`.
The file tracks provider start/finish times, reported instance counts,
configurable hourly rates, and whether the estimated accrued total is above the
default `$200` budget. If the ledger is already over budget, the wrapper skips
launching the next provider unless `--allow-over-budget` is passed.

Each VM script sources `VMs/scamper-smoke.sh` and runs a one-target scamper
canary after installing `scamper`, before the full target file starts. GCP uses
`1.1.1.1`; AWS and Azure use `8.8.8.8`. The canary converts the warts output
with `sc_warts2text` and fails unless the text output looks like a traceroute
with at least two hops. Set `SCAMPER_SMOKE_TARGET`, `SCAMPER_SMOKE_MIN_HOPS`, or
`SCAMPER_SMOKE_TEST=0` on the VM command environment to tune or skip it.

---

## Configuration reference (`settings.py`)

| Setting | Purpose |
|---|---|
| `WARTS_STORAGE_CREDENTIALS` | Path to GCP service account JSON (used by all three clouds for storage) |
| `SCAMPER_IP_DST` | Target IP list file (e.g. `./datasets/ipv4-24`) |
| `SCAMPER_UPLOAD_SCRIPT` | Path to `VMs/upload.py` |
| `SCAMPER_SMOKE_SCRIPT` | Path to shared VM smoke-test helper |
| `SCAMPER_CAMPAIGN_RUNNER` | Path to the shared traceroute/RR node runner |
| `SCAMPER_RESULTS_BUCKET` | Stable GCS results bucket used by every cloud provider |
| `GCP_PROJECT` | GCP project ID |
| `GCP_SERVICE_ACCOUNT` | Compute service account email |
| `GCP_SCAMPER_SSH_KEY` | Path to GCP SSH private key |
| `GCP_SCAMPER_USER` | SSH username on GCP VMs |
| `AWS_SCAMPER_SSH_KEY` | Path to AWS SSH private key (`.pem`) |
| `AWS_SCAMPER_USER` | SSH username on AWS VMs (default: `ubuntu`) |
| `AWS_SCAMPER_VM_SCRIPT` | Path to `VMs/run-scamper-aws.sh` |
| `AZR_SCAMPER_SSH_KEY` | Path to Azure SSH private key (`.pem`) |
| `AZR_SCAMPER_USER` | SSH username on Azure VMs (default: `azureuser`) |
| `AZR_SCAMPER_VM_SCRIPT` | Path to `VMs/run-scamper-azr.sh` |
