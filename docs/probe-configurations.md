# Scamper probe configurations

Every probe configuration this repository has used, what each flag does, and
which campaigns it produced. Two of these differ in ways that make their results
non-comparable, so the differences are recorded rather than quietly normalised.

## Current canonical definitions

Owned by `experiments/common/run_campaign.py` (`MEASUREMENT_COMMANDS`) and
mirrored in `experiments/scamper_v4_scanning/experiment.py` and
`experiments/RR_v4_scanning/experiment.py`. The worker scripts must not
duplicate these; they delegate to the runner so the definition lives in one
place.

| Measurement | Command |
|---|---|
| trace | `trace -m 20 -g 8 -w 3 -q 2 -P ICMP{payload_option}` |
| rr | `ping -P icmp-echo -R -c 1 -W {timeout}{payload_option}` |

Used by: the GCP 42-region campaign (`gcp-global42-20260803t2030z`), the AWS
27-region campaign (`aws-20260806-trace-global28`), and the Azure 44-region
campaign (`azure-global-20260813`) — every campaign whose artifacts carry
`rate_pps` and `normalized_target_sha256` metadata came from this runner.

## Historical Azure configuration (UDP-Paris)

```
trace -P UDP-Paris -f 6 -g 10 -q 1
```

Present only in `legacy/providers/azure/driver.py:301` and the pre-2026-08-23
`run-scamper-azr.sh`. **It appears nowhere else in the repository**, so it is
recorded here so the choices are not lost when that script delegates to the
shared runner.

Note this configuration probed scamper directly and produced **only a warts
file** — no metadata, no target hashing, no shuffle record — so runs made with it
cannot be validated or reproduced the way runner-produced campaigns can.

## What the flags do

Semantics are from scamper's documented behaviour. The *rationale* column is
reconstructed from what each flag achieves, not from recorded design notes, so
treat it as inference rather than the original intent.

| Flag | Meaning | Why it might have been chosen |
|---|---|---|
| `-P ICMP` | ICMP Echo probes | Widest router response rate; ICMP Time Exceeded is what reveals intermediate hops |
| `-P UDP-Paris` | UDP probes holding the flow identifier constant | **Deliberately avoids per-flow load-balancer artifacts.** Classic Paris-traceroute behaviour: varying flow IDs make one path look like several, inventing topology that does not exist. This is a methodologically stronger choice than plain ICMP on load-balanced paths |
| `-P icmp-echo -R` | ICMP Echo carrying the Record Route option | The RR measurement; `-R` is what makes reverse-path inference possible |
| `-m 20` | maximum TTL 20 | Bounds probe cost; paths beyond 20 hops are truncated |
| `-f 6` | **first hop TTL 6** — hops 1-5 are never probed | Skips the provider's own internal fabric: less noise, fewer probes, and fewer abuse reports from intermediate infrastructure. The cost is losing the near-VP topology entirely |
| `-g 8` / `-g 10` | gap limit: consecutive unresponsive hops before abandoning a trace | Higher tolerates longer silent stretches at the cost of probes spent on dead paths |
| `-w 3` | 3s per-probe timeout | Trades run duration against loss on slow paths |
| `-q 2` / `-q 1` | probes per hop | `-q 2` retries once, recovering hops lost to single-packet loss; `-q 1` is half the packets and half the confidence |
| `-p 10000` | probe rate, packets per second | Campaign throughput; campaigns since have set this via `rate_pps` metadata instead |
| `-c 1` | one probe per target (ping) | RR needs a single response per destination |
| `-W {timeout}` | inter-probe / response wait (ping) | Set from `--rr-timeout`, 2s in the campaigns run so far |

## Consequences for comparing results

- **`-f 6` versus first-hop 1** is the largest difference. A trace starting at
  TTL 6 cannot observe the first five hops, so near-VP topology, the provider's
  egress interfaces, and any direct-peer adjacency inside those hops are absent
  by construction. Peer counts from such a run are not comparable with a
  canonical run.
- **UDP-Paris versus ICMP** changes both which routers reply and how load
  balancers are handled. Paris-style probing gives a truer path where per-flow
  balancing exists; ICMP typically gets a higher raw response rate.
- **`-q 1` versus `-q 2`** means a single lost packet drops a hop entirely,
  which fragments adjacencies and depresses IP-link counts.

If a UDP-Paris comparison is ever wanted, the honest way is to run it as a named
experiment under `experiments/` so its provenance is recorded, rather than by
editing a worker script.
