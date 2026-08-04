# IPv4 Record Route scanning

Runs one ICMP echo probe per destination with Scamper's IPv4 Record Route option
(`-R`). Its responsive target population is distinct from traceroute and is
independently shuffled on every node. Completion requires decoded `v4rr`
request flags and destination cardinality equal to the RR target count.
