# IPv4 traceroute target generation

This folder is intentionally independent of campaign execution. Run it once to
produce one deterministic target for every `/24` covered by an IPv4 prefix in a
current BGP RIB. A `/16`, for example, contributes 256 candidates. Prefixes more
specific than `/24` contribute one candidate in their containing `/24`.

Download the latest RIB reported by the official RouteViews metadata API and
generate the list:

```bash
python -m target_generation.ipv4_bgp.generate \
  --download-latest \
  --collector route-views2 \
  --output datasets/ipv4-bgp-one-per-24-20260801.txt
```

The generator requires `bgpdump` for an MRT RIB (`brew install bgpdump` on
macOS). It can also read an existing CAIDA `pfx2as` text or gzip file:

```bash
python -m target_generation.ipv4_bgp.generate \
  --rib path/to/routeviews.pfx2as.gz \
  --output datasets/ipv4-bgp-one-per-24.txt
```

The seed makes the choice reproducible. The adjacent metadata JSON records the
RIB URL and timestamp, source and output SHA-256 digests, selection policy, and
cardinality. Default routes and non-global address space are excluded.
