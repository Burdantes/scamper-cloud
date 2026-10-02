# IPv6 Hitlist target import

The importer downloads or reads the public TUM IPv6 Hitlist responsive-address
dataset, validates IPv6 addresses, excludes non-global addresses, removes
duplicates, and writes a canonical one-address-per-line file plus provenance
metadata.

For an initial deterministic 1,000-destination canary:

```bash
python -m target_generation.ipv6_hitlist.import_hitlist \
  --download-responsive \
  --max-targets 1000 \
  --output datasets/ipv6-hitlist-responsive-1000.txt
```

Omit `--max-targets` to retain the complete responsive, non-aliased population.
When capped, selection uses the lowest seeded SHA-256 scores rather than the
first addresses in the source, avoiding source-order bias. The adjacent
`.metadata.json` records the source URL and digest, retrieval time, selection
policy, filtering counts, output digest, and target count.

The open list is published by the [TUM IPv6 Hitlist
service](https://ipv6hitlist.github.io/). Review its current terms and dataset
description before each campaign.
