# Architecture and repository boundary

`scamper-cloud` owns everything required to obtain measurement VMs and execute
experiments on them. It does not own scientific interpretation of collected
results.

## Active layers

1. `scamperctl/` is the provider-neutral command-line control plane. It stores
   run inventory, provisions and destroys workers, deploys experiments, and
   collects artifacts.
2. `controller/` is the persistent GCP-hosted, multi-cloud scheduler. A laptop
   submits a GCP, AWS, or Azure campaign, then may disconnect; the controller
   owns worker lifecycle. Its
   persistent content-addressed target registry receives each immutable target
   population from the laptop once. Later runs refer to the source SHA-256 ID.
3. `experiments/scamper_v4_scanning/` defines IPv4 ICMP traceroute.
4. `experiments/rr_v4_scanning/` defines one-probe IPv4 Record Route ping.
5. `target_generation/ipv4_bgp/` creates the versioned one-address-per-routed
   `/24` traceroute population. RR receives its own independently versioned
   responsive population at submission time.

Targets, credentials, live inventories, and results stay local or in configured
cloud storage and are never committed.

## Campaign data flow

```text
laptop -- register once --> controller target registry
                              |
                              +-- run A --> worker A -- independent shuffle
                              +-- run B --> worker B -- independent shuffle
```

The controller transfers the registered canonical file to every worker. The
worker checks its normalized SHA-256, trusts the manifest's recorded count only
after that check, shuffles locally with a fresh random seed, and records both
the verification method and shuffle provenance in measurement metadata. Stable
OS and Python dependencies live in the custom GCP worker image; experiment
scripts and per-run configuration are still deployed by the controller so
ordinary experiment changes do not require rebuilding the image.

## Provider boundary

`providers/` contains the supported GCP, AWS, and Azure campaign drivers. Every
driver accepts the same controller contract: distinct traceroute and RR target
sets, region selection, measurement rates, probe/contact metadata, exclusions,
smoke testing, instance caps, immutable artifacts, and cleanup. Required CI
passes the exact controller-generated command through every provider parser.

`legacy/providers/` contains historical implementations only. Production code,
the controller, and required tests must not import them.

The AWS boundary uses workload federation rather than stored IAM access keys.
The GCP VM metadata identity is exchanged through STS for a one-hour role
session on demand. AWS regions are explicit and pass account, role, VPC, subnet,
key-pair, security-group, image, instance-type, and zone checks before monthly
dispatch. The controller's reserved public IP is the sole SSH ingress source;
workers are tagged and always terminated in the driver's cleanup path.

## Boundary with analysis

The sibling analysis repository consumes immutable run artifacts. It owns
decoding, AS/IXP/geolocation datasets, enrichment, and derived outputs under a
general `vm_analysis` package. Cloud code must not import analysis modules, and
analysis code must not provision or delete VMs.
