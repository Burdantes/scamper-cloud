from __future__ import annotations

import ipaddress
import lzma
from pathlib import Path

import pytest

from target_generation.ipv6_bgp.generate import (
    build_prefix_trie,
    deterministic_uncovered_target,
    generate_targets,
    prefixes_from_text,
)


def test_prefix_inventory_rejects_non_ipv6_rib() -> None:
    with pytest.raises(
        ValueError,
        match="BGP input contained no eligible announced IPv6 prefixes",
    ):
        build_prefix_trie([])


def test_prefix_text_accepts_pfx2as_and_cidr(tmp_path: Path) -> None:
    source = tmp_path / "rib.txt"
    source.write_text(
        "2001:4860::\t32\t15169\n"
        "2606:4700::/32 13335\n"
        "8.8.8.0/24 15169\n",
        encoding="utf-8",
    )

    with source.open(encoding="utf-8") as stream:
        assert list(prefixes_from_text(stream)) == [
            ipaddress.ip_network("2001:4860::/32"),
            ipaddress.ip_network("2606:4700::/32"),
        ]


def test_longest_prefix_match_selects_unique_deterministic_targets(
    tmp_path: Path,
) -> None:
    responsive = tmp_path / "responsive.txt.xz"
    rows = [
        "2606:4700::1",
        "2606:4700:1::1",
        "2606:4700:1::2",
        "2001:4860::1",
        "2001:db8::1",
        "192.0.2.1",
        "not-an-address",
    ]
    with lzma.open(responsive, "wt", encoding="utf-8") as output:
        output.write("\n".join(rows) + "\n")
    prefixes = [
        ipaddress.ip_network("2606:4700::/32"),
        ipaddress.ip_network("2606:4700:1::/48"),
        ipaddress.ip_network("2001:4860::/32"),
        ipaddress.ip_network("2620:fe::/48"),
    ]
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"

    stats = generate_targets(prefixes, responsive, first, seed="test")
    generate_targets(reversed(prefixes), responsive, second, seed="test")

    assert first.read_bytes() == second.read_bytes()
    assert stats["unique_announced_ipv6_prefixes"] == 4
    assert stats["represented_prefixes"] == 3
    assert stats["unrepresented_prefixes"] == 1
    assert stats["target_count"] == 3
    mapping = Path(f"{first}.prefixes.tsv").read_text(encoding="utf-8")
    assert "2606:4700::/32\t2606:4700::1\ttum-responsive" in mapping
    assert "2606:4700:1::/48\t" in mapping
    assert Path(f"{first}.unrepresented-prefixes.txt").read_text().strip() == (
        "2620:fe::/48"
    )


def test_source_order_does_not_change_selected_target(tmp_path: Path) -> None:
    prefixes = [ipaddress.ip_network("2606:4700::/32")]
    rows = ["2606:4700::1", "2606:4700::2", "2606:4700::3"]
    first_source = tmp_path / "first.txt"
    second_source = tmp_path / "second.txt"
    first_source.write_text("\n".join(rows) + "\n", encoding="utf-8")
    second_source.write_text("\n".join(reversed(rows)) + "\n", encoding="utf-8")
    first_output = tmp_path / "first-output.txt"
    second_output = tmp_path / "second-output.txt"

    generate_targets(prefixes, first_source, first_output, seed="same")
    generate_targets(prefixes, second_source, second_output, seed="same")

    assert first_output.read_bytes() == second_output.read_bytes()


def test_uncovered_target_avoids_announced_more_specifics() -> None:
    prefix = ipaddress.ip_network("2606:4700::/32")
    child = ipaddress.ip_network("2606:4700::/33")

    target = deterministic_uncovered_target(prefix, [child], seed="same")

    assert target is not None
    assert target in prefix
    assert target not in child


def test_complete_policy_prefers_tum_and_reports_fully_covered_routes(
    tmp_path: Path,
) -> None:
    responsive = tmp_path / "responsive.txt"
    responsive.write_text(
        "2620:fe::9\n"
        "2606:4700::9\n"
        "2606:4700:8000::9\n",
        encoding="utf-8",
    )
    prefixes = [
        ipaddress.ip_network("2606:4700::/32"),
        ipaddress.ip_network("2606:4700::/33"),
        ipaddress.ip_network("2606:4700:8000::/33"),
        ipaddress.ip_network("2620:fe::/48"),
        ipaddress.ip_network("2001:4860::/32"),
    ]
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"

    stats = generate_targets(
        prefixes,
        responsive,
        first,
        seed="complete",
        include_unresponsive_prefixes=True,
    )
    generate_targets(
        reversed(prefixes),
        responsive,
        second,
        seed="complete",
        include_unresponsive_prefixes=True,
    )

    assert first.read_bytes() == second.read_bytes()
    assert stats["unique_announced_ipv6_prefixes"] == 5
    assert stats["responsive_target_count"] == 3
    assert stats["synthetic_target_count"] == 1
    assert stats["untargetable_fully_covered_prefixes"] == 1
    assert stats["target_count"] == 4

    mapping_rows = Path(f"{first}.prefixes.tsv").read_text(encoding="utf-8").splitlines()
    assert mapping_rows[0] == "prefix\ttarget\ttarget_source"
    assert any(row.endswith("\ttum-responsive") for row in mapping_rows[1:])
    synthetic = next(
        row for row in mapping_rows[1:] if row.endswith("\tdeterministic-uncovered")
    )
    synthetic_prefix, synthetic_target, _ = synthetic.split("\t")
    assert ipaddress.ip_address(synthetic_target) in ipaddress.ip_network(
        synthetic_prefix
    )
    assert Path(f"{first}.untargetable-prefixes.txt").read_text().strip() == (
        "2606:4700::/32"
    )
