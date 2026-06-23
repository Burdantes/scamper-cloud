# Regional fan-out and cost controls

## What the orchestrator does

`scamperctl` separates infrastructure lifetime from experiment lifetime:

```text
configure -> provision -> deploy -> monitor/status -> collect -> destroy
               |                                      |
               +-- local inventory under .scamper/ ---+
```

1. `configure` stores a local name for a specific gcloud configuration and
   project. It does not create cloud resources.
2. `provision` discovers or accepts zones, validates the VM and cost ceilings,
   writes a startup script, and produces a JSON plan. Only `--apply` creates
   VMs. Each successful creation is immediately recorded in the run inventory,
   so a partially completed fan-out can still be destroyed.
3. `deploy` copies the private target file to each VM, authenticates to the
   selected container registry, pulls the image, and starts one detached
   container per VM. The result directory remains on the VM.
4. `status`, `cost`, and `monitor` inspect the run. `cost` and `monitor` are
   local runtime estimates; they do not query delayed Cloud Billing records.
5. `collect` downloads the result directory from every VM.
6. `destroy` deletes every VM recorded in the run inventory.

The default for provisioning, deployment, and destruction is a dry run. The
state-changing form always requires `--apply`, except that `monitor` uses the
separate and explicit `--auto-destroy` switch for early cleanup.

## One VM per region

`--zones all` means every active zone and can create multiple VMs in one region.
`--zones one-per-region` is the geographically broad but smaller alternative:

1. list active GCP zones;
2. list zones that offer the selected machine type;
3. intersect those lists;
4. group zones by region;
5. select the lexicographically first available zone in every region.

The selected zones are printed in the dry-run plan. Availability and quotas can
still change between planning and creation, so use a unique run ID and retain
the inventory until cleanup is verified.

## The three cost brakes

### 1. VM-count ceiling

`--max-vms` rejects a plan whose VM count is larger than the reviewed ceiling.
For regional fan-out, set this above the discovered region count only after
reading the dry-run plan.

### 2. Conservative preflight estimate

The following flags form one indivisible cost guard:

- `--estimated-vm-hourly-usd`: a conservative per-VM hourly rate, including
  the expected external IPv4 charge;
- `--estimated-disk-gb-monthly-usd`: a conservative boot-disk GB-month rate;
- `--max-runtime-hours`: the maximum lifetime of every VM;
- `--max-estimated-cost-usd`: the maximum accepted plan estimate.

The estimate is:

```text
VM count * hourly VM rate * maximum hours
+ VM count * disk GB * monthly disk rate * maximum hours / 730
```

The plan is rejected if this exceeds the configured maximum. This estimate does
not include network egress, taxes, discounts, container-registry storage,
logging, monitoring, BigQuery, or other services. Supply rates with enough
headroom for regional pricing differences.

### 3. Server-side deletion deadline

Cost-guarded VMs are created with `--max-run-duration` and
`--instance-termination-action=DELETE`. Compute Engine therefore deletes each VM
at the runtime ceiling even if the local monitor stops or the laptop sleeps.

The optional local monitor prints elapsed runtime, estimated compute and disk
cost, remaining estimated budget, and progress fractions. With
`--auto-destroy`, it deletes the run early when either runtime or estimated cost
reaches the requested percentage (90% by default).

## Estimated versus billed spend

The local estimate is immediate and intentionally conservative. Cloud Billing
reports are authoritative but usage reporting is delayed. Google Cloud budgets
send alerts and do not impose a hard spending cap by themselves. For defense in
depth, create a project-scoped Cloud Billing budget below the real funding limit
and use this orchestrator's server-side VM lifetime as the immediate hard stop.

## Example workflow

```bash
# 1. Dry run: discover regions and inspect the full cost ceiling.
scamperctl provision \
  --profile lab \
  --run global-validation \
  --zones one-per-region \
  --machine-type e2-small \
  --disk-size-gb 10 \
  --count-per-zone 1 \
  --max-vms 50 \
  --estimated-vm-hourly-usd 0.05 \
  --estimated-disk-gb-monthly-usd 0.05 \
  --max-runtime-hours 2 \
  --max-estimated-cost-usd 6

# 2. Repeat the reviewed command with --apply.

# 3. Keep this running in another terminal for progress and early cleanup.
scamperctl monitor \
  --run global-validation \
  --interval-seconds 60 \
  --auto-destroy \
  --auto-destroy-at-percent 90

# 4. Deploy, wait for completion, and collect results.

# 5. Delete immediately after collection; do not wait for the deadline.
scamperctl destroy --run global-validation --apply
```
