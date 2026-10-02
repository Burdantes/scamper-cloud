# IPv6 targets by announced prefix

This generator combines the current RouteViews `route-views6` BGP RIB with the
public TUM IPv6 Hitlist. Every responsive address is assigned to its
longest-matching announced IPv6 prefix, and the lowest seeded SHA-256 score
selects one deterministic target for each represented prefix.

For production prefix coverage, `--include-unresponsive-prefixes` retains those
TUM targets and generates a deterministic address for every other prefix that
has address space outside its announced more-specifics. This ensures the target
actually selects the intended route under BGP longest-prefix matching. A
covering prefix whose entire address space is announced by more-specifics cannot
be selected by any destination address; those prefixes are reported separately
instead of being counted as measured.

Generate a current target set with complete provenance:

```bash
python -m target_generation.ipv6_bgp.generate \
  --download-latest-rib \
  --download-responsive \
  --include-unresponsive-prefixes \
  --output datasets/ipv6-all-route-selectable-prefixes-YYYYMMDD.txt
```

The command also writes:

- `OUTPUT.metadata.json` with input digests, source timestamps, responsive and
  synthetic target counts, route-selectable coverage, and output digest;
- `OUTPUT.prefixes.tsv` mapping each target to its longest-matching BGP prefix
  and recording whether it came from TUM or deterministic synthesis;
- `OUTPUT.unrepresented-prefixes.txt` listing announced prefixes with no TUM
  responsive address; and
- `OUTPUT.untargetable-prefixes.txt` listing covering routes that cannot be
  selected because more-specific announcements cover their entire address
  space.

Omit `--include-unresponsive-prefixes` to produce the responsive-only subset.

Downloaded RIBs are cached under a collector-specific directory so collectors
that publish identical dump filenames cannot be confused.

`pytricia` and `bgpdump` are required. On macOS:

```bash
python -m pip install pytricia
brew install bgpdump
```
