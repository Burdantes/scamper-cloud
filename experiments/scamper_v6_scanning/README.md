# IPv6 Scamper scanning

`trace6` runs ICMP traceroute over native provider IPv6 against a separately
registered IPv6 target population. The recommended source is the public,
responsive, non-aliased TUM IPv6 Hitlist imported with
`target_generation.ipv6_hitlist`.

The probe definition intentionally matches IPv4 `trace`. Address family comes
from the canonical destination file and is checked before launch and again at
the worker boundary. IPv4 Record Route has no IPv6 counterpart in this
campaign.
