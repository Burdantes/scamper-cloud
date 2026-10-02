from __future__ import annotations

import argparse
import hashlib
import heapq
import ipaddress
import json
import lzma
import shutil
import sqlite3
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

DEFAULT_RESPONSIVE_URL = (
    "https://alcatraz.net.in.tum.de/ipv6-hitlist-service/open/"
    "responsive-addresses.txt.xz"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def materialize_source(source: str, download_dir: Path) -> tuple[Path, str | None]:
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme in {"http", "https"}:
        download_dir.mkdir(parents=True, exist_ok=True)
        name = Path(parsed.path).name or "responsive-addresses.txt.xz"
        destination = download_dir / name
        with urllib.request.urlopen(source, timeout=120) as response:
            with destination.open("wb") as output:
                shutil.copyfileobj(response, output)
        return destination, source
    if parsed.scheme == "file":
        path = Path(urllib.request.url2pathname(parsed.path))
    elif parsed.scheme:
        raise ValueError(f"unsupported source scheme: {parsed.scheme}")
    else:
        path = Path(source).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"IPv6 Hitlist source does not exist: {path}")
    return path.resolve(), None


def open_text_source(path: Path) -> TextIO:
    if path.suffix == ".xz":
        return lzma.open(path, "rt", encoding="utf-8", errors="strict")
    return path.open("r", encoding="utf-8")


def selection_score(seed: str, address: str) -> int:
    return int.from_bytes(
        hashlib.sha256(f"{seed}\0{address}".encode("ascii")).digest(), "big"
    )


def import_hitlist(
    source_path: Path,
    output_path: Path,
    *,
    source_url: str | None = None,
    max_targets: int | None = None,
    seed: str = "scamper-cloud-ipv6-v1",
    include_non_global: bool = False,
) -> dict[str, object]:
    if max_targets is not None and max_targets <= 0:
        raise ValueError("max_targets must be greater than zero")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    selected: list[tuple[int, str]] = []
    input_rows = 0
    invalid_rows = 0
    non_ipv6_rows = 0
    non_global_rows = 0
    duplicate_rows = 0
    eligible_rows = 0

    with tempfile.TemporaryDirectory(dir=output_path.parent) as temporary_dir:
        database_path = Path(temporary_dir) / "seen.sqlite3"
        staged_output = Path(temporary_dir) / "targets.txt"
        with sqlite3.connect(database_path) as database:
            database.execute("PRAGMA journal_mode=OFF")
            database.execute("PRAGMA synchronous=OFF")
            database.execute("CREATE TABLE seen (address TEXT PRIMARY KEY)")
            with open_text_source(source_path) as source:
                with staged_output.open("w", encoding="utf-8") as output:
                    for raw_line in source:
                        input_rows += 1
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
                        try:
                            database.execute(
                                "INSERT INTO seen(address) VALUES (?)", (canonical,)
                            )
                        except sqlite3.IntegrityError:
                            duplicate_rows += 1
                            continue
                        eligible_rows += 1
                        if max_targets is None:
                            output.write(f"{canonical}\n")
                            continue
                        score = selection_score(seed, canonical)
                        item = (-score, canonical)
                        if len(selected) < max_targets:
                            heapq.heappush(selected, item)
                        elif score < -selected[0][0]:
                            heapq.heapreplace(selected, item)

        if max_targets is not None:
            with staged_output.open("w", encoding="utf-8") as output:
                for _, address in sorted(
                    ((-negative_score, address) for negative_score, address in selected)
                ):
                    output.write(f"{address}\n")
        target_count = min(eligible_rows, max_targets or eligible_rows)
        if target_count == 0:
            raise ValueError("IPv6 Hitlist source contained no eligible IPv6 targets")
        staged_output.replace(output_path)

    retrieved_at = datetime.now(timezone.utc).isoformat()
    metadata = {
        "schema_version": 1,
        "generator": "target_generation.ipv6_hitlist.import_hitlist",
        "source_file": str(source_path),
        "source_url": source_url,
        "source_sha256": sha256_file(source_path),
        "retrieved_at": retrieved_at,
        "selection_policy": (
            "all-eligible-in-source-order"
            if max_targets is None
            else "lowest-sha256-score"
        ),
        "selection_seed": seed if max_targets is not None else None,
        "max_targets": max_targets,
        "include_non_global": include_non_global,
        "input_rows": input_rows,
        "invalid_rows": invalid_rows,
        "non_ipv6_rows": non_ipv6_rows,
        "non_global_rows": non_global_rows,
        "duplicate_rows": duplicate_rows,
        "eligible_unique_ipv6_rows": eligible_rows,
        "target_count": target_count,
        "output_file": str(output_path),
        "output_sha256": sha256_file(output_path),
    }
    metadata_path = Path(f"{output_path}.metadata.json")
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import a canonical, reproducible target set from the TUM IPv6 Hitlist."
    )
    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument("--source", help="local path, file URL, or HTTP(S) URL")
    source.add_argument(
        "--download-responsive",
        action="store_true",
        help="download the public responsive, non-aliased IPv6 list",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-targets", type=int)
    parser.add_argument("--seed", default="scamper-cloud-ipv6-v1")
    parser.add_argument("--include-non-global", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = args.source or DEFAULT_RESPONSIVE_URL
    with tempfile.TemporaryDirectory() as download_dir:
        source_path, source_url = materialize_source(source, Path(download_dir))
        metadata = import_hitlist(
            source_path,
            args.output,
            source_url=source_url,
            max_targets=args.max_targets,
            seed=args.seed,
            include_non_global=args.include_non_global,
        )
    print(json.dumps(metadata, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
