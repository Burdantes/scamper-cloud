"""Contract for the IPv6 traceroute experiment."""

NAME = "scamper_v6_scanning"
SCAMPER_OPERATION = "trace -m 20 -g 8 -w 3 -q 2 -P ICMP{payload_option}"
TARGET_ROLE = "responsive, non-aliased IPv6 addresses from the TUM IPv6 Hitlist"
