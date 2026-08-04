# GCP Scamper worker image

This image family removes per-VM `apt` and `pip` installation while keeping
experiment code outside the image. The controller still copies the small worker
and experiment scripts for each run, so ordinary code changes do not require an
image rebuild.

Build a candidate image:

```bash
packer init images/gcp-worker/packer.pkr.hcl
packer build \
  -var project_id=nsf-2148275-66720 \
  images/gcp-worker/packer.pkr.hcl
```

Validate the candidate with a capped campaign before selecting the family for a
full run. Submit with:

```text
--worker-image-project nsf-2148275-66720
--worker-image-family scamper-worker-debian12
```

The worker script detects `/opt/scamper-worker/venv/bin/python` and skips all
runtime package installation. Public Debian images remain supported through a
compatibility fallback; set `SCAMPER_ALLOW_RUNTIME_INSTALL=0` when validating a
prebuilt image to make missing dependencies fail immediately.

After the capped validation succeeds, keep the family as the laptop-side
default:

```bash
export GCP_WORKER_IMAGE_PROJECT=nsf-2148275-66720
export GCP_WORKER_IMAGE_FAMILY=scamper-worker-debian12
```

The image intentionally contains no target data and no experiment source. The
former remains versioned in the persistent controller registry; the latter is
deployed with the current controller release.
