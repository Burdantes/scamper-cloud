# IPv4 Scamper scanning

Runs ICMP traceroute against the independently shuffled traceroute population:
one deterministic address for every `/24` covered by the selected BGP RIB.
The run records its exact command, target source/version/hash/count, payload,
shuffle seed, raw warts output, decoded JSONL, and decoded cardinality.
