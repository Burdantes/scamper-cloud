import argparse
import hashlib
import ipaddress
import json
import shlex
import shutil
import tarfile
import urllib.parse
import urllib.request
from bisect import bisect_right

import os
import time
import logging
from providers import settings
from providers.preflight import assert_worker_assets
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from controller.target_registry import load_registered_target

credentials = None
compute = None


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than 0")
    return parsed


def positive_float(value):
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than 0")
    return parsed


def probe_payload_text(value):
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise argparse.ArgumentTypeError(
            "probe payload must contain ASCII text"
        ) from error
    if not encoded:
        raise argparse.ArgumentTypeError("probe payload must not be empty")
    if len(encoded) > 128:
        raise argparse.ArgumentTypeError("probe payload must be at most 128 bytes")
    return value


def csv_values(value):
    parsed = tuple(item.strip() for item in value.split(",") if item.strip())
    if not parsed:
        raise argparse.ArgumentTypeError("value must contain at least one item")
    return parsed


def normalized_object_prefix(value):
    parsed = value.strip("/")
    if not parsed or any(part in {".", ".."} for part in parsed.split("/")):
        raise argparse.ArgumentTypeError("object prefix must be a non-empty GCS path")
    return parsed


def max_instances_from_env():
    raw = os.environ.get("SCAMPER_GCP_MAX_INSTANCES") or os.environ.get(
        "SCAMPER_LEGACY_MAX_INSTANCES"
    )
    if not raw:
        return None
    return positive_int(raw)


def max_targets_from_env():
    raw = os.environ.get("SCAMPER_GCP_MAX_TARGETS") or os.environ.get(
        "SCAMPER_LEGACY_MAX_TARGETS"
    )
    if not raw:
        return None
    return positive_int(raw)


def get_credentials():
    global credentials
    if credentials is None:
        credential_path = Path(settings.WARTS_STORAGE_CREDENTIALS).expanduser()
        if credential_path.is_file():
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_file(
                credential_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        else:
            import google.auth

            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
    return credentials


def get_compute():
    global compute
    if compute is None:
        from googleapiclient import discovery

        compute = discovery.build("compute", "v1", credentials=get_credentials())
    return compute


def get_storage_client():
    from google.cloud import storage

    return storage.Client(project=settings.GCP_PROJECT, credentials=get_credentials())


# Logger
time_format = "%Y-%m-%d %H:%M:%S"
formatter = logging.Formatter(
    fmt="%(asctime)s - %(levelname)s - %(message)s", datefmt=time_format
)
logger = logging.getLogger()
handler = logging.StreamHandler()
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)


def record_expense_instances(instance_count):
    try:
        from providers.expenses import record_provider_instances_from_env

        if record_provider_instances_from_env("gcp", instance_count):
            logging.info("Recorded %d instances in the expense ledger", instance_count)
    except Exception as err:
        logging.warning("Could not update expense ledger: %s", err)


def send_to_cloud_storage(file_name, bucket_name, object_name=None):
    attempt = 0
    blob = None
    success = False
    max_attempts = int(os.environ.get("SCAMPER_UPLOAD_MAX_ATTEMPTS", "5"))
    while not success and attempt < max_attempts:
        try:
            attempt += 1
            storage_client = get_storage_client()
            bucket = storage_client.get_bucket(bucket_name)
            blob = bucket.blob(object_name or Path(file_name).name)
            logging.info(
                "Uploading results to Cloud Storage (try #{}): {}".format(attempt, blob)
            )
            blob.upload_from_filename(file_name)
            logging.info(
                "Successfully uploaded ({} attempts) {}.".format(attempt, blob)
            )
            success = True
        except Exception as err:
            logging.info(
                "Attempt {} failed to upload {} due to {}:{}".format(
                    attempt, blob, Exception, err
                )
            )
    if not success:
        raise RuntimeError(
            f"failed to upload {file_name} after {max_attempts} attempts"
        )


def uploaded_artifact_sizes(bucket_name, artifact_names):
    storage_client = get_storage_client()
    bucket = storage_client.bucket(bucket_name)
    sizes = {}
    for name in artifact_names:
        blob = bucket.get_blob(name)
        sizes[name] = int(blob.size or 0) if blob is not None else 0
    return sizes


def incomplete_uploaded_statuses(bucket_name, artifact_names):
    storage_client = get_storage_client()
    bucket = storage_client.bucket(bucket_name)
    incomplete = []
    for name in artifact_names:
        if not name.endswith(".status.json"):
            continue
        blob = bucket.get_blob(name)
        if blob is None:
            incomplete.append(name)
            continue
        try:
            status = json.loads(blob.download_as_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            incomplete.append(name)
            continue
        if status.get("complete") is not True:
            incomplete.append(name)
    return incomplete


def missing_uploaded_artifacts(bucket_name, artifact_names):
    sizes = uploaded_artifact_sizes(bucket_name, artifact_names)
    return [name for name in artifact_names if sizes.get(name, 0) <= 0]


def package_and_upload_logs(log_dir, bucket_name, object_prefix):
    if not os.path.isdir(log_dir):
        logging.info("Log directory %s does not exist; skipping log upload", log_dir)
        return

    logging.info("Zipping logs")
    tarname = Path(log_dir).with_name(f"{Path(log_dir).stem}.tar.gz")
    with tarfile.open(tarname, "w:gz") as tar:
        for path in Path(log_dir).iterdir():
            if path.suffix == ".log" or path.name.endswith(".manifest.json"):
                tar.add(path, arcname=f"{Path(log_dir).name}/{path.name}")

    logging.info("Sending logs to gcp bucket %s", bucket_name)
    send_to_cloud_storage(
        tarname,
        bucket_name,
        f"{object_prefix}/logs/{tarname.name}",
    )

    logging.info("Removing logs and zipped logs")
    shutil.rmtree(log_dir)
    tarname.unlink()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def materialize_target_source(target_source, log_dir, prefix, label=None):
    parsed = urllib.parse.urlparse(target_source)
    if parsed.scheme in {"http", "https"}:
        suffix = Path(parsed.path).suffix or ".txt"
        name = f"{prefix}-{label}-target-source" if label else f"{prefix}-target-source"
        destination = Path(log_dir) / f"{name}{suffix}"
        logging.info("Downloading complete target source %s", target_source)
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


def load_do_not_probe_prefixes(path):
    if path is None:
        return ()
    source_path = Path(path).expanduser()
    if not source_path.is_file():
        raise FileNotFoundError(f"do-not-probe file does not exist: {source_path}")
    networks = []
    with source_path.open("r", encoding="utf-8") as source:
        for line_number, raw_line in enumerate(source, start=1):
            value = raw_line.split("#", 1)[0].strip()
            if not value:
                continue
            try:
                network = ipaddress.ip_network(value, strict=False)
            except ValueError as error:
                raise ValueError(
                    f"invalid do-not-probe prefix on line {line_number}: {value!r}"
                ) from error
            networks.append(network)
    ipv4 = ipaddress.collapse_addresses(
        network for network in networks if network.version == 4
    )
    ipv6 = ipaddress.collapse_addresses(
        network for network in networks if network.version == 6
    )
    return (*ipv4, *ipv6)


def address_is_excluded(address, networks, network_starts):
    if not networks:
        return False
    index = bisect_right(network_starts, int(address)) - 1
    return index >= 0 and address in networks[index]


def build_target_file(
    log_dir,
    prefix,
    max_targets=None,
    target_source=None,
    do_not_probe_networks=(),
    address_family=4,
):
    if address_family not in {4, 6}:
        raise ValueError("address_family must be 4 or 6")
    source_path = target_source or settings.SCAMPER_IP_DST
    suffix = f"-{max_targets}" if max_targets is not None else ""
    target_path = Path(log_dir) / f"{prefix}-targets{suffix}.txt"
    written = 0
    excluded = 0
    seen = set()
    family_networks = tuple(
        network for network in do_not_probe_networks if network.version == address_family
    )
    network_starts = tuple(int(network.network_address) for network in family_networks)
    with open(source_path, "r", encoding="utf-8") as src:
        with target_path.open("w", encoding="utf-8") as dst:
            for line_number, line in enumerate(src, start=1):
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
                if address.version != address_family:
                    raise ValueError(
                        f"non-IPv{address_family} target on source line "
                        f"{line_number}: {value!r}"
                    )
                normalized = str(address)
                if normalized in seen:
                    raise ValueError(
                        f"duplicate target on source line {line_number}: {normalized}"
                    )
                seen.add(normalized)
                if address_is_excluded(
                    address,
                    family_networks,
                    network_starts,
                ):
                    excluded += 1
                    continue
                dst.write(normalized + "\n")
                written += 1
    if written == 0:
        raise ValueError(
            f"target source contained no IPv{address_family} destinations: {source_path}"
        )
    logging.info(
        "Created normalized target file %s with %d targets; excluded %d targets",
        target_path,
        written,
        excluded,
    )
    return str(target_path)


def command_strings(
    trace_rate, rr_rate, rr_timeout, payload_text=None, trace6_rate=None
):
    payload_hex = payload_text.encode("ascii").hex() if payload_text else None
    trace_payload = f" -p {payload_hex}" if payload_hex else ""
    rr_payload = f" -B {payload_hex}" if payload_hex else ""
    trace6_rate = trace_rate if trace6_rate is None else trace6_rate
    return {
        "trace": (
            "scamper -c 'trace -m 20 -g 8 -w 3 -q 2 -P ICMP"
            f"{trace_payload}' -p {trace_rate} -f SHUFFLED_TARGETS "
            "-o OUTPUT.trace.warts -O warts"
        ),
        "trace6": (
            "scamper -c 'trace -m 20 -g 8 -w 3 -q 2 -P ICMP"
            f"{trace_payload}' -p {trace6_rate} -f SHUFFLED_TARGETS "
            "-o OUTPUT.trace6.warts -O warts"
        ),
        "rr": (
            "scamper -c 'ping -P icmp-echo -R -c 1 "
            f"-W {rr_timeout:g}{rr_payload}' -p {rr_rate} "
            "-f SHUFFLED_TARGETS -o OUTPUT.rr.warts -O warts"
        ),
    }


def init_cmd(target_files):
    ssh_key = str(Path(settings.GCP_SCAMPER_SSH_KEY).expanduser())
    command = [
        "scp",
        "-i",
        ssh_key,
        "-oStrictHostKeyChecking=no",
        "-oUserKnownHostsFile=/dev/null",
    ]
    credential_path = Path(settings.WARTS_STORAGE_CREDENTIALS).expanduser()
    if credential_path.is_file():
        command.append(str(credential_path))
    command.extend(target_files)
    command.extend(
        [
            settings.GCP_SCAMPER_SCRIPT,
            settings.SCAMPER_SMOKE_SCRIPT,
            settings.SCAMPER_CAMPAIGN_RUNNER,
            settings.SCAMPER_UPLOAD_SCRIPT,
        ]
    )
    return command


def remote_campaign_command(
    trace_target_file,
    rr_target_file,
    output_prefix,
    bucket_name,
    object_prefix,
    *,
    region,
    node,
    trace_target_source,
    trace_target_version,
    trace_target_count,
    trace_target_sha256,
    rr_target_source,
    rr_target_version,
    rr_target_count,
    rr_target_sha256,
    trace_rate,
    rr_rate,
    rr_timeout,
    measurements,
    trace6_target_file=None,
    trace6_target_source=None,
    trace6_target_version=None,
    trace6_target_count=None,
    trace6_target_sha256=None,
    trace6_rate=100,
    probe_payload,
    measurement_contact,
    do_not_probe_version,
    skip_smoke=False,
):
    environment = {
        "SCAMPER_PROVIDER": "gcp",
        "SCAMPER_REGION": region,
        "SCAMPER_NODE": node,
        "SCAMPER_TRACE_TARGET_SOURCE": trace_target_source,
        "SCAMPER_TRACE_TARGET_VERSION": trace_target_version,
        "SCAMPER_TRACE_TARGET_COUNT": str(trace_target_count),
        "SCAMPER_TRACE_TARGET_SHA256": trace_target_sha256,
        "SCAMPER_RR_TARGET_SOURCE": rr_target_source,
        "SCAMPER_RR_TARGET_VERSION": rr_target_version,
        "SCAMPER_RR_TARGET_COUNT": str(rr_target_count),
        "SCAMPER_RR_TARGET_SHA256": rr_target_sha256,
        "SCAMPER_TRACE_RATE_PPS": str(trace_rate),
        "SCAMPER_RR_RATE_PPS": str(rr_rate),
        "SCAMPER_TRACE6_RATE_PPS": str(trace6_rate),
        "SCAMPER_RR_TIMEOUT_SECONDS": f"{rr_timeout:g}",
        "SCAMPER_MEASUREMENTS": ",".join(measurements),
    }
    if trace6_target_file is not None:
        environment.update(
            {
                "SCAMPER_TRACE6_TARGET_SOURCE": str(trace6_target_source),
                "SCAMPER_TRACE6_TARGET_VERSION": str(trace6_target_version),
                "SCAMPER_TRACE6_TARGET_COUNT": str(trace6_target_count),
                "SCAMPER_TRACE6_TARGET_SHA256": str(trace6_target_sha256),
            }
        )
    if skip_smoke:
        environment["SCAMPER_SKIP_SMOKE"] = "1"
    if probe_payload:
        environment["SCAMPER_PROBE_PAYLOAD_TEXT"] = probe_payload
    if measurement_contact:
        environment["SCAMPER_MEASUREMENT_CONTACT"] = measurement_contact
    if do_not_probe_version:
        environment["SCAMPER_DO_NOT_PROBE_VERSION"] = do_not_probe_version
    assignments = " ".join(
        f"{name}={shlex.quote(value)}" for name, value in environment.items()
    )
    script = Path(settings.GCP_SCAMPER_SCRIPT).name
    argument_values = [Path(trace_target_file).name, Path(rr_target_file).name]
    if trace6_target_file is not None:
        argument_values.append(Path(trace6_target_file).name)
    argument_values.extend((output_prefix, bucket_name, object_prefix))
    arguments = " ".join(shlex.quote(value) for value in argument_values)
    return f"chmod +x {shlex.quote(script)}; {assignments} ./{shlex.quote(script)} {arguments}"


def expected_campaign_artifacts(object_prefix, output_prefix, measurements):
    artifacts = [f"{object_prefix}/{output_prefix}.status.json"]
    for measurement in measurements:
        artifacts.extend(
            [
                f"{object_prefix}/{output_prefix}.{measurement}.warts",
                f"{object_prefix}/{output_prefix}.{measurement}.metadata.json",
                f"{object_prefix}/{output_prefix}.{measurement}.targets.txt",
            ]
        )
    return artifacts


def target_count(path):
    with open(path, "r", encoding="utf-8") as source:
        return sum(1 for line in source if line.strip())


def write_run_manifest(
    log_dir,
    *,
    prefix,
    bucket_name,
    object_prefix,
    target_sets,
    regions,
    measurements,
    trace_rate,
    rr_rate,
    rr_timeout,
    probe_payload,
    measurement_contact,
    do_not_probe_file,
    do_not_probe_version,
    nodes,
    started_at,
    complete,
    failure,
    trace6_rate=None,
):
    manifest_path = Path(log_dir) / "manifest.json"
    manifest = {
        "schema_version": 1,
        "run_id": prefix,
        "provider": "gcp",
        "project": settings.GCP_PROJECT,
        "bucket": bucket_name,
        "object_prefix": object_prefix,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "complete": complete,
        "failure": failure,
        "target_sets": target_sets,
        "measurement_contact": measurement_contact,
        "probe_payload_text": probe_payload,
        "probe_payload_hex": probe_payload.encode("ascii").hex()
        if probe_payload
        else None,
        "do_not_probe_file": do_not_probe_file,
        "do_not_probe_version": do_not_probe_version,
        "do_not_probe_enforcement": (
            "controller_target_filter" if do_not_probe_file else None
        ),
        "regions": list(regions) if regions else "all-enabled-regions",
        "measurements": list(measurements),
        "commands": command_strings(
            trace_rate,
            rr_rate,
            rr_timeout,
            probe_payload,
            trace6_rate,
        ),
        "nodes": nodes,
        "failed_nodes": [node["node"] for node in nodes if not node["complete"]],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def close_instance_logs(logs):
    logging.info("Closing logs")
    for name, log in logs.items():
        if not log.closed:
            logging.info("Closing log for %s", name)
            log.close()


def wait_for_process(process, label, timeout_seconds):
    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        logging.warning("%s timed out after %s seconds", label, timeout_seconds)
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return 124


def terminate_process(process, label, grace_seconds=5):
    if process.poll() is not None:
        return
    logging.warning("Terminating lingering local process for %s", label)
    process.terminate()
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=grace_seconds)


def wait_for_campaign_processes(processes, bucket_name, on_node_terminal):
    timeout_seconds = float(
        os.environ.get("SCAMPER_GCP_SCAMPER_TIMEOUT_SECONDS", "172800")
    )
    poll_seconds = float(os.environ.get("SCAMPER_GCP_ARTIFACT_POLL_SECONDS", "30"))
    deadline = time.monotonic() + timeout_seconds
    last_missing = None
    pending = list(processes)
    exits_by_name = {}
    failed_nodes = []

    while pending:
        still_pending = []
        missing_count = 0
        missing_preview = []
        for process, info in pending:
            name = info["name"]
            artifact_names = info["artifact_names"]
            missing = missing_uploaded_artifacts(bucket_name, artifact_names)
            if missing and process.poll() is None:
                still_pending.append((process, info))
                missing_count += len(missing)
                missing_preview.extend(missing[: max(0, 5 - len(missing_preview))])
                continue

            incomplete = []
            failure = None
            if missing:
                failure = f"missing {len(missing)} expected artifacts: {missing[:5]}"
            else:
                incomplete = incomplete_uploaded_statuses(
                    bucket_name, artifact_names
                )
                if incomplete:
                    failure = f"incomplete status: {incomplete[:5]}"

            if process.poll() is None:
                terminate_process(process, f"ssh {name}")
            exit_code = process.wait()
            exits_by_name[name] = exit_code
            complete = failure is None
            on_node_terminal(info, complete)
            if complete:
                logging.info(
                    "Verified artifacts and cleaned up completed GCP worker %s",
                    name,
                )
            else:
                failed_nodes.append((name, failure))
                logging.error(
                    "Cleaned up failed GCP worker %s after terminal state: %s",
                    name,
                    failure,
                )

        pending = still_pending
        if not pending:
            break
        if missing_count != last_missing:
            logging.info(
                "Waiting for %d GCP campaign artifacts across %d workers",
                missing_count,
                len(pending),
            )
            last_missing = missing_count
        if time.monotonic() >= deadline:
            for process, info in pending:
                terminate_process(process, f"ssh {info['name']}")
            raise TimeoutError(
                f"timed out waiting for GCP artifacts after {timeout_seconds} seconds: {missing_preview}"
            )
        time.sleep(poll_seconds)

    if failed_nodes:
        raise RuntimeError(
            f"incomplete GCP campaign status for {len(failed_nodes)} nodes: "
            f"{failed_nodes[:5]}"
        )

    exits = [exits_by_name[info["name"]] for _process, info in processes]
    failed_exits = [exit_code for exit_code in exits if exit_code != 0]
    if failed_exits:
        logging.warning(
            "Ignoring nonzero SSH exits because campaign status is complete: %s",
            failed_exits,
        )
    return exits


def get_zones():
    zones = []
    request = get_compute().zones().list(project=settings.GCP_PROJECT)
    response = request.execute()
    for zone in response["items"]:
        zones.append(zone["name"])
    return zones


def zones_in_regions(zones, regions):
    if not regions:
        return list(zones)
    selected = [zone for zone in zones if zone.rsplit("-", 1)[0] in set(regions)]
    if not selected:
        raise ValueError(
            f"no GCP zones found in requested regions: {', '.join(regions)}"
        )
    return selected


def find_ips(prefix, zone):
    instances = []
    result = (
        get_compute()
        .instances()
        .list(project=settings.GCP_PROJECT, zone=zone)
        .execute()
    )
    if "items" not in result:
        return instances

    for instance in result["items"]:
        if prefix in instance["name"] and instance["status"] == "RUNNING":
            instances.append(
                (
                    instance["name"],
                    instance["networkInterfaces"][0]["accessConfigs"][0]["natIP"],
                    zone,
                )
            )
    return instances


def create_bucket(name):
    from googleapiclient import discovery
    from googleapiclient.errors import HttpError

    gcp_storage = discovery.build("storage", "v1", credentials=get_credentials())
    body = {
        "name": name,
        "storageClass": settings.GCP_STORAGE_CLASS,
        "location": settings.GCP_STORAGE_LOCATION,
        "locationType": "region",
    }
    try:
        return (
            gcp_storage.buckets()
            .insert(project=settings.GCP_PROJECT, body=body)
            .execute()
        )
    except HttpError as err:
        if getattr(err.resp, "status", None) == 409:
            logging.info("Bucket %s already exists; reusing it", name)
            return None
        raise


def wait_global_operation(project, operation):
    result = (
        get_compute()
        .globalOperations()
        .wait(project=project, operation=operation)
        .execute()
    )
    if "error" in result:
        raise RuntimeError(f"GCP global operation {operation} failed: {result['error']}")
    return result


def wait_region_operation(project, region, operation):
    result = (
        get_compute()
        .regionOperations()
        .wait(project=project, region=region, operation=operation)
        .execute()
    )
    if "error" in result:
        raise RuntimeError(
            f"GCP regional operation {operation} in {region} failed: {result['error']}"
        )
    return result


def controller_ssh_cidr():
    value = os.environ.get("SCAMPER_GCP_SSH_CIDR", "").strip()
    if not value:
        request = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/"
            "network-interfaces/0/access-configs/0/external-ip",
            headers={"Metadata-Flavor": "Google"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                value = response.read().decode("ascii").strip()
        except Exception as error:
            raise RuntimeError(
                "could not determine controller IPv4; set SCAMPER_GCP_SSH_CIDR"
            ) from error
    if "/" not in value:
        value = f"{value}/32"
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as error:
        raise ValueError("SCAMPER_GCP_SSH_CIDR must be a valid IPv4 /32") from error
    if network.version != 4 or network.prefixlen != 32:
        raise ValueError("SCAMPER_GCP_SSH_CIDR must be the controller's IPv4 /32")
    return str(network)


def ensure_dual_stack_subnetwork(project, region):
    """Create or reuse the controller-managed native dual-stack measurement subnet."""
    from googleapiclient.errors import HttpError

    client = get_compute()
    network_name = "scamper-dual-stack"
    subnet_name = f"scamper-dual-stack-{region}"
    try:
        client.networks().get(project=project, network=network_name).execute()
    except HttpError as error:
        if getattr(error.resp, "status", None) != 404:
            raise
        operation = client.networks().insert(
            project=project,
            body={"name": network_name, "autoCreateSubnetworks": False},
        ).execute()
        wait_global_operation(project, operation["name"])

    firewall_name = "scamper-controller-ssh"
    try:
        client.firewalls().get(project=project, firewall=firewall_name).execute()
    except HttpError as error:
        if getattr(error.resp, "status", None) != 404:
            raise
        operation = client.firewalls().insert(
            project=project,
            body={
                "name": firewall_name,
                "network": f"global/networks/{network_name}",
                "direction": "INGRESS",
                "sourceRanges": [controller_ssh_cidr()],
                "targetTags": ["scamper-worker"],
                "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}],
            },
        ).execute()
        wait_global_operation(project, operation["name"])

    try:
        subnet = client.subnetworks().get(
            project=project, region=region, subnetwork=subnet_name
        ).execute()
    except HttpError as error:
        if getattr(error.resp, "status", None) != 404:
            raise
        digest = hashlib.sha256(region.encode("ascii")).digest()
        cidr = f"10.{digest[0]}.{digest[1]}.0/24"
        operation = client.subnetworks().insert(
            project=project,
            region=region,
            body={
                "name": subnet_name,
                "network": f"global/networks/{network_name}",
                "ipCidrRange": cidr,
                "stackType": "IPV4_IPV6",
                "ipv6AccessType": "EXTERNAL",
            },
        ).execute()
        wait_region_operation(project, region, operation["name"])
        subnet = client.subnetworks().get(
            project=project, region=region, subnetwork=subnet_name
        ).execute()
    if subnet.get("stackType") != "IPV4_IPV6" or subnet.get("ipv6AccessType") != "EXTERNAL":
        raise RuntimeError(f"GCP subnet {subnet_name} is not external dual-stack")
    return subnet["selfLink"]


def create_instance(project, zone, name, ipv6_enabled=False):
    logging.info("Creating %s in %s", name, zone)
    client = get_compute()
    image_response = (
        client.images()
        .getFromFamily(
            project=settings.GCP_IMAGE_PROJECT, family=settings.GCP_IMAGE_FAMILY
        )
        .execute()
    )

    source_disk_image = image_response["selfLink"]
    machine_type = "zones/%s/machineTypes/%s" % (zone, settings.GCP_MACHINE_TYPE)
    network_interface = {
        "network": "global/networks/default",
        "accessConfigs": [
            {
                "name": "External NAT",
                "type": "ONE_TO_ONE_NAT",
                "networkTier": settings.GCP_NETWORK_TIER,
            }
        ],
        "stackType": "IPV4_ONLY",
    }
    if ipv6_enabled:
        region = zone.rsplit("-", 1)[0]
        network_interface = {
            "subnetwork": ensure_dual_stack_subnetwork(project, region),
            "accessConfigs": [
                {
                    "name": "External NAT",
                    "type": "ONE_TO_ONE_NAT",
                    "networkTier": settings.GCP_NETWORK_TIER,
                }
            ],
            "ipv6AccessConfigs": [
                {
                    "name": "External IPv6",
                    "type": "DIRECT_IPV6",
                    "networkTier": "PREMIUM",
                }
            ],
            "stackType": "IPV4_IPV6",
        }
    config = {
        "name": name,
        "tags": {"items": ["scamper-worker"]},
        "machineType": machine_type,
        "disks": [
            {
                "boot": True,
                "autoDelete": True,
                "initializeParams": {
                    "sourceImage": source_disk_image,
                },
            }
        ],
        "networkInterfaces": [network_interface],
        "serviceAccounts": [
            {"email": settings.GCP_SERVICE_ACCOUNT, "scopes": settings.GCP_SCOPES}
        ],
    }
    public_key_path = Path(f"{Path(settings.GCP_SCAMPER_SSH_KEY).expanduser()}.pub")
    if public_key_path.is_file():
        public_key = public_key_path.read_text(encoding="utf-8").strip()
        if not public_key:
            raise ValueError(f"SSH public key is empty: {public_key_path}")
        config["metadata"] = {
            "items": [
                {
                    "key": "ssh-keys",
                    "value": f"{settings.GCP_SCAMPER_USER}:{public_key}",
                }
            ]
        }
    return client.instances().insert(project=project, zone=zone, body=config).execute()


def delete_instance(project, zone, name):
    logging.info("Deleting %s in %s", name, zone)
    return (
        get_compute()
        .instances()
        .delete(project=project, zone=zone, instance=name)
        .execute()
    )


def wait_zone_operation(project, zone, operation):
    while True:
        result = (
            get_compute()
            .zoneOperations()
            .wait(
                project=project,
                operation=operation,
                zone=zone,
            )
            .execute()
        )
        status = result.get("status")
        logging.info("Zone %s operation %s returned status %s", zone, operation, status)
        if status == "DONE":
            if "error" in result:
                raise RuntimeError(
                    f"GCP operation {operation} in {zone} failed: {result['error']}"
                )
            return result


def create_instance_zones(prefix, zones, max_instances=None, ipv6_enabled=False):
    created_zones = []
    remaining_zones = list(zones)

    while remaining_zones and (
        max_instances is None or len(created_zones) < max_instances
    ):
        needed = len(remaining_zones)
        if max_instances is not None:
            needed = max_instances - len(created_zones)

        operations = []
        while remaining_zones and len(operations) < needed:
            zone = remaining_zones.pop(0)
            try:
                arguments = (settings.GCP_PROJECT, zone, f"{prefix}-{zone}")
                operation = (
                    create_instance(*arguments, ipv6_enabled=True)
                    if ipv6_enabled
                    else create_instance(*arguments)
                )["name"]
                operations.append((zone, operation))
            except Exception as err:
                logging.warning("Skipping zone %s: %s", zone, err)

        if not operations:
            break

        for zone, operation in operations:
            try:
                wait_zone_operation(settings.GCP_PROJECT, zone, operation)
                created_zones.append(zone)
                logging.info("Instance %s created", zone)
            except Exception as err:
                logging.warning("Skipping created zone %s: %s", zone, err)

        if max_instances is None:
            break

    if max_instances is not None and len(created_zones) < max_instances:
        logging.warning(
            "Created %d of %d requested GCP instances after trying %d zones",
            len(created_zones),
            max_instances,
            len(zones),
        )
    return created_zones


def create_instance_regions(
    prefix, zones, regions, max_instances=None, ipv6_enabled=False
):
    """Create at most one worker per region, trying alternate zones on failure."""
    selected_regions = list(regions)
    if max_instances is not None:
        selected_regions = selected_regions[:max_instances]

    zones_by_region = {region: [] for region in selected_regions}
    for zone in zones:
        region = zone.rsplit("-", 1)[0]
        if region in zones_by_region:
            zones_by_region[region].append(zone)

    missing = [
        region for region, candidates in zones_by_region.items() if not candidates
    ]
    if missing:
        raise ValueError(
            "no GCP zones found in requested regions: " + ", ".join(missing)
        )

    created_zones = []
    for region in selected_regions:
        for zone in zones_by_region[region]:
            try:
                arguments = (settings.GCP_PROJECT, zone, f"{prefix}-{zone}")
                operation = (
                    create_instance(*arguments, ipv6_enabled=True)
                    if ipv6_enabled
                    else create_instance(*arguments)
                )["name"]
                wait_zone_operation(settings.GCP_PROJECT, zone, operation)
                created_zones.append(zone)
                logging.info("Instance %s created for region %s", zone, region)
                break
            except Exception as err:
                logging.warning(
                    "Could not create worker in %s for region %s: %s",
                    zone,
                    region,
                    err,
                )
        else:
            logging.error("No worker could be created in GCP region %s", region)

    return created_zones


def collect_instances(prefix, zones, expected_count):
    deadline = time.monotonic() + float(
        os.environ.get("SCAMPER_GCP_INSTANCE_WAIT_SECONDS", "600")
    )
    while True:
        instances = []
        for zone in zones:
            instances.extend(find_ips(prefix, zone))
        if len(instances) == expected_count:
            return instances
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"found {len(instances)} GCP instances for {prefix}; expected {expected_count}"
            )
        logging.info(
            "Retry to fetch instance informations due to incorrect instance count"
        )
        time.sleep(10)


def get_instance_details(name, zone):
    return (
        get_compute()
        .instances()
        .get(project=settings.GCP_PROJECT, zone=zone, instance=name)
        .execute()
    )


def verify_standard_network_tier(instances):
    configured_tier = str(settings.GCP_NETWORK_TIER).upper()
    if configured_tier != "STANDARD":
        raise RuntimeError(
            f"GCP network tier must be STANDARD for this campaign, got {configured_tier}"
        )

    verified = {}
    for name, _nat_ip, zone in instances:
        details = get_instance_details(name, zone)
        tiers = [
            str(access_config.get("networkTier", "PREMIUM")).upper()
            for interface in details.get("networkInterfaces", [])
            for access_config in interface.get("accessConfigs", [])
        ]
        if not tiers:
            raise RuntimeError(
                f"GCP instance {name} in {zone} has no external access configuration"
            )
        unexpected = [tier for tier in tiers if tier != "STANDARD"]
        if unexpected:
            raise RuntimeError(
                f"GCP instance {name} in {zone} is not STANDARD network tier: {tiers}"
            )
        verified[name] = "STANDARD"
        logging.info("Verified STANDARD network tier for %s in %s", name, zone)
    return verified


def delete_instances(instances):
    logging.info("Deleting instances")
    operations = []
    deleted = set()
    for name, nat_ip, zone in instances:
        try:
            operation = delete_instance(settings.GCP_PROJECT, zone, name)
            if operation and "name" in operation:
                operations.append((name, zone, operation["name"]))
            else:
                deleted.add(name)
        except Exception as err:
            logging.exception(
                "Could not delete GCP instance %s in %s: %s", name, zone, err
            )

    for name, zone, operation in operations:
        try:
            wait_zone_operation(settings.GCP_PROJECT, zone, operation)
            deleted.add(name)
        except Exception as err:
            logging.exception(
                "Could not wait for GCP delete operation %s in %s: %s",
                operation,
                zone,
                err,
            )
    return deleted


def run_gcp_scamper(
    log_dir,
    prefix,
    max_instances=None,
    max_targets=None,
    max_trace6_targets=None,
    *,
    target_source=None,
    trace_target_source=None,
    rr_target_source=None,
    trace6_target_source=None,
    bucket_name=None,
    object_prefix=None,
    regions=None,
    trace_rate=100,
    rr_rate=10,
    trace6_rate=100,
    rr_timeout=2.0,
    measurements=("trace", "rr"),
    probe_payload=None,
    measurement_contact=None,
    do_not_probe_file=None,
    skip_smoke=False,
):
    if "trace6" in measurements and not trace6_target_source:
        raise ValueError("trace6_target_source is required when trace6 is enabled")
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    fh = logging.FileHandler(os.path.join(log_dir, f"{prefix}.log"))
    fh.setFormatter(formatter)
    fh.setLevel(logging.INFO)
    logger.addHandler(fh)

    bucket_name = bucket_name or settings.SCAMPER_RESULTS_BUCKET
    object_prefix = normalized_object_prefix(object_prefix or f"runs/{prefix}")
    campaign_started_at = datetime.now(timezone.utc).isoformat()
    campaign_complete = False
    campaign_failure = None
    target_sets = {}
    target_files = {}
    target_versions = {}
    do_not_probe_version = None
    do_not_probe_networks = ()
    manifest_nodes = []
    instances = []
    deleted_instance_names = set()
    created_zones = []
    logs = {}
    upload_logs = False
    try:
        if max_instances is None:
            max_instances = max_instances_from_env()
        if max_targets is None:
            max_targets = max_targets_from_env()
        if max_instances is not None:
            logging.info("Limiting GCP run to at most %d instances", max_instances)
        if max_targets is not None:
            logging.info("Limiting GCP run to at most %d targets", max_targets)

        trace_target_source = (
            trace_target_source or target_source or settings.SCAMPER_IP_DST
        )
        rr_target_source = rr_target_source or target_source or settings.SCAMPER_IP_DST
        if do_not_probe_file:
            do_not_probe_file = str(Path(do_not_probe_file).expanduser())
            do_not_probe_networks = load_do_not_probe_prefixes(do_not_probe_file)
            do_not_probe_version = (
                f"{Path(do_not_probe_file).name}@sha256:"
                f"{sha256_file(do_not_probe_file)}"
            )
            logging.info(
                "Loaded %d collapsed do-not-probe prefixes from %s",
                len(do_not_probe_networks),
                do_not_probe_file,
            )
        requested_targets = (
            ("trace", trace_target_source, 4, max_targets),
            ("rr", rr_target_source, 4, max_targets),
            ("trace6", trace6_target_source, 6, max_trace6_targets),
        )
        for measurement, source, address_family, measurement_max_targets in (
            item for item in requested_targets if item[1] is not None
        ):
            local_source = materialize_target_source(
                source,
                log_dir,
                prefix,
                label=measurement,
            )
            registered = load_registered_target(Path(local_source))
            if registered is None:
                version = (
                    f"{Path(local_source).name}@sha256:{sha256_file(local_source)}"
                )
            else:
                version = registered.source_version
                if registered.address_family != address_family:
                    raise ValueError(
                        f"{measurement} requires IPv{address_family} targets, but "
                        f"registry entry is IPv{registered.address_family}"
                    )

            if (
                registered is not None
                and measurement_max_targets is None
                and not do_not_probe_networks
            ):
                normalized_file = local_source
                normalized_sha256 = registered.normalized_sha256
                normalized_target_count = registered.target_count
                logging.info(
                    "Reusing registered %s target set %s with %d targets",
                    measurement,
                    registered.target_id,
                    registered.target_count,
                )
            else:
                normalized_file = build_target_file(
                    log_dir,
                    f"{prefix}-{measurement}",
                    max_targets=measurement_max_targets,
                    target_source=local_source,
                    do_not_probe_networks=do_not_probe_networks,
                    address_family=address_family,
                )
                normalized_sha256 = sha256_file(normalized_file)
                normalized_target_count = target_count(normalized_file)
            target_files[measurement] = normalized_file
            target_versions[measurement] = version
            target_sets[measurement] = {
                "source": source,
                "version": version,
                "normalized_file": normalized_file,
                "normalized_sha256": normalized_sha256,
                "target_count": normalized_target_count,
            }
        zones = zones_in_regions(get_zones(), regions)

        create_bucket(bucket_name)
        upload_logs = True
        if do_not_probe_file:
            send_to_cloud_storage(
                do_not_probe_file,
                bucket_name,
                f"{object_prefix}/do-not-probe.txt",
            )

        logging.info("Creating instances")
        if regions:
            if "trace6" in measurements:
                created_zones = create_instance_regions(
                    prefix,
                    zones,
                    regions,
                    max_instances=max_instances,
                    ipv6_enabled=True,
                )
            else:
                created_zones = create_instance_regions(
                    prefix, zones, regions, max_instances=max_instances
                )
        else:
            if "trace6" in measurements:
                created_zones = create_instance_zones(
                    prefix, zones, max_instances=max_instances, ipv6_enabled=True
                )
            else:
                created_zones = create_instance_zones(
                    prefix, zones, max_instances=max_instances
                )
        instance_count = len(created_zones)
        if instance_count == 0:
            raise RuntimeError("no GCP instances were created")

        logging.info("Fetching instance informations for %d instances", instance_count)
        instances = collect_instances(prefix, created_zones, instance_count)
        logging.info("Instances list: %s", instances)
        record_expense_instances(len(instances))

        for name, nat_ip, zone in instances:
            output_prefix = f"{name}-{nat_ip}"
            region = zone.rsplit("-", 1)[0]
            node_object_prefix = f"{object_prefix}/nodes/{region}/{name}"
            manifest_nodes.append(
                {
                    "node": name,
                    "region": region,
                    "zone": zone,
                    "public_ip": nat_ip,
                    "object_prefix": node_object_prefix,
                    "expected_objects": expected_campaign_artifacts(
                        node_object_prefix, output_prefix, measurements
                    ),
                    "status_object": (
                        f"{node_object_prefix}/{output_prefix}.status.json"
                    ),
                    "complete": False,
                }
            )
        manifest_nodes_by_name = {node["node"]: node for node in manifest_nodes}
        verified_network_tiers = verify_standard_network_tier(instances)
        for node in manifest_nodes:
            node["network_tier"] = verified_network_tiers[node["node"]]
            node["ipv6_network_tier"] = (
                "PREMIUM" if "trace6" in measurements else None
            )

        logging.info("Waiting until all instances are ready for ssh")
        wait_seconds = float(os.environ.get("SCAMPER_GCP_SSH_WAIT_SECONDS", "600"))
        for name, nat_ip, zone in instances:
            logs[name] = open(os.path.join(log_dir, f"{name}-{nat_ip}.log"), "w")
            deadline = time.monotonic() + wait_seconds
            nc = subprocess.Popen(
                ["nc", "-z", "-w", "1", nat_ip, "22"],
                stdout=logs[name],
                stderr=logs[name],
            )
            while nc.wait() != 0:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for ssh on {name} {nat_ip}")
                logging.info("Retrying nc for %s", name)
                nc = subprocess.Popen(
                    ["nc", "-z", "-w", "1", nat_ip, "22"],
                    stdout=logs[name],
                    stderr=logs[name],
                )
                time.sleep(1)
            logging.info("Instance %s is ready for ssh", name)

        processes = []
        logging.info("Scp necessary files to instances")
        for name, nat_ip, zone in instances:
            logging.info("Scp files to %s", name)
            processes.append(
                [
                    name,
                    nat_ip,
                    zone,
                    subprocess.Popen(
                        init_cmd(list(target_files.values()))
                        + [f"{settings.GCP_SCAMPER_USER}@{nat_ip}:~"],
                        stdout=logs[name],
                        stderr=logs[name],
                    ),
                ]
            )

        scp_timeout = float(os.environ.get("SCAMPER_GCP_SCP_WAIT_SECONDS", "900"))
        for name, nat_ip, zone, process in processes:
            while wait_for_process(process, f"scp to {name}", scp_timeout) != 0:
                logging.info("Retrying scp for %s", name)
                process = subprocess.Popen(
                    init_cmd(list(target_files.values()))
                    + [f"{settings.GCP_SCAMPER_USER}@{nat_ip}:~"],
                    stdout=logs[name],
                    stderr=logs[name],
                )

        logging.info("Scp Complete")

        processes = []
        logging.info("Start Scamper")
        for name, nat_ip, zone in instances:
            output_prefix = f"{name}-{nat_ip}"
            region = zone.rsplit("-", 1)[0]
            node_manifest = manifest_nodes_by_name[name]
            node_object_prefix = node_manifest["object_prefix"]
            node_artifacts = node_manifest["expected_objects"]
            trace6_options = {}
            if "trace6" in target_files:
                trace6_options = {
                    "trace6_target_file": target_files["trace6"],
                    "trace6_target_source": trace6_target_source,
                    "trace6_target_version": target_versions["trace6"],
                    "trace6_target_count": target_sets["trace6"]["target_count"],
                    "trace6_target_sha256": target_sets["trace6"]["normalized_sha256"],
                    "trace6_rate": trace6_rate,
                }
            cmd = remote_campaign_command(
                target_files["trace"],
                target_files["rr"],
                output_prefix,
                bucket_name,
                node_object_prefix,
                region=region,
                node=name,
                trace_target_source=trace_target_source,
                trace_target_version=target_versions["trace"],
                trace_target_count=target_sets["trace"]["target_count"],
                trace_target_sha256=target_sets["trace"]["normalized_sha256"],
                rr_target_source=rr_target_source,
                rr_target_version=target_versions["rr"],
                rr_target_count=target_sets["rr"]["target_count"],
                rr_target_sha256=target_sets["rr"]["normalized_sha256"],
                trace_rate=trace_rate,
                rr_rate=rr_rate,
                rr_timeout=rr_timeout,
                measurements=measurements,
                probe_payload=probe_payload,
                measurement_contact=measurement_contact,
                do_not_probe_version=do_not_probe_version,
                skip_smoke=skip_smoke,
                **trace6_options,
            )

            ssh_key = str(Path(settings.GCP_SCAMPER_SSH_KEY).expanduser())
            processes.append(
                (
                    subprocess.Popen(
                        [
                            "ssh",
                            "-i",
                            ssh_key,
                            "-oStrictHostKeyChecking=no",
                            "-oUserKnownHostsFile=/dev/null",
                            f"{settings.GCP_SCAMPER_USER}@{nat_ip}",
                            cmd,
                            "2>&1",
                        ],
                        stdout=logs[name],
                        stderr=logs[name],
                    ),
                    {
                        "name": name,
                        "instance": (name, nat_ip, zone),
                        "artifact_names": node_artifacts,
                    },
                )
            )
            logging.info("Instance %s started", name)

        def clean_up_terminal_node(info, complete):
            name = info["name"]
            manifest_nodes_by_name[name]["complete"] = complete
            deleted_instance_names.update(delete_instances([info["instance"]]))

        exits = wait_for_campaign_processes(
            processes,
            bucket_name,
            clean_up_terminal_node,
        )
        logging.info("Scamper script exit codes: %s", exits)
        campaign_complete = True
    except Exception as err:
        campaign_failure = f"{type(err).__name__}: {err}"
        logging.exception("GCP scamper flow failed; cleaning up instances")
        raise
    finally:
        close_instance_logs(logs)
        registered_nodes = {node["node"] for node in manifest_nodes}
        for zone in created_zones:
            name = f"{prefix}-{zone}"
            if name in registered_nodes:
                continue
            region = zone.rsplit("-", 1)[0]
            manifest_nodes.append(
                {
                    "node": name,
                    "region": region,
                    "zone": zone,
                    "public_ip": None,
                    "object_prefix": f"{object_prefix}/nodes/{region}/{name}",
                    "expected_objects": [],
                    "status_object": None,
                    "complete": False,
                }
            )
        cleanup_instances = instances or [
            (f"{prefix}-{zone}", "", zone) for zone in created_zones
        ]
        cleanup_instances = [
            instance
            for instance in cleanup_instances
            if instance[0] not in deleted_instance_names
        ]
        if cleanup_instances:
            delete_instances(cleanup_instances)
        fh.flush()
        logger.removeHandler(fh)
        fh.close()
        if upload_logs:
            try:
                manifest_path = write_run_manifest(
                    log_dir,
                    prefix=prefix,
                    bucket_name=bucket_name,
                    object_prefix=object_prefix,
                    target_sets=target_sets,
                    regions=regions,
                    measurements=measurements,
                    trace_rate=trace_rate,
                    rr_rate=rr_rate,
                    rr_timeout=rr_timeout,
                    probe_payload=probe_payload,
                    measurement_contact=measurement_contact,
                    do_not_probe_file=do_not_probe_file,
                    do_not_probe_version=do_not_probe_version,
                    nodes=manifest_nodes,
                    started_at=campaign_started_at,
                    complete=campaign_complete,
                    failure=campaign_failure,
                    trace6_rate=trace6_rate,
                )
                send_to_cloud_storage(
                    manifest_path,
                    bucket_name,
                    f"{object_prefix}/manifest.json",
                )
                package_and_upload_logs(log_dir, bucket_name, object_prefix)
            except Exception as err:
                logging.exception(
                    "Could not upload GCP flow logs to %s: %s", bucket_name, err
                )

    return bucket_name


def build_plan(
    prefix,
    log_dir,
    max_instances=None,
    max_targets=None,
    max_trace6_targets=None,
    *,
    target_source=None,
    trace_target_source=None,
    rr_target_source=None,
    trace6_target_source=None,
    bucket_name=None,
    object_prefix=None,
    regions=None,
    trace_rate=100,
    rr_rate=10,
    trace6_rate=100,
    rr_timeout=2.0,
    measurements=("trace", "rr"),
    probe_payload=None,
    measurement_contact=None,
    do_not_probe_file=None,
    skip_smoke=False,
):
    if max_instances is None:
        max_instances = max_instances_from_env()
    if max_targets is None:
        max_targets = max_targets_from_env()
    return {
        "provider": "gcp",
        "prefix": prefix,
        "bucket": bucket_name or settings.SCAMPER_RESULTS_BUCKET,
        "object_prefix": normalized_object_prefix(object_prefix or f"runs/{prefix}"),
        "log_dir": log_dir,
        "project": settings.GCP_PROJECT,
        "machine_type": settings.GCP_MACHINE_TYPE,
        "network_tier": settings.GCP_NETWORK_TIER,
        "target_sets": {
            "trace": {
                "source": trace_target_source
                or target_source
                or settings.SCAMPER_IP_DST,
            },
            "rr": {
                "source": rr_target_source or target_source or settings.SCAMPER_IP_DST,
            },
            **(
                {"trace6": {"source": trace6_target_source}}
                if trace6_target_source is not None
                else {}
            ),
        },
        "max_instances": max_instances,
        "max_targets": max_targets,
        "max_trace6_targets": max_trace6_targets,
        "regions": list(regions) if regions else "all-enabled-regions",
        "measurements": list(measurements),
        "trace_rate_pps": trace_rate,
        "rr_rate_pps": rr_rate,
        "trace6_rate_pps": trace6_rate,
        "rr_timeout_seconds": rr_timeout,
        "measurement_contact": measurement_contact,
        "probe_payload_text": probe_payload,
        "probe_payload_hex": probe_payload.encode("ascii").hex()
        if probe_payload
        else None,
        "do_not_probe_file": do_not_probe_file,
        "do_not_probe_enforcement": (
            "controller_target_filter" if do_not_probe_file else None
        ),
        "vm_script": settings.GCP_SCAMPER_SCRIPT,
        "campaign_runner": settings.SCAMPER_CAMPAIGN_RUNNER,
        "smoke_script": settings.SCAMPER_SMOKE_SCRIPT,
        "smoke_test": {
            "enabled": not skip_smoke,
            "default_target": "1.1.1.1",
            "min_hops": 2,
        },
        "commands": command_strings(
            trace_rate,
            rr_rate,
            rr_timeout,
            probe_payload,
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the legacy GCP all-zone scamper flow."
    )
    parser.add_argument("--prefix", help="run prefix used for VM, bucket, and logs")
    parser.add_argument("--log-dir", help="local log directory")
    parser.add_argument(
        "--max-instances",
        type=positive_int,
        help="stop creating GCP VMs after this many zones",
    )
    parser.add_argument(
        "--max-targets",
        type=positive_int,
        help="copy only the first N targets into a canary target file",
    )
    parser.add_argument("--max-trace6-targets", type=positive_int)
    parser.add_argument(
        "--target-source",
        default=settings.SCAMPER_IP_DST,
        help="legacy fallback source used for both measurements",
    )
    parser.add_argument(
        "--trace-target-source",
        help="local path, file:// URL, or HTTPS URL for traceroute targets",
    )
    parser.add_argument(
        "--rr-target-source",
        help="local path, file:// URL, or HTTPS URL for RR targets",
    )
    parser.add_argument("--trace6-target-source")
    parser.add_argument(
        "--bucket-name",
        help=f"GCS bucket for all runs (default: {settings.SCAMPER_RESULTS_BUCKET})",
    )
    parser.add_argument(
        "--object-prefix",
        type=normalized_object_prefix,
        help="object path for this run (default: runs/PREFIX)",
    )
    parser.add_argument(
        "--regions",
        type=csv_values,
        help="comma-separated GCP regions to use (default: every available zone)",
    )
    parser.add_argument(
        "--measurements",
        type=csv_values,
        default=("trace", "rr"),
        help="comma-separated measurements: trace,rr (default: trace,rr)",
    )
    parser.add_argument("--trace-rate", type=positive_int, default=100)
    parser.add_argument("--rr-rate", type=positive_int, default=10)
    parser.add_argument("--trace6-rate", type=positive_int, default=100)
    parser.add_argument("--rr-timeout", type=positive_float, default=2.0)
    parser.add_argument(
        "--probe-payload",
        type=probe_payload_text,
        help="ASCII notice embedded in ICMP probe payloads",
    )
    parser.add_argument(
        "--measurement-contact",
        help="monitored contact address recorded in campaign metadata",
    )
    parser.add_argument(
        "--do-not-probe-file",
        help="local file of IPv4 addresses/prefixes excluded before deployment",
    )
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="start the requested measurements without a preliminary smoke probe",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create VMs and run scamper; without this flag only print the plan",
    )
    args = parser.parse_args(argv)

    unsupported_measurements = set(args.measurements) - {"trace", "trace6", "rr"}
    if unsupported_measurements:
        parser.error(
            "unsupported measurements: " + ", ".join(sorted(unsupported_measurements))
        )
    if "trace6" in args.measurements and not args.trace6_target_source:
        parser.error("--trace6-target-source is required when trace6 is enabled")

    prefix = args.prefix or f"gcp-{int(time.time())}"
    log_dir = args.log_dir or f"{prefix}-logs"
    plan = build_plan(
        prefix,
        log_dir,
        max_instances=args.max_instances,
        max_targets=args.max_targets,
        max_trace6_targets=args.max_trace6_targets,
        target_source=args.target_source,
        trace_target_source=args.trace_target_source,
        rr_target_source=args.rr_target_source,
        trace6_target_source=args.trace6_target_source,
        bucket_name=args.bucket_name,
        object_prefix=args.object_prefix,
        regions=args.regions,
        trace_rate=args.trace_rate,
        rr_rate=args.rr_rate,
        trace6_rate=args.trace6_rate,
        rr_timeout=args.rr_timeout,
        measurements=args.measurements,
        probe_payload=args.probe_payload,
        measurement_contact=args.measurement_contact,
        do_not_probe_file=args.do_not_probe_file,
        skip_smoke=args.skip_smoke,
    )
    if not args.apply:
        print(json.dumps(plan, indent=2))
        return 0

    # Fail before provisioning if a file the workers need is absent here.
    assert_worker_assets("gcp")

    run_gcp_scamper(
        log_dir,
        prefix,
        max_instances=args.max_instances,
        max_targets=args.max_targets,
        max_trace6_targets=args.max_trace6_targets,
        target_source=args.target_source,
        trace_target_source=args.trace_target_source,
        rr_target_source=args.rr_target_source,
        trace6_target_source=args.trace6_target_source,
        bucket_name=args.bucket_name,
        object_prefix=args.object_prefix,
        regions=args.regions,
        trace_rate=args.trace_rate,
        rr_rate=args.rr_rate,
        trace6_rate=args.trace6_rate,
        rr_timeout=args.rr_timeout,
        measurements=args.measurements,
        probe_payload=args.probe_payload,
        measurement_contact=args.measurement_contact,
        do_not_probe_file=args.do_not_probe_file,
        skip_smoke=args.skip_smoke,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
