from __future__ import annotations

import ipaddress
from pathlib import Path

from target_generation.ipv4_bgp.generate import generate_targets, prefixes_from_text


def test_prefix_text_accepts_pfx2as_and_cidr(tmp_path: Path) -> None:
    source = tmp_path / "rib.txt"
    source.write_text(
        "8.8.0.0\t16\t15169\n9.9.9.0/24 19281\n2001:db8::/32 64500\n",
        encoding="utf-8",
    )

    with source.open(encoding="utf-8") as stream:
        assert list(prefixes_from_text(stream)) == [
            ipaddress.ip_network("8.8.0.0/16"),
            ipaddress.ip_network("9.9.9.0/24"),
        ]


def test_slash_16_expands_to_256_unique_24_targets(tmp_path: Path) -> None:
    output = tmp_path / "targets.txt"
    stats = generate_targets(
        [ipaddress.ip_network("8.8.0.0/16")],
        output,
        seed="test",
    )
    targets = [ipaddress.ip_address(line) for line in output.read_text().splitlines()]

    assert stats["target_count"] == 256
    assert len({int(address) >> 8 for address in targets}) == 256
    assert all(address in ipaddress.ip_network("8.8.0.0/16") for address in targets)


def test_overlaps_are_deduplicated_and_output_is_deterministic(tmp_path: Path) -> None:
    prefixes = [
        ipaddress.ip_network("8.8.8.0/24"),
        ipaddress.ip_network("8.8.8.0/25"),
        ipaddress.ip_network("8.8.8.128/25"),
    ]
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"

    generate_targets(prefixes, first, seed="same")
    generate_targets(reversed(prefixes), second, seed="same")

    assert first.read_bytes() == second.read_bytes()
    assert len(first.read_text().splitlines()) == 1


def test_more_specific_prefix_selects_address_inside_announcement(tmp_path: Path) -> None:
    output = tmp_path / "targets.txt"
    network = ipaddress.ip_network("8.8.8.128/26")

    generate_targets([network], output, seed="partial")

    assert ipaddress.ip_address(output.read_text().strip()) in network
