"""Contract for the IPv4 Record Route experiment."""

NAME = "RR_v4_scanning"
SCAMPER_OPERATION = "ping -P icmp-echo -R -c 1 -W {timeout}{payload_option}"
TARGET_ROLE = "complete versioned RR-responsive destination subset"
PROBES_PER_DESTINATION = 1
RETRY_BEHAVIOR = "none configured"
