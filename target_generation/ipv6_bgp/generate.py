from __future__ import annotations

import argparse
import gzip
import ipaddress
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from target_generation.ipv4_bgp.generate import download_latest_rib, sha256_file
from target_generation.ipv6_hitlist.import_hitlist import (
    DEFAULT_RESPONSIVE_URL,
    open_text_source,
    selection_score,
)

logger = logging.getLogger(__name__)


def parsed_prefix(value: str) -> ipaddress.IPv6Network | None:
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None
    return network if network.version == 6 else None


def prefixes_from_text(source: TextIO) -> Iterator[ipaddress.IPv6Network]:
    for raw_line in source:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        network = parsed_prefix(fields[0]) if "/" in fields[0] else None
        if network is None and len(fields) >= 2 and fields[1].isdigit():
            network = parsed_prefix(f"{fields[0]}/{fields[1]}")
        if network is not None:
            yield network


def prefixes_from_bgpdump(
    path: Path, executable: str = "bgpdump"
) -> Iterator[ipaddress.IPv6Network]:
    if shutil.which(executable) is None:
        raise RuntimeError(
            "bgpdump is required for MRT RIB input; install it with "
            "`brew install bgpdump`"
        )
    process = subprocess.Popen(
        [executable, "-m", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    for line in process.stdout:
        fields = line.rstrip("\n").split("|")
        if len(fields) > 5:
            network = parsed_prefix(fields[5])
            if network is not None:
                yield network
    stderr = process.stderr.read() if process.stderr is not None else ""
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(
            f"bgpdump failed with exit code {return_code}: {stderr.strip()}"
        )


def read_prefixes(path: Path) -> Iterator[ipaddress.IPv6Network]:
    if path.suffix == ".bz2":
        yield from prefixes_from_bgpdump(path)
        return
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as source:
        yield from prefixes_from_text(source)


def build_prefix_trie(
    prefixes: Iterable[ipaddress.IPv6Network],
    *,
    include_non_global: bool = False,
) -> tuple[Any, tuple[str, ...], dict[str, int]]:
    try:
        import pytricia
    except ImportError as error:
        raise RuntimeError(
            "pytricia is required for IPv6 longest-prefix matching; "
            "install it with `python -m pip install pytricia`"
        ) from error

    trie = pytricia.PyTricia(128)
    unique_prefixes: set[str] = set()
    input_rows = 0
    skipped_default_routes = 0
    skipped_non_global = 0
    duplicate_rows = 0
    for network in prefixes:
        input_rows += 1
        if network.prefixlen == 0:
            skipped_default_routes += 1
            continue
        if not include_non_global and not network.network_address.is_global:
            skipped_non_global += 1
            continue
        canonical = str(network)
        if canonical in unique_prefixes:
            duplicate_rows += 1
            continue
        unique_prefixes.add(canonical)
        trie[canonical] = canonical

    ordered_prefixes = tuple(
        sorted(
            unique_prefixes,
            key=lambda value: (
                int(ipaddress.ip_network(value).network_address),
                ipaddress.ip_network(value).prefixlen,
            ),
        )
    )
    if not ordered_prefixes:
        raise ValueError("BGP input contained no eligible announced IPv6 prefixes")
    return trie, ordered_prefixes, {
        "input_ipv6_prefix_rows": input_rows,
        "skipped_default_routes": skipped_default_routes,
        "skipped_non_global_prefix_rows": skipped_non_global,
        "duplicate_prefix_rows": duplicate_rows,
        "unique_announced_ipv6_prefixes": len(ordered_prefixes),
    }


def immediate_children_by_prefix(
    trie: Any,
    ordered_prefixes: Iterable[str],
) -> dict[str, tuple[ipaddress.IPv6Network, ...]]:
    """Return the closest announced more-specifics for each covering prefix."""
    children: defaultdict[str, list[ipaddress.IPv6Network]] = defaultdict(list)
    for prefix in ordered_prefixes:
        parent = trie.parent(prefix)
        if parent is not None:
            children[parent].append(ipaddress.IPv6Network(prefix))
    return {
        prefix: tuple(
            sorted(networks, key=lambda network: int(network.network_address))
        )
        for prefix, networks in children.items()
    }


def deterministic_uncovered_target(
    prefix: ipaddress.IPv6Network,
    children: Iterable[ipaddress.IPv6Network],
    *,
    seed: str,
) -> ipaddress.IPv6Address | None:
    """Select a stable address whose longest announced match is ``prefix``.

    Immediate child announcements partition the parts of the covering prefix
    that would route through a more-specific. The remaining gaps are the only
    addresses capable of exercising the covering route.
    """
    cursor = int(prefix.network_address)
    final_address = int(prefix.broadcast_address)
    gaps: list[tuple[int, int]] = []
    for child in children:
        child_start = int(child.network_address)
        child_end = int(child.broadcast_address)
        if cursor < child_start:
            gaps.append((cursor, child_start - 1))
        cursor = max(cursor, child_end + 1)
        if cursor > final_address:
            break
    if cursor <= final_address:
        gaps.append((cursor, final_address))
    if not gaps:
        return None

    uncovered_addresses = sum(end - start + 1 for start, end in gaps)
    selected_offset = selection_score(seed, f"synthetic:{prefix}") % uncovered_addresses
    for start, end in gaps:
        gap_size = end - start + 1
        if selected_offset < gap_size:
            return ipaddress.IPv6Address(start + selected_offset)
        selected_offset -= gap_size
    raise AssertionError("uncovered IPv6 target selection exhausted its address ranges")


def generate_targets(
    prefixes: Iterable[ipaddress.IPv6Network],
    responsive_source: Path,
    output_path: Path,
    *,
    seed: str,
    include_non_global: bool = False,
    include_unresponsive_prefixes: bool = False,
    mapping_path: Path | None = None,
    unrepresented_path: Path | None = None,
    untargetable_path: Path | None = None,
) -> dict[str, int | str | bool]:
    trie, ordered_prefixes, prefix_stats = build_prefix_trie(
        prefixes,
        include_non_global=include_non_global,
    )
    best_by_prefix: dict[str, tuple[int, str]] = {}
    input_rows = 0
    invalid_rows = 0
    non_ipv6_rows = 0
    non_global_rows = 0
    matching_rows = 0
    unmatched_rows = 0

    with open_text_source(responsive_source) as source:
        for raw_line in source:
            input_rows += 1
            if input_rows % 1_000_000 == 0:
                logger.info(
                    "processed %d responsive rows; %d prefixes represented",
                    input_rows,
                    len(best_by_prefix),
                )
            value = raw_line.strip().split("\t", 1)[0].strip()
            if not value or value.startswith("#"):
                continue
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                invalid_rows += 1
                continue
            if address.version != 6:
                non_ipv6_rows += 1
                continue
            if not include_non_global and not address.is_global:
                non_global_rows += 1
                continue
            canonical = str(address)
            prefix = trie.get_key(canonical)
            if prefix is None:
                unmatched_rows += 1
                continue
            matching_rows += 1
            score = selection_score(seed, canonical)
            current = best_by_prefix.get(prefix)
            if current is None or (score, canonical) < current:
                best_by_prefix[prefix] = (score, canonical)

    output_path = output_path.expanduser().resolve()
    mapping_path = (
        mapping_path.expanduser().resolve()
        if mapping_path is not None
        else Path(f"{output_path}.prefixes.tsv")
    )
    unrepresented_path = (
        unrepresented_path.expanduser().resolve()
        if unrepresented_path is not None
        else Path(f"{output_path}.unrepresented-prefixes.txt")
    )
    untargetable_path = (
        untargetable_path.expanduser().resolve()
        if untargetable_path is not None
        else Path(f"{output_path}.untargetable-prefixes.txt")
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    unrepresented_path.parent.mkdir(parents=True, exist_ok=True)
    untargetable_path.parent.mkdir(parents=True, exist_ok=True)

    represented = [prefix for prefix in ordered_prefixes if prefix in best_by_prefix]
    unrepresented = [prefix for prefix in ordered_prefixes if prefix not in best_by_prefix]
    children_by_prefix = immediate_children_by_prefix(trie, ordered_prefixes)
    synthetic_by_prefix: dict[str, str] = {}
    untargetable: list[str] = []
    for prefix in unrepresented:
        target = deterministic_uncovered_target(
            ipaddress.IPv6Network(prefix),
            children_by_prefix.get(prefix, ()),
            seed=seed,
        )
        if target is None:
            untargetable.append(prefix)
        else:
            synthetic_by_prefix[prefix] = str(target)

    selected_by_prefix = {
        prefix: (best_by_prefix[prefix][1], "tum-responsive")
        for prefix in represented
    }
    if include_unresponsive_prefixes:
        selected_by_prefix.update(
            {
                prefix: (target, "deterministic-uncovered")
                for prefix, target in synthetic_by_prefix.items()
            }
        )
    selected_targets = [
        selected_by_prefix[prefix][0]
        for prefix in ordered_prefixes
        if prefix in selected_by_prefix
    ]
    if len(selected_targets) != len(set(selected_targets)):
        raise AssertionError("IPv6 prefix target selection produced duplicate targets")

    with tempfile.TemporaryDirectory(dir=output_path.parent) as temporary_dir:
        temporary_root = Path(temporary_dir)
        staged_targets = temporary_root / "targets.txt"
        staged_mapping = temporary_root / "prefixes.tsv"
        staged_unrepresented = temporary_root / "unrepresented-prefixes.txt"
        staged_untargetable = temporary_root / "untargetable-prefixes.txt"
        with staged_targets.open("w", encoding="utf-8") as targets_output:
            with staged_mapping.open("w", encoding="utf-8") as mapping_output:
                mapping_output.write("prefix\ttarget\ttarget_source\n")
                for prefix in ordered_prefixes:
                    selected = selected_by_prefix.get(prefix)
                    if selected is None:
                        continue
                    address, target_source = selected
                    targets_output.write(f"{address}\n")
                    mapping_output.write(f"{prefix}\t{address}\t{target_source}\n")
        with staged_unrepresented.open("w", encoding="utf-8") as output:
            for prefix in unrepresented:
                output.write(f"{prefix}\n")
        with staged_untargetable.open("w", encoding="utf-8") as output:
            for prefix in untargetable:
                output.write(f"{prefix}\n")
        staged_targets.replace(output_path)
        staged_mapping.replace(mapping_path)
        staged_unrepresented.replace(unrepresented_path)
        staged_untargetable.replace(untargetable_path)

    return {
        **prefix_stats,
        "responsive_input_rows": input_rows,
        "invalid_responsive_rows": invalid_rows,
        "non_ipv6_responsive_rows": non_ipv6_rows,
        "non_global_responsive_rows": non_global_rows,
        "responsive_rows_matching_announced_prefixes": matching_rows,
        "responsive_rows_outside_announced_prefixes": unmatched_rows,
        "represented_prefixes": len(represented),
        "unrepresented_prefixes": len(unrepresented),
        "responsive_target_count": len(represented),
        "synthetic_target_count": (
            len(synthetic_by_prefix) if include_unresponsive_prefixes else 0
        ),
        "targetable_announced_prefixes": (
            len(ordered_prefixes) - len(untargetable)
        ),
        "untargetable_fully_covered_prefixes": len(untargetable),
        "target_count": len(selected_targets),
        "include_unresponsive_prefixes": include_unresponsive_prefixes,
        "selection": (
            "TUM responsive address when available; otherwise lowest seeded "
            "SHA-256 address rank outside announced more-specific prefixes"
            if include_unresponsive_prefixes
            else "lowest seeded SHA-256 responsive address per longest-matching BGP prefix"
        ),
        "mapping_file": str(mapping_path),
        "mapping_sha256": sha256_file(mapping_path),
        "unrepresented_file": str(unrepresented_path),
        "unrepresented_sha256": sha256_file(unrepresented_path),
        "untargetable_file": str(untargetable_path),
        "untargetable_sha256": sha256_file(untargetable_path),
    }


def download_responsive_source(output_dir: Path, url: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / (Path(url).name or "responsive-addresses.txt.xz")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=output_dir, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            logger.info("downloading responsive IPv6 source from %s", url)
            with urllib.request.urlopen(url, timeout=120) as response:
                shutil.copyfileobj(response, temporary)
        temporary_path.replace(destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    logger.info("downloaded responsive IPv6 source to %s", destination)
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Select one deterministic IPv6 target per route-selectable announced "
            "BGP prefix, preferring responsive TUM Hitlist addresses."
        )
    )
    rib_source = parser.add_mutually_exclusive_group(required=True)
    rib_source.add_argument("--rib", type=Path)
    rib_source.add_argument("--download-latest-rib", action="store_true")
    responsive_source = parser.add_mutually_exclusive_group(required=True)
    responsive_source.add_argument("--responsive-source", type=Path)
    responsive_source.add_argument("--download-responsive", action="store_true")
    parser.add_argument("--collector", default="route-views6")
    parser.add_argument(
        "--download-dir", type=Path, default=Path("target_generation/ipv6_bgp/downloads")
    )
    parser.add_argument("--responsive-url", default=DEFAULT_RESPONSIVE_URL)
    parser.add_argument(
        "--responsive-source-url",
        help="provenance URL for a local --responsive-source file",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--mapping", type=Path)
    parser.add_argument("--unrepresented", type=Path)
    parser.add_argument("--untargetable", type=Path)
    parser.add_argument("--seed", default="scamper-ipv6-prefix-target-v1")
    parser.add_argument("--include-non-global", action="store_true")
    parser.add_argument(
        "--include-unresponsive-prefixes",
        action="store_true",
        help=(
            "emit deterministic in-prefix targets when no TUM responsive address "
            "exists; fully covered parent routes are reported as untargetable"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    source_metadata: dict[str, object] = {}
    rib_path = args.rib
    if args.download_latest_rib:
        logger.info("resolving latest RouteViews RIB for %s", args.collector)
        rib_path, source_metadata = download_latest_rib(
            args.download_dir / args.collector,
            args.collector,
        )
    assert rib_path is not None
    rib_path = rib_path.expanduser().resolve()
    if not rib_path.is_file():
        raise FileNotFoundError(rib_path)

    responsive_path = args.responsive_source
    if args.download_responsive:
        responsive_path = download_responsive_source(
            args.download_dir,
            args.responsive_url,
        )
    assert responsive_path is not None
    responsive_path = responsive_path.expanduser().resolve()
    if not responsive_path.is_file():
        raise FileNotFoundError(responsive_path)

    logger.info("building IPv6 prefix inventory from %s", rib_path)
    stats = generate_targets(
        read_prefixes(rib_path),
        responsive_path,
        args.output,
        seed=args.seed,
        include_non_global=args.include_non_global,
        include_unresponsive_prefixes=args.include_unresponsive_prefixes,
        mapping_path=args.mapping,
        unrepresented_path=args.unrepresented,
        untargetable_path=args.untargetable,
    )
    metadata_path = args.metadata or Path(f"{args.output}.metadata.json")
    metadata = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rib_source_file": str(rib_path),
        "rib_source_sha256": sha256_file(rib_path),
        **source_metadata,
        "responsive_source_file": str(responsive_path),
        "responsive_source_url": (
            args.responsive_url
            if args.download_responsive
            else args.responsive_source_url
        ),
        "responsive_source_sha256": sha256_file(responsive_path),
        "seed": args.seed,
        "include_non_global": args.include_non_global,
        **stats,
        "output_file": str(args.output.expanduser().resolve()),
        "output_sha256": sha256_file(args.output),
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
