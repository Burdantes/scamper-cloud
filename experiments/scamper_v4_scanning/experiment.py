"""Contract for the IPv4 traceroute experiment."""
# Flag semantics and the historical per-provider variants:
# docs/probe-configurations.md

NAME = "scamper_v4_scanning"
SCAMPER_OPERATION = "trace -m 20 -g 8 -w 3 -q 2 -P ICMP{payload_option}"
TARGET_ROLE = "one deterministic address per BGP-announced /24-equivalent"
