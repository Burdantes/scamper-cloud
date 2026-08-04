from __future__ import annotations

import argparse
import gzip
import hashlib
import ipaddress
import json
import shutil
import subprocess
import sys
import urllib.request
from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

ROUTEVIEWS_COLLECTORS_URL = "https://api.routeviews.org/meta/collectors"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_latest_rib(output_dir: Path, collector: str) -> tuple[Path, dict[str, object]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(ROUTEVIEWS_COLLECTORS_URL, timeout=30) as response:
        metadata = json.load(response)
    collectors = metadata.get("data", {}).get("collectors", {})
    if collector not in collectors:
        available = ", ".join(sorted(collectors))
        raise ValueError(f"unknown RouteViews collector {collector!r}; available: {available}")
    ribs = collectors[collector]["dataTypes"]["ribs"]
    rib_url = ribs["latestDumpFile"]
    destination = output_dir / Path(rib_url).name
    if not destination.is_file():
        with urllib.request.urlopen(rib_url, timeout=60) as response:
            with destination.open("wb") as output:
                shutil.copyfileobj(response, output)
    return destination, {
        "collector": collector,
        "rib_url": rib_url,
        "rib_timestamp": ribs.get("latestDumpTimeISO8601"),
        "routeviews_metadata_time": metadata.get("timeISO8601"),
    }


def parsed_prefix(value: str) -> ipaddress.IPv4Network | None:
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None
    return network if network.version == 4 else None


def prefixes_from_text(source: TextIO) -> Iterator[ipaddress.IPv4Network]:
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


def prefixes_from_bgpdump(path: Path, executable: str = "bgpdump") -> Iterator[ipaddress.IPv4Network]:
    if shutil.which(executable) is None:
        raise RuntimeError(
            "bgpdump is required for MRT RIB input; install it with `brew install bgpdump`"
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
        raise RuntimeError(f"bgpdump failed with exit code {return_code}: {stderr.strip()}")


def read_prefixes(path: Path) -> Iterator[ipaddress.IPv4Network]:
    if path.suffix == ".bz2":
        yield from prefixes_from_bgpdump(path)
        return
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as source:
        yield from prefixes_from_text(source)


def merge_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def contains_index(intervals: list[tuple[int, int]], index: int) -> bool:
    low = 0
    high = len(intervals)
    while low < high:
        middle = (low + high) // 2
        start, end = intervals[middle]
        if index < start:
            high = middle
        elif index > end:
            low = middle + 1
        else:
            return True
    return False


def deterministic_offset(seed: str, network_index: int, size: int) -> int:
    material = f"{seed}:{ipaddress.ip_address(network_index << 8)}/24".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") % size


def eligible_24(network_index: int, include_non_global: bool) -> bool:
    if include_non_global:
        return True
    return ipaddress.ip_address((network_index << 8) + 1).is_global


def select_partial_address(
    network_index: int,
    ranges: list[tuple[int, int]],
    seed: str,
) -> int:
    merged = merge_intervals(ranges)
    total = sum(end - start + 1 for start, end in merged)
    offset = deterministic_offset(seed, network_index, total)
    for start, end in merged:
        width = end - start + 1
        if offset < width:
            return start + offset
        offset -= width
    raise AssertionError("partial address selection exceeded available ranges")


def generate_targets(
    prefixes: Iterable[ipaddress.IPv4Network],
    output_path: Path,
    *,
    seed: str,
    include_non_global: bool = False,
) -> dict[str, int]:
    full_intervals: list[tuple[int, int]] = []
    partial_ranges: dict[int, list[tuple[int, int]]] = defaultdict(list)
    input_prefixes = 0
    skipped_default_routes = 0
    for network in prefixes:
        input_prefixes += 1
        if network.prefixlen == 0:
            skipped_default_routes += 1
            continue
        start_24 = int(network.network_address) >> 8
        end_24 = int(network.broadcast_address) >> 8
        if network.prefixlen <= 24:
            full_intervals.append((start_24, end_24))
        else:
            partial_ranges[start_24].append(
                (int(network.network_address), int(network.broadcast_address))
            )

    merged_full = merge_intervals(full_intervals)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    target_count = 0
    with output_path.open("w", encoding="utf-8") as output:
        for start, end in merged_full:
            for network_index in range(start, end + 1):
                if not eligible_24(network_index, include_non_global):
                    continue
                host = 1 + deterministic_offset(seed, network_index, 254)
                output.write(f"{ipaddress.ip_address((network_index << 8) + host)}\n")
                target_count += 1
        for network_index in sorted(partial_ranges):
            if contains_index(merged_full, network_index):
                continue
            if not eligible_24(network_index, include_non_global):
                continue
            address = select_partial_address(
                network_index,
                partial_ranges[network_index],
                seed,
            )
            output.write(f"{ipaddress.ip_address(address)}\n")
            target_count += 1

    return {
        "input_ipv4_prefix_rows": input_prefixes,
        "skipped_default_routes": skipped_default_routes,
        "merged_full_24_intervals": len(merged_full),
        "partial_24s": len(partial_ranges),
        "target_count": target_count,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate one deterministic candidate per /24 covered by a BGP RIB."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--rib", type=Path, help="MRT .bz2, pfx2as[.gz], or prefix text file")
    source.add_argument(
        "--download-latest",
        action="store_true",
        help="download the current RouteViews RIB reported by its metadata API",
    )
    parser.add_argument("--collector", default="route-views2")
    parser.add_argument(
        "--download-dir", type=Path, default=Path("target_generation/ipv4_bgp/ribs")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--seed", default="scamper-ipv4-target-v1")
    parser.add_argument("--include-non-global", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source_metadata: dict[str, object] = {}
    rib_path = args.rib
    if args.download_latest:
        rib_path, source_metadata = download_latest_rib(args.download_dir, args.collector)
    rib_path = rib_path.expanduser().resolve()
    if not rib_path.is_file():
        raise FileNotFoundError(rib_path)

    stats = generate_targets(
        read_prefixes(rib_path),
        args.output,
        seed=args.seed,
        include_non_global=args.include_non_global,
    )
    metadata_path = args.metadata or args.output.with_suffix(args.output.suffix + ".metadata.json")
    metadata = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_file": str(rib_path),
        "source_sha256": sha256_file(rib_path),
        **source_metadata,
        "seed": args.seed,
        "selection": "one deterministic address per announced IPv4 /24-equivalent",
        "include_non_global": args.include_non_global,
        **stats,
        "output_file": str(args.output.resolve()),
        "output_sha256": sha256_file(args.output),
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
