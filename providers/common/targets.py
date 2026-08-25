"""Provider-neutral preparation of immutable IPv4 campaign target sets."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import urllib.parse
import urllib.request
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from controller.target_registry import load_registered_target

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedTarget:
    source: str
    version: str
    normalized_file: str
    normalized_sha256: str
    target_count: int


@dataclass(frozen=True)
class PreparedTargets:
    trace: PreparedTarget
    rr: PreparedTarget
    do_not_probe_file: str | None
    do_not_probe_version: str | None

    def as_manifest(self) -> dict[str, dict[str, str | int]]:
        return {
            measurement: {
                "source": prepared.source,
                "version": prepared.version,
                "normalized_file": prepared.normalized_file,
                "normalized_sha256": prepared.normalized_sha256,
                "target_count": prepared.target_count,
            }
            for measurement, prepared in (("trace", self.trace), ("rr", self.rr))
        }


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def target_count(path: str | Path) -> int:
    with Path(path).open("r", encoding="utf-8") as source:
        return sum(1 for line in source if line.strip())


def materialize_target_source(
    target_source: str,
    log_dir: str | Path,
    prefix: str,
    *,
    label: str,
) -> str:
    parsed = urllib.parse.urlparse(target_source)
    if parsed.scheme in {"http", "https"}:
        suffix = Path(parsed.path).suffix or ".txt"
        destination = Path(log_dir) / f"{prefix}-{label}-target-source{suffix}"
        logger.info("Downloading complete %s target source %s", label, target_source)
        urllib.request.urlretrieve(target_source, destination)
        return str(destination)
    if parsed.scheme == "file":
        path = Path(urllib.request.url2pathname(parsed.path))
    elif parsed.scheme:
        raise ValueError(f"unsupported target source scheme: {parsed.scheme}")
    else:
        path = Path(target_source).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"target source does not exist: {path}")
    return str(path)


def load_do_not_probe_prefixes(path: str | Path) -> tuple[ipaddress.IPv4Network, ...]:
    networks: list[ipaddress.IPv4Network] = []
    with Path(path).open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            value = line.split("#", 1)[0].strip()
            if not value:
                continue
            try:
                network = ipaddress.ip_network(value, strict=False)
            except ValueError as error:
                raise ValueError(
                    f"invalid do-not-probe entry on line {line_number}: {value!r}"
                ) from error
            if network.version != 4:
                raise ValueError(
                    f"non-IPv4 do-not-probe entry on line {line_number}: {value!r}"
                )
            networks.append(network)
    return tuple(ipaddress.collapse_addresses(networks))


def _address_is_excluded(
    address: ipaddress.IPv4Address,
    networks: tuple[ipaddress.IPv4Network, ...],
    starts: tuple[int, ...],
) -> bool:
    if not networks:
        return False
    index = bisect_right(starts, int(address)) - 1
    return index >= 0 and address in networks[index]


def build_target_file(
    log_dir: str | Path,
    prefix: str,
    *,
    target_source: str | Path,
    max_targets: int | None = None,
    do_not_probe_networks: tuple[ipaddress.IPv4Network, ...] = (),
) -> str:
    source_path = Path(target_source)
    suffix = f"-{max_targets}" if max_targets is not None else ""
    target_path = Path(log_dir) / f"{prefix}-targets{suffix}.txt"
    starts = tuple(int(network.network_address) for network in do_not_probe_networks)
    written = 0
    excluded = 0
    seen: set[int] = set()
    with source_path.open("r", encoding="utf-8") as source:
        with target_path.open("w", encoding="utf-8") as destination:
            for line_number, line in enumerate(source, start=1):
                if max_targets is not None and written >= max_targets:
                    break
                value = line.rstrip("\r\n").split("\t", 1)[0].strip()
                if not value:
                    continue
                try:
                    address = ipaddress.ip_address(value)
                except ValueError as error:
                    raise ValueError(
                        f"invalid target on source line {line_number}: {value!r}"
                    ) from error
                if address.version != 4:
                    raise ValueError(
                        f"non-IPv4 target on source line {line_number}: {value!r}"
                    )
                encoded = int(address)
                if encoded in seen:
                    raise ValueError(
                        f"duplicate target on source line {line_number}: {address}"
                    )
                seen.add(encoded)
                if _address_is_excluded(address, do_not_probe_networks, starts):
                    excluded += 1
                    continue
                destination.write(f"{address}\n")
                written += 1
    if written == 0:
        raise ValueError(f"target source contained no IPv4 destinations: {source_path}")
    logger.info(
        "Created normalized target file %s with %d targets; excluded %d targets",
        target_path,
        written,
        excluded,
    )
    return str(target_path)


def prepare_target_sets(
    *,
    log_dir: str | Path,
    prefix: str,
    fallback_source: str,
    trace_target_source: str | None,
    rr_target_source: str | None,
    max_targets: int | None,
    do_not_probe_file: str | None,
) -> PreparedTargets:
    networks: tuple[ipaddress.IPv4Network, ...] = ()
    do_not_probe_version = None
    normalized_do_not_probe_file = None
    if do_not_probe_file:
        normalized_do_not_probe_file = str(Path(do_not_probe_file).expanduser())
        networks = load_do_not_probe_prefixes(normalized_do_not_probe_file)
        do_not_probe_version = (
            f"{Path(normalized_do_not_probe_file).name}@sha256:"
            f"{sha256_file(normalized_do_not_probe_file)}"
        )

    prepared: dict[str, PreparedTarget] = {}
    for measurement, source in (
        ("trace", trace_target_source or fallback_source),
        ("rr", rr_target_source or fallback_source),
    ):
        local_source = materialize_target_source(
            source,
            log_dir,
            prefix,
            label=measurement,
        )
        registered = load_registered_target(Path(local_source))
        version = (
            registered.source_version
            if registered is not None
            else f"{Path(local_source).name}@sha256:{sha256_file(local_source)}"
        )
        if registered is not None and max_targets is None and not networks:
            normalized_file = local_source
            normalized_sha256 = registered.normalized_sha256
            count = registered.target_count
        else:
            normalized_file = build_target_file(
                log_dir,
                f"{prefix}-{measurement}",
                target_source=local_source,
                max_targets=max_targets,
                do_not_probe_networks=networks,
            )
            normalized_sha256 = sha256_file(normalized_file)
            count = target_count(normalized_file)
        prepared[measurement] = PreparedTarget(
            source=source,
            version=version,
            normalized_file=normalized_file,
            normalized_sha256=normalized_sha256,
            target_count=count,
        )

    return PreparedTargets(
        trace=prepared["trace"],
        rr=prepared["rr"],
        do_not_probe_file=normalized_do_not_probe_file,
        do_not_probe_version=do_not_probe_version,
    )
