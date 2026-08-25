from __future__ import annotations

import lzma
from pathlib import Path

from target_generation.ipv6_hitlist.import_hitlist import import_hitlist


def test_hitlist_import_is_deterministic_and_filters_bad_rows(tmp_path: Path) -> None:
    rows = [
        "2606:4700:4700::1111",
        "2001:4860:4860::8888",
        "2620:fe::fe",
        "2606:4700:4700::1111",
        "192.0.2.1",
        "not-an-address",
        "2001:db8::1",
    ]
    first_source = tmp_path / "first.txt.xz"
    second_source = tmp_path / "second.txt.xz"
    with lzma.open(first_source, "wt", encoding="utf-8") as output:
        output.write("\n".join(rows) + "\n")
    with lzma.open(second_source, "wt", encoding="utf-8") as output:
        output.write("\n".join(reversed(rows)) + "\n")

    first_output = tmp_path / "first.targets.txt"
    second_output = tmp_path / "second.targets.txt"
    metadata = import_hitlist(first_source, first_output, max_targets=2, seed="test")
    import_hitlist(second_source, second_output, max_targets=2, seed="test")

    assert first_output.read_bytes() == second_output.read_bytes()
    assert metadata["target_count"] == 2
    assert metadata["duplicate_rows"] == 1
    assert metadata["non_ipv6_rows"] == 1
    assert metadata["invalid_rows"] == 1
    assert metadata["non_global_rows"] == 1
    assert Path(f"{first_output}.metadata.json").is_file()
