import argparse
import hashlib
import ipaddress
import json
import shlex
import tarfile
import urllib.parse
import urllib.request
from providers import settings
import logging
import subprocess
import time
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

AMI_NAME = "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-"
KEY_NAME = "aws-scamper-key-pair"

PROJECT = settings.GCP_PROJECT

instance_types = ['t3.micro','t2.micro']

credentials = None


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
    raw = os.environ.get("SCAMPER_AWS_MAX_INSTANCES") or os.environ.get(
        "SCAMPER_LEGACY_MAX_INSTANCES"
    )
    if not raw:
        return None
    return positive_int(raw)


def max_targets_from_env():
    raw = os.environ.get("SCAMPER_AWS_MAX_TARGETS") or os.environ.get(
        "SCAMPER_LEGACY_MAX_TARGETS"
    )
    if not raw:
        return None
    return positive_int(raw)


def get_gcp_credentials():
    global credentials
    if credentials is None:
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            settings.WARTS_STORAGE_CREDENTIALS
        )
    return credentials


def ec2_client(region):
    import boto3

    return boto3.client("ec2", region_name=region)


def ec2_resource(region):
    import boto3

    return boto3.resource("ec2", region_name=region)


def client_error_type():
    import botocore.exceptions

    return botocore.exceptions.ClientError

# Logger
time_format = "%Y-%m-%d %H:%M:%S"
formatter = logging.Formatter(fmt='%(asctime)s - %(levelname)s - %(message)s', datefmt=time_format)
logger = logging.getLogger()
handler = logging.StreamHandler()
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)


def record_expense_instances(instance_count):
    try:
        from legacy.cli.expenses import record_provider_instances_from_env

        if record_provider_instances_from_env("aws", instance_count):
            logging.info("Recorded %d instances in the expense ledger", instance_count)
    except Exception as err:
        logging.warning("Could not update expense ledger: %s", err)


def create_bucket(name):
    from googleapiclient import discovery
    from googleapiclient.errors import HttpError

    gcp_storage = discovery.build('storage', 'v1', credentials=get_gcp_credentials())
    body = {
        "name": name,
        "storageClass": settings.GCP_STORAGE_CLASS,
        "location": settings.GCP_STORAGE_LOCATION,
        "locationType": "region"
    }
    try:
        return gcp_storage.buckets().insert(project=PROJECT, body=body).execute()
    except HttpError as err:
        if getattr(err.resp, "status", None) == 409:
            logging.info("Bucket %s already exists; reusing it", name)
            return None
        raise


def send_to_cloud_storage(file_name, bucket_name, object_name=None):
    attempt = 0
    blob = None
    success = False
    max_attempts = int(os.environ.get("SCAMPER_UPLOAD_MAX_ATTEMPTS", "5"))
    while not success and attempt < max_attempts:
        try:
            attempt += 1
            from google.cloud import storage

            storage_client = storage.Client.from_service_account_json(settings.WARTS_STORAGE_CREDENTIALS)
            bucket = storage_client.get_bucket(bucket_name)
            blob = bucket.blob(object_name or Path(file_name).name)
            logging.info("Uploading results to Cloud Storage (try #{}): {}".format(attempt, blob))
            blob.upload_from_filename(file_name)
            logging.info('Successfully uploaded ({} attempts) {}.'.format(attempt, blob))
            success = True
        except Exception as err:
            logging.info("Attempt {} failed to upload {} due to {}:{}".format(
                attempt, blob, Exception, err))
    if not success:
        raise RuntimeError(f"failed to upload {file_name} after {max_attempts} attempts")


def aws_timeout_seconds(name, default):
    raw = os.environ.get(name)
    if not raw:
        return float(default)
    try:
        parsed = float(raw)
    except ValueError:
        logging.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return float(default)
    if parsed < 0:
        logging.warning("Ignoring negative %s=%r; using %s", name, raw, default)
        return float(default)
    return parsed


def uploaded_artifact_sizes(bucket_name, artifact_names):
    from google.cloud import storage

    storage_client = storage.Client.from_service_account_json(settings.WARTS_STORAGE_CREDENTIALS)
    bucket = storage_client.bucket(bucket_name)
    sizes = {}
    for name in artifact_names:
        blob = bucket.get_blob(name)
        sizes[name] = int(blob.size or 0) if blob is not None else 0
    return sizes


def missing_uploaded_artifacts(bucket_name, artifact_names):
    sizes = uploaded_artifact_sizes(bucket_name, artifact_names)
    return [name for name in artifact_names if sizes.get(name, 0) <= 0]


def incomplete_uploaded_statuses(bucket_name, artifact_names):
    from google.cloud import storage

    status_names = [name for name in artifact_names if name.endswith(".status.json")]
    storage_client = storage.Client.from_service_account_json(
        settings.WARTS_STORAGE_CREDENTIALS
    )
    bucket = storage_client.bucket(bucket_name)
    incomplete = []
    for name in status_names:
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


def terminate_process(process, label, *, grace_seconds=5):
    if process.poll() is not None:
        return
    logging.warning("Terminating lingering local process for %s", label)
    process.terminate()
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        logging.warning("Killing lingering local process for %s", label)
        process.kill()
        process.wait(timeout=grace_seconds)


def wait_for_scp(process, info):
    timeout_seconds = aws_timeout_seconds("SCAMPER_AWS_SCP_TIMEOUT_SECONDS", 600)
    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as err:
        terminate_process(process, f"scp {info['name']}")
        raise TimeoutError(
            f"timed out after {timeout_seconds} seconds copying files to {info['name']}"
        ) from err


def wait_for_scamper_processes(processes, bucket_name, warts_list):
    timeout_seconds = aws_timeout_seconds("SCAMPER_AWS_SCAMPER_TIMEOUT_SECONDS", 14400)
    poll_seconds = aws_timeout_seconds("SCAMPER_AWS_ARTIFACT_POLL_SECONDS", 30)
    deadline = time.monotonic() + timeout_seconds
    last_missing = None

    while True:
        running = [(process, info, warts_name) for process, info, warts_name in processes if process.poll() is None]
        if not running:
            exits = [process.wait() for process, _info, _warts_name in processes]
            logging.info("Scamper script exit codes: %s", exits)
            missing = missing_uploaded_artifacts(bucket_name, warts_list)
            if missing:
                raise RuntimeError(
                    f"missing {len(missing)} expected AWS artifacts after scamper exit: {missing[:5]}"
                )
            incomplete = incomplete_uploaded_statuses(bucket_name, warts_list)
            if incomplete:
                raise RuntimeError(
                    f"incomplete AWS campaign status for {len(incomplete)} nodes: {incomplete[:5]}"
                )
            failed_exits = [exit_code for exit_code in exits if exit_code != 0]
            if failed_exits:
                logging.warning(
                    "Ignoring nonzero SSH exits because all expected artifacts are uploaded: %s",
                    failed_exits,
                )
            return exits

        missing = missing_uploaded_artifacts(bucket_name, warts_list)
        if not missing:
            incomplete = incomplete_uploaded_statuses(bucket_name, warts_list)
            if incomplete:
                for process, info, _warts_name in running:
                    terminate_process(process, f"ssh {info['name']}")
                raise RuntimeError(
                    f"incomplete AWS campaign status for {len(incomplete)} nodes: {incomplete[:5]}"
                )
            logging.info(
                "All %d expected AWS artifacts are uploaded; stopping %d lingering SSH sessions",
                len(warts_list),
                len(running),
            )
            for process, info, _warts_name in running:
                terminate_process(process, f"ssh {info['name']}")
            exits = [process.wait() for process, _info, _warts_name in processes]
            logging.info("Scamper script exit codes after artifact-verified stop: %s", exits)
            return exits

        missing_count = len(missing)
        if missing_count != last_missing:
            logging.info("Waiting for %d AWS artifacts to upload", missing_count)
            last_missing = missing_count

        if time.monotonic() >= deadline:
            for process, info, _warts_name in running:
                terminate_process(process, f"ssh {info['name']}")
            raise TimeoutError(
                f"timed out after {timeout_seconds} seconds waiting for AWS artifacts: {missing[:5]}"
            )

        time.sleep(poll_seconds)


def package_and_upload_logs(log_dir, bucket_name, object_prefix):
    if not os.path.isdir(log_dir):
        logging.info("Log directory %s does not exist; skipping log upload", log_dir)
        return

    logging.info("Zipping logs")
    tarname = Path(log_dir).with_name(f"{Path(log_dir).stem}.tar.gz")
    tar = tarfile.open(tarname, "w:gz")
    tar.add(log_dir, arcname=os.path.basename(log_dir))
    tar.close()

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


def materialize_target_source(target_source, log_dir, prefix):
    parsed = urllib.parse.urlparse(target_source)
    if parsed.scheme in {"http", "https"}:
        suffix = Path(parsed.path).suffix or ".txt"
        destination = Path(log_dir) / f"{prefix}-target-source{suffix}"
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


def build_target_file(log_dir, prefix, max_targets=None, target_source=None):
    source_path = target_source or settings.SCAMPER_IP_DST
    suffix = f"-{max_targets}" if max_targets is not None else ""
    target_path = Path(log_dir) / f"{prefix}-targets{suffix}.txt"
    written = 0
    seen = set()
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
                if address.version != 4:
                    raise ValueError(
                        f"non-IPv4 target on source line {line_number}: {value!r}"
                    )
                normalized = str(address)
                if normalized in seen:
                    raise ValueError(
                        f"duplicate target on source line {line_number}: {normalized}"
                    )
                seen.add(normalized)
                dst.write(normalized + "\n")
                written += 1
    if written == 0:
        raise ValueError(f"target source contained no IPv4 destinations: {source_path}")
    logging.info("Created normalized target file %s with %d targets", target_path, written)
    return str(target_path)


def init_cmd(target_file):
    return [
        "scp",
        "-i",
        settings.AWS_SCAMPER_SSH_KEY,
        "-oStrictHostKeyChecking=no",
        settings.WARTS_STORAGE_CREDENTIALS,
        target_file,
        settings.AWS_SCAMPER_VM_SCRIPT,
        settings.SCAMPER_SMOKE_SCRIPT,
        settings.SCAMPER_CAMPAIGN_RUNNER,
        settings.SCAMPER_UPLOAD_SCRIPT,
    ]


def remote_campaign_command(
    target_file,
    output_prefix,
    bucket_name,
    object_prefix,
    *,
    region,
    node,
    target_source,
    target_version,
    trace_rate,
    rr_rate,
    rr_timeout,
    measurements,
):
    environment = {
        "SCAMPER_PROVIDER": "aws",
        "SCAMPER_REGION": region,
        "SCAMPER_NODE": node,
        "SCAMPER_TARGET_SOURCE": target_source,
        "SCAMPER_TARGET_VERSION": target_version,
        "SCAMPER_TRACE_RATE_PPS": str(trace_rate),
        "SCAMPER_RR_RATE_PPS": str(rr_rate),
        "SCAMPER_RR_TIMEOUT_SECONDS": f"{rr_timeout:g}",
        "SCAMPER_MEASUREMENTS": ",".join(measurements),
    }
    assignments = " ".join(
        f"{name}={shlex.quote(value)}" for name, value in environment.items()
    )
    script = Path(settings.AWS_SCAMPER_VM_SCRIPT).name
    arguments = " ".join(
        shlex.quote(value)
        for value in (Path(target_file).name, output_prefix, bucket_name, object_prefix)
    )
    return f"chmod +x {shlex.quote(script)}; {assignments} ./{shlex.quote(script)} {arguments}"


def get_regions():
    client = ec2_client('us-west-2')
    regions = [region['RegionName'] for region in client.describe_regions()['Regions']]
    return regions


def create_instance(region, zone, sg_id, name):
    logging.info("Creating Instance in %s with security group %s", region, sg_id)
    ec2 = ec2_resource(region)
    client = ec2_client(region)
    images = client.describe_images(
        Owners=["099720109477"],  # Canonical
        Filters=[{"Name": "name", "Values": [AMI_NAME + "*"]},
                 {"Name": "architecture", "Values": ["x86_64"]},
                 {"Name": "state", "Values": ["available"]}]
    )["Images"]
    if not images:
        logging.info("No matching AMI found in %s", region)
        return None
    ami_id = sorted(images, key=lambda x: x["CreationDate"], reverse=True)[0]["ImageId"]
    client.describe_instance_types(Filters=[{"Name":"instance-type", "Values":["t2.micro","t3.micro"]}])
    instance = None

    for type in instance_types:
        try:
            instance = ec2.create_instances(
                ImageId=ami_id,
                MinCount=1,
                MaxCount=1,
                InstanceType=type,
                KeyName=KEY_NAME,
                TagSpecifications=[
                    {
                        'ResourceType': 'instance',
                        'Tags': [
                            {
                                'Key': 'Name',
                                "Value": name
                            },
                        ]
                    },
                ],
                SecurityGroupIds=[sg_id],
                Placement={'AvailabilityZone': zone}
            )[0]
            break
        except Exception as err:
            logging.info("Instance creation failed for %s %s due to %s %s",region,zone,Exception, err)
    if instance is None:
        logging.info("No instance was created in %s %s",region,zone)
    return instance


def get_ssh_ready_ip(instance):
    logging.info("Waiting until Instance %s is running", instance)
    instance.wait_until_running()
    logging.info("Reloading Instance %s", instance)
    instance.reload()
    ip = instance.public_ip_address
    logging.info("Checking ssh availability for ip %s", ip)
    wait_seconds = float(os.environ.get("SCAMPER_AWS_SSH_WAIT_SECONDS", "600"))
    deadline = time.monotonic() + wait_seconds
    nc = subprocess.Popen(["nc", "-z", "-w", "1", ip, "22"])
    while nc.wait() != 0:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for ssh on {ip} after {wait_seconds} seconds")
        logging.info("Retrying nc for %s", instance)
        nc = subprocess.Popen(["nc", "-z", "-w", "1", ip, "22"])
        time.sleep(1)
    return ip


def close_instance_logs(logs):
    logging.info("Closing logs")
    for name, log in logs.items():
        if not log.closed:
            logging.info("Closing log for %s", name)
            log.close()


def terminate_created_instances(instances):
    if not instances:
        return

    logging.info("Terminating instances")
    for instance, info in instances:
        try:
            logging.info("Terminating %s", info['name'])
            instance.terminate()
        except Exception as err:
            logging.warning("Could not terminate %s: %s", info.get("name", instance), err)


def get_or_create_default_vpc(region):
    client = ec2_client(region)
    vpcs = client.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if vpcs:
        return vpcs[0]["VpcId"]
    logging.info("No default VPC in %s, creating one", region)
    return client.create_default_vpc()["Vpc"]["VpcId"]


def create_default_security_group(region, sg_name):
    client = ec2_client(region)
    client_error = client_error_type()
    vpc_id = get_or_create_default_vpc(region)
    sg_id = None
    try:
        sgs = client.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [sg_name]},
                     {"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]
        if sgs:
            sg_id = sgs[0]["GroupId"]
            logging.info("Security Group %s already exists", sg_name)
        else:
            raise client_error({"Error": {"Code": "InvalidGroup.NotFound", "Message": ""}}, "DescribeSecurityGroups")
    except client_error:
        response = client.create_security_group(
            Description="security group used for scamper",
            GroupName=sg_name,
            VpcId=vpc_id,
        )
        sg_id = response["GroupId"]

    try:
        client.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [
                        {
                            "CidrIp": os.environ.get(
                                "SCAMPER_AWS_SSH_CIDR", "0.0.0.0/0"
                            )
                        }
                    ],
                }
            ],
        )
    except client_error:
        logging.info("Ingress Rule %s already exists", sg_name)

    return sg_id


def get_zones(region):
    client = ec2_client(region)
    return [zone["ZoneName"] for zone in client.describe_availability_zones()["AvailabilityZones"]]


def expected_campaign_artifacts(object_prefix, output_prefix, measurements):
    artifacts = [f"{object_prefix}/{output_prefix}.status.json"]
    for measurement in measurements:
        artifacts.extend(
            [
                f"{object_prefix}/{output_prefix}.{measurement}.warts",
                f"{object_prefix}/{output_prefix}.{measurement}.jsonl",
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
    target_source,
    target_version,
    normalized_target_file,
    regions,
    measurements,
    trace_rate,
    rr_rate,
    rr_timeout,
    nodes,
    started_at,
    complete,
    failure,
):
    manifest_path = Path(log_dir) / "manifest.json"
    manifest = {
        "schema_version": 1,
        "run_id": prefix,
        "provider": "aws",
        "bucket": bucket_name,
        "object_prefix": object_prefix,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "complete": complete,
        "failure": failure,
        "target_source": target_source,
        "target_version": target_version,
        "normalized_target_file": normalized_target_file,
        "normalized_target_sha256": (
            sha256_file(normalized_target_file) if normalized_target_file else None
        ),
        "target_count": target_count(normalized_target_file) if normalized_target_file else 0,
        "regions": list(regions) if regions else "all-enabled-regions",
        "measurements": list(measurements),
        "commands": {
            "trace": f"scamper -c 'trace -m 20 -g 8 -w 3 -q 2 -P ICMP' -p {trace_rate} -f SHUFFLED_TARGETS -o OUTPUT.trace.warts -O warts",
            "rr": f"scamper -c 'ping -P icmp-echo -R -c 1 -W {rr_timeout:g}' -p {rr_rate} -f SHUFFLED_TARGETS -o OUTPUT.rr.warts -O warts",
        },
        "nodes": nodes,
        "failed_nodes": [node["node"] for node in nodes if not node["complete"]],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def run_aws_scamper(
    log_dir,
    prefix,
    max_instances=None,
    max_targets=None,
    *,
    target_source=None,
    bucket_name=None,
    object_prefix=None,
    regions=None,
    trace_rate=100,
    rr_rate=10,
    rr_timeout=2.0,
    measurements=("trace", "rr"),
):
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
    manifest_nodes = []
    if max_instances is None:
        max_instances = max_instances_from_env()
    if max_targets is None:
        max_targets = max_targets_from_env()
    if max_instances is not None:
        logging.info("Limiting AWS run to at most %d instances", max_instances)
    if max_targets is not None:
        logging.info("Limiting AWS run to at most %d targets", max_targets)

    target_source = target_source or settings.SCAMPER_IP_DST
    local_source = materialize_target_source(target_source, log_dir, prefix)
    source_sha256 = sha256_file(local_source)
    target_version = f"{Path(local_source).name}@sha256:{source_sha256}"
    target_file = build_target_file(
        log_dir,
        prefix,
        max_targets=max_targets,
        target_source=local_source,
    )

    instances = []
    logs = {}
    upload_logs = False
    try:
        create_bucket(bucket_name)
        upload_logs = True

        selected_regions = list(regions) if regions else get_regions()
        for region in selected_regions:
            sg_name = f"{region}-scamper"
            try:
                sg_id = create_default_security_group(region, sg_name)
                zones = get_zones(region)
            except Exception as err:
                logging.exception("Skipping region %s due to setup failure: %s", region, err)
                continue

            for zone in zones:
                name = f"{prefix}-{zone}"

                info = {
                    'name': name,
                    'zone': zone,
                    'region': region,
                }
                try:
                    instance = create_instance(region, zone, sg_id, name)
                except Exception as err:
                    logging.exception("No instance was created in %s %s due to %s", region, zone, err)
                    continue
                if instance is not None:
                    instances.append([instance, info])
                    record_expense_instances(len(instances))
                if max_instances is not None and len(instances) >= max_instances:
                    logging.info("Reached AWS instance cap of %d", max_instances)
                    break
            if max_instances is not None and len(instances) >= max_instances:
                break
        record_expense_instances(len(instances))

        if not instances:
            raise RuntimeError("no AWS instances were created")

        for _instance, info in instances:
            node_object_prefix = (
                f"{object_prefix}/nodes/{info['region']}/{info['name']}"
            )
            manifest_nodes.append(
                {
                    "node": info["name"],
                    "region": info["region"],
                    "zone": info["zone"],
                    "public_ip": None,
                    "object_prefix": node_object_prefix,
                    "expected_objects": [],
                    "status_object": None,
                    "complete": False,
                }
            )
        manifest_nodes_by_name = {node["node"]: node for node in manifest_nodes}

        processes = []
        for instance, info in instances:
            ip = get_ssh_ready_ip(instance)
            info["ip"] = ip
            manifest_nodes_by_name[info["name"]]["public_ip"] = ip
            logs[info['name']] = (open(os.path.join(log_dir, f"{info['name']}-{ip}.log"), "w"))
            logging.info("Scp necessary files to instance %s", info['name'])
            processes.append([subprocess.Popen(init_cmd(target_file) + [f"{settings.AWS_SCAMPER_USER}@{ip}:~"],
                                               stdout=logs[info['name']],
                                               stderr=logs[info['name']]),
                              info])

        for process, info in processes:
            while wait_for_scp(process, info) != 0:
                logging.info("Retrying scp for %s", info['name'])
                process = subprocess.Popen(init_cmd(target_file) + [f"{settings.AWS_SCAMPER_USER}@{info['ip']}:~"],
                                           stdout=logs[info['name']],
                                           stderr=logs[info['name']])

        logging.info("Scp Complete")

        artifact_list = []
        processes = []
        logging.info("Start Scamper")
        for instance, info in instances:
            output_prefix = f"{info['name']}-{info['ip']}"
            node_manifest = manifest_nodes_by_name[info["name"]]
            node_object_prefix = node_manifest["object_prefix"]
            node_artifacts = expected_campaign_artifacts(
                node_object_prefix, output_prefix, measurements
            )
            artifact_list.extend(node_artifacts)
            node_manifest["expected_objects"] = node_artifacts
            node_manifest["status_object"] = (
                f"{node_object_prefix}/{output_prefix}.status.json"
            )
            cmd = remote_campaign_command(
                target_file,
                output_prefix,
                bucket_name,
                node_object_prefix,
                region=info["region"],
                node=info["name"],
                target_source=target_source,
                target_version=target_version,
                trace_rate=trace_rate,
                rr_rate=rr_rate,
                rr_timeout=rr_timeout,
                measurements=measurements,
            )

            processes.append((subprocess.Popen(["ssh", "-i", settings.AWS_SCAMPER_SSH_KEY, "-oStrictHostKeyChecking=no",
                                                f"ubuntu@{info['ip']}", cmd, "2>&1"],
                                               stdout=logs[info['name']],
                                               stderr=logs[info['name']]), info, output_prefix))

            logging.info("Instance %s started", info['name'])
        wait_for_scamper_processes(processes, bucket_name, artifact_list)
        campaign_complete = True
        for node in manifest_nodes:
            node["complete"] = True
    except Exception as err:
        campaign_failure = f"{type(err).__name__}: {err}"
        logging.exception("AWS scamper flow failed; cleaning up created instances")
        raise
    finally:
        close_instance_logs(logs)
        terminate_created_instances(instances)
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
                    target_source=target_source,
                    target_version=target_version,
                    normalized_target_file=target_file,
                    regions=regions,
                    measurements=measurements,
                    trace_rate=trace_rate,
                    rr_rate=rr_rate,
                    rr_timeout=rr_timeout,
                    nodes=manifest_nodes,
                    started_at=campaign_started_at,
                    complete=campaign_complete,
                    failure=campaign_failure,
                )
                send_to_cloud_storage(
                    manifest_path,
                    bucket_name,
                    f"{object_prefix}/manifest.json",
                )
                package_and_upload_logs(log_dir, bucket_name, object_prefix)
            except Exception as err:
                logging.exception("Could not upload AWS flow logs to %s: %s", bucket_name, err)

def build_plan(
    prefix,
    log_dir,
    max_instances=None,
    max_targets=None,
    *,
    target_source=None,
    bucket_name=None,
    object_prefix=None,
    regions=None,
    trace_rate=100,
    rr_rate=10,
    rr_timeout=2.0,
    measurements=("trace", "rr"),
):
    if max_instances is None:
        max_instances = max_instances_from_env()
    if max_targets is None:
        max_targets = max_targets_from_env()
    return {
        "provider": "aws",
        "prefix": prefix,
        "bucket": bucket_name or settings.SCAMPER_RESULTS_BUCKET,
        "object_prefix": normalized_object_prefix(object_prefix or f"runs/{prefix}"),
        "log_dir": log_dir,
        "target_source": target_source or settings.SCAMPER_IP_DST,
        "vm_script": settings.AWS_SCAMPER_VM_SCRIPT,
        "campaign_runner": settings.SCAMPER_CAMPAIGN_RUNNER,
        "smoke_script": settings.SCAMPER_SMOKE_SCRIPT,
        "smoke_test": {"default_target": "8.8.8.8", "min_hops": 2},
        "instance_types": instance_types,
        "max_instances": max_instances,
        "max_targets": max_targets,
        "regions": list(regions) if regions else "all-enabled-regions",
        "measurements": list(measurements),
        "trace_rate_pps": trace_rate,
        "rr_rate_pps": rr_rate,
        "rr_timeout_seconds": rr_timeout,
        "commands": {
            "trace": f"scamper -c 'trace -m 20 -g 8 -w 3 -q 2 -P ICMP' -p {trace_rate} -f SHUFFLED_TARGETS -o OUTPUT.trace.warts -O warts",
            "rr": f"scamper -c 'ping -P icmp-echo -R -c 1 -W {rr_timeout:g}' -p {rr_rate} -f SHUFFLED_TARGETS -o OUTPUT.rr.warts -O warts",
        },
        "remote_timeout_seconds": aws_timeout_seconds("SCAMPER_AWS_SCAMPER_TIMEOUT_SECONDS", 14400),
        "artifact_poll_seconds": aws_timeout_seconds("SCAMPER_AWS_ARTIFACT_POLL_SECONDS", 30),
        "ssh_ingress_cidr": os.environ.get("SCAMPER_AWS_SSH_CIDR", "0.0.0.0/0"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the legacy AWS all-region scamper flow."
    )
    parser.add_argument("--prefix", help="run prefix used for VMs, bucket, and logs")
    parser.add_argument("--log-dir", help="local log directory")
    parser.add_argument(
        "--max-instances",
        type=positive_int,
        help="stop creating AWS VMs after this many successful instance launches",
    )
    parser.add_argument(
        "--max-targets",
        type=positive_int,
        help="copy only the first N targets into a canary target file",
    )
    parser.add_argument(
        "--target-source",
        default=settings.SCAMPER_IP_DST,
        help="local path, file:// URL, or HTTPS URL for the complete target source",
    )
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
        help="comma-separated AWS regions to use (default: every enabled region)",
    )
    parser.add_argument(
        "--measurements",
        type=csv_values,
        default=("trace", "rr"),
        help="comma-separated measurements: trace,rr (default: trace,rr)",
    )
    parser.add_argument("--trace-rate", type=positive_int, default=100)
    parser.add_argument("--rr-rate", type=positive_int, default=10)
    parser.add_argument("--rr-timeout", type=positive_float, default=2.0)
    parser.add_argument(
        "--list-regions",
        action="store_true",
        help="list AWS regions visible to the configured boto3 profile",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create VMs and run scamper; without this flag only print the plan",
    )
    args = parser.parse_args(argv)

    unsupported_measurements = set(args.measurements) - {"trace", "rr"}
    if unsupported_measurements:
        parser.error(
            "unsupported measurements: " + ", ".join(sorted(unsupported_measurements))
        )

    if args.list_regions:
        print(json.dumps({"regions": get_regions()}, indent=2))
        return 0

    prefix = args.prefix or f"aws-{int(time.time())}"
    log_dir = args.log_dir or f"{prefix}-logs"
    plan = build_plan(
        prefix,
        log_dir,
        max_instances=args.max_instances,
        max_targets=args.max_targets,
        target_source=args.target_source,
        bucket_name=args.bucket_name,
        object_prefix=args.object_prefix,
        regions=args.regions,
        trace_rate=args.trace_rate,
        rr_rate=args.rr_rate,
        rr_timeout=args.rr_timeout,
        measurements=args.measurements,
    )
    if not args.apply:
        print(json.dumps(plan, indent=2))
        return 0

    run_aws_scamper(
        log_dir,
        prefix,
        max_instances=args.max_instances,
        max_targets=args.max_targets,
        target_source=args.target_source,
        bucket_name=args.bucket_name,
        object_prefix=args.object_prefix,
        regions=args.regions,
        trace_rate=args.trace_rate,
        rr_rate=args.rr_rate,
        rr_timeout=args.rr_timeout,
        measurements=args.measurements,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
