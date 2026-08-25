# Import the needed credential and management objects from the libraries.
import argparse
import hashlib
import json
import os
import shlex

from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path
from providers import settings
from providers.preflight import assert_worker_assets
from providers.gcs_credentials import (
    google_credentials,
    storage_client as gcs_storage_client,
)
from providers.common.targets import PreparedTargets, prepare_target_sets
import logging
import subprocess
import time
import tarfile
import shutil

PROJECT = settings.GCP_PROJECT
SERVICE_ACCOUNT = settings.GCP_SERVICE_ACCOUNT
gcp_credential = None


INIT_CMD = ["scp", "-i", settings.AZR_SCAMPER_SSH_KEY,
            "-oStrictHostKeyChecking=no",
            settings.WARTS_STORAGE_CREDENTIALS,
            settings.AZR_SCAMPER_VM_SCRIPT,
            settings.SCAMPER_SMOKE_SCRIPT,
            # The worker delegates to the shared runner, so it must be shipped.
            # Omitting it left the VM failing on "chmod: cannot access
            # ./run_campaign.py" after a successful smoke test.
            settings.SCAMPER_CAMPAIGN_RUNNER,
            settings.SCAMPER_UPLOAD_SCRIPT]
RESOURCE_GROUP_NAME = "azr-scamper"
PRIVATE_IP = "10.0.0.4"
credential = None
network_client = None
resource_client = None

PREFERRED_AZR_LOCATIONS = (
    "eastus",
    "eastus2",
    "centralus",
    "northcentralus",
    "southcentralus",
    "westus",
    "westus2",
    "westus3",
    "westcentralus",
    "canadacentral",
    "canadaeast",
    "northeurope",
    "westeurope",
    "uksouth",
    "ukwest",
    "francecentral",
    "germanywestcentral",
    "switzerlandnorth",
    "norwayeast",
    "swedencentral",
    "polandcentral",
    "italynorth",
    "japaneast",
    "japanwest",
    "eastasia",
    "southeastasia",
    "koreacentral",
    "koreasouth",
    "australiaeast",
    "australiasoutheast",
    "australiacentral",
    "brazilsouth",
    "centralindia",
    "southindia",
    "westindia",
    "uaenorth",
    "qatarcentral",
    "southafricanorth",
    "israelcentral",
    "mexicocentral",
)


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


def probe_payload_text(value):
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise argparse.ArgumentTypeError("probe payload must contain ASCII text") from error
    if not encoded:
        raise argparse.ArgumentTypeError("probe payload must not be empty")
    if len(encoded) > 128:
        raise argparse.ArgumentTypeError("probe payload must be at most 128 bytes")
    return value


def normalized_object_prefix(value):
    parsed = value.strip("/")
    if not parsed or any(part in {".", ".."} for part in parsed.split("/")):
        raise argparse.ArgumentTypeError("object prefix must be a non-empty GCS path")
    return parsed


def max_instances_from_env():
    raw = os.environ.get("SCAMPER_AZR_MAX_INSTANCES") or os.environ.get(
        "SCAMPER_LEGACY_MAX_INSTANCES"
    )
    if not raw:
        return None
    return positive_int(raw)


def max_targets_from_env():
    raw = os.environ.get("SCAMPER_AZR_MAX_TARGETS") or os.environ.get(
        "SCAMPER_LEGACY_MAX_TARGETS"
    )
    if not raw:
        return None
    return positive_int(raw)


def get_subscription_id(required=True):
    value = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    if required and not value:
        raise RuntimeError("AZURE_SUBSCRIPTION_ID is required for the Azure apply path")
    return value


def get_azure_credential():
    global credential
    if credential is None:
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()
    return credential


def get_network_client():
    global network_client
    if network_client is None:
        from azure.mgmt.network import NetworkManagementClient

        network_client = NetworkManagementClient(
            get_azure_credential(),
            get_subscription_id(),
        )
    return network_client


def get_resource_client():
    global resource_client
    if resource_client is None:
        from azure.mgmt.resource.resources import ResourceManagementClient

        resource_client = ResourceManagementClient(
            get_azure_credential(),
            get_subscription_id(),
        )
    return resource_client


def get_gcp_credentials():
    # Shared with every provider: explicit key if configured, else ADC.
    return google_credentials()

time_format = "%Y-%m-%d %H:%M:%S"
formatter = logging.Formatter(fmt='%(asctime)s - %(levelname)s - %(message)s', datefmt=time_format)
logger = logging.getLogger()
handler = logging.StreamHandler()
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)
for noisy_logger_name in (
    "azure",
    "azure.core.pipeline.policies.http_logging_policy",
    "msal",
):
    logging.getLogger(noisy_logger_name).setLevel(logging.WARNING)


def record_expense_instances(instance_count):
    try:
        from providers.expenses import record_provider_instances_from_env

        if record_provider_instances_from_env("azr", instance_count):
            logging.info("Recorded %d instances in the expense ledger", instance_count)
    except Exception as err:
        logging.warning("Could not update expense ledger: %s", err)


def read_azure_public_key():
    public_key_path = Path(f"{settings.AZR_SCAMPER_SSH_KEY}.pub").expanduser()
    return public_key_path.read_text(encoding="utf-8").strip()

def send_to_cloud_storage(file_name, bucket_name, object_name=None):
    attempt = 0
    blob = None
    success = False
    max_attempts = int(os.environ.get("SCAMPER_UPLOAD_MAX_ATTEMPTS", "5"))
    while not success and attempt < max_attempts:
        try:
            attempt += 1
            storage_client = gcs_storage_client()
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


def build_target_file(log_dir, prefix, max_targets=None):
    if max_targets is None:
        return settings.SCAMPER_IP_DST

    target_path = Path(log_dir) / f"{prefix}-targets-{max_targets}.txt"
    written = 0
    with open(settings.SCAMPER_IP_DST, "r", encoding="utf-8") as src:
        with target_path.open("w", encoding="utf-8") as dst:
            for line in src:
                if written >= max_targets:
                    break
                dst.write(line)
                written += 1
    logging.info("Created capped target file %s with %d targets", target_path, written)
    return str(target_path)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    locations,
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
):
    manifest_path = Path(log_dir) / "manifest.json"
    manifest = {
        "schema_version": 1,
        "run_id": prefix,
        "provider": "azr",
        "subscription_id": get_subscription_id(required=False) or "not set",
        "resource_group": prefix,
        "bucket": bucket_name,
        "object_prefix": object_prefix,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "complete": complete,
        "failure": failure,
        "target_sets": target_sets,
        "do_not_probe_file": do_not_probe_file,
        "do_not_probe_version": do_not_probe_version,
        "locations": list(locations),
        "measurements": list(measurements),
        "trace_rate_pps": trace_rate,
        "rr_rate_pps": rr_rate,
        "rr_timeout_seconds": rr_timeout,
        "probe_payload": probe_payload,
        "measurement_contact": measurement_contact,
        "commands": {
            "trace": f"scamper -c 'trace -m 20 -g 8 -w 3 -q 2 -P ICMP' -p {trace_rate} -f SHUFFLED_TARGETS -o OUTPUT.trace.warts -O warts",
            "rr": f"scamper -c 'ping -P icmp-echo -R -c 1 -W {rr_timeout:g}' -p {rr_rate} -f SHUFFLED_TARGETS -o OUTPUT.rr.warts -O warts",
        },
        "nodes": nodes,
        "failed_nodes": [node["node"] for node in nodes if not node["complete"]],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def init_cmd(target_files):
    if isinstance(target_files, (str, Path)):
        target_files = [str(target_files)]
    return [
        *INIT_CMD[:5],
        *target_files,
        *INIT_CMD[5:],
    ]


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


def remote_scamper_command(
    trace_target_file,
    rr_target_file,
    output_prefix,
    bucket_name,
    object_prefix,
    *,
    location,
    node,
    targets: PreparedTargets,
    trace_rate,
    rr_rate,
    rr_timeout,
    measurements,
    probe_payload=None,
    measurement_contact=None,
    skip_smoke=False,
):
    script = Path(settings.AZR_SCAMPER_VM_SCRIPT).name
    environment = {
        "SCAMPER_PROVIDER": "azure",
        "SCAMPER_REGION": location,
        "SCAMPER_NODE": node,
        "SCAMPER_TRACE_TARGET_SOURCE": targets.trace.source,
        "SCAMPER_TRACE_TARGET_VERSION": targets.trace.version,
        "SCAMPER_TRACE_TARGET_COUNT": str(targets.trace.target_count),
        "SCAMPER_TRACE_TARGET_SHA256": targets.trace.normalized_sha256,
        "SCAMPER_RR_TARGET_SOURCE": targets.rr.source,
        "SCAMPER_RR_TARGET_VERSION": targets.rr.version,
        "SCAMPER_RR_TARGET_COUNT": str(targets.rr.target_count),
        "SCAMPER_RR_TARGET_SHA256": targets.rr.normalized_sha256,
        "SCAMPER_TRACE_RATE_PPS": str(trace_rate),
        "SCAMPER_RR_RATE_PPS": str(rr_rate),
        "SCAMPER_RR_TIMEOUT_SECONDS": f"{rr_timeout:g}",
        "SCAMPER_MEASUREMENTS": ",".join(measurements),
    }
    if probe_payload:
        environment["SCAMPER_PROBE_PAYLOAD_TEXT"] = probe_payload
    if measurement_contact:
        environment["SCAMPER_MEASUREMENT_CONTACT"] = measurement_contact
    if targets.do_not_probe_version:
        environment["SCAMPER_DO_NOT_PROBE_VERSION"] = targets.do_not_probe_version
    if skip_smoke:
        environment["SCAMPER_SKIP_SMOKE"] = "1"
    assignments = " ".join(
        f"{name}={shlex.quote(value)}" for name, value in environment.items()
    )
    arguments = " ".join(
        shlex.quote(value)
        for value in (
            Path(trace_target_file).name,
            Path(rr_target_file).name,
            output_prefix,
            bucket_name,
            object_prefix,
        )
    )
    return (
        f"chmod +x {shlex.quote(script)}; {assignments} "
        f"sudo -E ./{shlex.quote(script)} {arguments}"
    )


def close_instance_logs(logs):
    logging.info("Closing logs")
    for location, log in logs.items():
        if not log.closed:
            logging.info("Closing log for %s", location)
            log.close()


def cleanup_resource_group(prefix):
    logging.info("Delete Resource Group")
    poller = delete_rg(prefix)
    wait = getattr(poller, "wait", None)
    if wait is not None:
        wait()
        return
    result = getattr(poller, "result", None)
    if result is not None:
        result()


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

def locations_from_env():
    """Restrict the campaign to an explicit location list.

    Without this the driver is all-location, so a canary cannot target a chosen
    region - capping instances to 1 just takes whichever location sorts first.
    Being able to name the region is what makes a single-region validation
    possible, e.g. re-running a region that previously produced no topology.
    """
    raw = os.environ.get("SCAMPER_AZR_LOCATIONS", "").strip()
    if not raw:
        return None
    requested = [value.strip() for value in raw.split(",") if value.strip()]
    if not requested:
        return None
    return requested


def get_locations():
    #Currently using the error msg's list of locations that support public IP creation
    # pip = set('westus,eastus,northeurope,westeurope,eastasia,southeastasia,northcentralus,southcentralus,centralus,eastus2,japaneast,japanwest,brazilsouth,australiaeast,australiasoutheast,centralindia,southindia,westindia,canadacentral,canadaeast,westcentralus,westus2,ukwest,uksouth,koreacentral,koreasouth,francecentral,australiacentral,southafricanorth,uaenorth,switzerlandnorth,germanywestcentral,norwayeast,westus3,jioindiawest,swedencentral,qatarcentral,polandcentral,italynorth,israelcentral,mexicocentral'.split(","))
    # perm = set('australiacentral,australiacentral2,australiaeast,australiasoutheast,brazilsouth,brazilsoutheast,canadacentral,canadaeast,centralindia,centralus,centraluseuap,eastasia,eastus,eastus2,eastus2euap,francecentral,francesouth,germanynorth,germanywestcentral,israelcentral,italynorth,japaneast,japanwest,koreacentral,koreasouth,malaysiasouth,mexicocentral,northcentralus,northeurope,norwayeast,norwaywest,polandcentral,qatarcentral,southafricanorth,southafricawest,southcentralus,southeastasia,southindia,spaincentral,swedencentral,swedensouth,switzerlandnorth,switzerlandwest,taiwannorth,taiwannorthwest,uaecentral,uaenorth,uksouth,ukwest,westcentralus,westeurope,westindia,westus,westus2,westus3,asia,asiapacific,australia,brazil,canada,devfabric,europe,global,india,japan,northwestus,uk,france,germany,switzerland,korea,norway,uae,southafrica,unitedstates,unitedstateseuap,westuspartner,singapore,sweden,italy,israel,newzealand,poland,qatar,austriaeast,chilecentral,eastusslv,indonesiacentral,israelnorthwest,malaysiawest,newzealandnorth'.split(","))
    # size = set('westindia')
    from azure.mgmt.subscription import SubscriptionClient

    client = SubscriptionClient(
        credential=get_azure_credential(),
    )

    response = client.subscriptions.list_locations(
        subscription_id = get_subscription_id(),
    )
    available_locations = [item.name for item in response]
    available = set(available_locations)
    preferred_locations = [
        location for location in PREFERRED_AZR_LOCATIONS if location in available
    ]
    preferred = set(preferred_locations)
    fallback_locations = [
        location for location in available_locations if location not in preferred
    ]
    requested = locations_from_env()
    if requested is not None:
        unknown = [name for name in requested if name not in available]
        if unknown:
            raise SystemExit(
                f"SCAMPER_AZR_LOCATIONS names locations unavailable in this "
                f"subscription: {unknown}"
            )
        logging.info("restricting to SCAMPER_AZR_LOCATIONS=%s", requested)
        return requested
    return preferred_locations + fallback_locations

def create_rg(rg_name):
    rg_result = get_resource_client().resource_groups.create_or_update(
        rg_name, {"location": "eastus"}
    )
    return rg_result

def delete_rg(rg_name):
    rg_result = get_resource_client().resource_groups.begin_delete(rg_name)
    return rg_result

def create_ip(rg_name,location,ip_name):
    from azure.mgmt.network.models import PublicIPAddress
    from azure.mgmt.network.models import PublicIPAddressPropertiesFormat
    from azure.mgmt.network.models import PublicIPAddressSku

    poller = get_network_client().public_ip_addresses.begin_create_or_update(
        rg_name,
        ip_name,
        PublicIPAddress(
            location=location,
            sku=PublicIPAddressSku(name="Standard"),
            properties=PublicIPAddressPropertiesFormat(
                public_ip_allocation_method="Static",
                public_ip_address_version="IPv4",
            ),
        ),
    )

    ip_address_result = poller.result()
    return ip_address_result

def create_vnet(rg_name,location,vnet_name):
    from azure.mgmt.network.models import AddressSpace
    from azure.mgmt.network.models import VirtualNetwork
    from azure.mgmt.network.models import VirtualNetworkPropertiesFormat

    poller = get_network_client().virtual_networks.begin_create_or_update(
        rg_name,
        vnet_name,
        VirtualNetwork(
            location=location,
            properties=VirtualNetworkPropertiesFormat(
                address_space=AddressSpace(address_prefixes=["10.0.0.0/24"]),
            ),
        ),
    )
    vnet_result = poller.result()
    return vnet_result

def create_subnet(rg_name,vnet_name, subnet_name):
    from azure.mgmt.network.models import Subnet
    from azure.mgmt.network.models import SubnetPropertiesFormat

    poller = get_network_client().subnets.begin_create_or_update(
        rg_name,
        vnet_name,
        subnet_name,
        Subnet(
            properties=SubnetPropertiesFormat(address_prefix="10.0.0.0/28"),
        ),
    )
    subnet_result = poller.result()
    return subnet_result

def create_nsg(rg_name,location,  nsg_name):
    from azure.mgmt.network.models import NetworkSecurityGroup
    from azure.mgmt.network.models import NetworkSecurityGroupPropertiesFormat
    from azure.mgmt.network.models import SecurityRule
    from azure.mgmt.network.models import SecurityRulePropertiesFormat

    security_rules = [
        SecurityRule(
            name="AllowICMP",
            properties=SecurityRulePropertiesFormat(
                source_address_prefix="*",
                source_port_range="*",
                destination_address_prefix="*",
                destination_port_range="*",
                protocol="Icmp",
                access="Allow",
                priority=100,
                direction="Inbound",
            ),
        ),
        SecurityRule(
            name="SSH",
            properties=SecurityRulePropertiesFormat(
                source_address_prefix="*",
                source_port_range="*",
                destination_address_prefix="*",
                destination_port_range="22",
                protocol="Tcp",
                access="Allow",
                priority=110,
                direction="Inbound",
            ),
        )
    ]
    poller = get_network_client().network_security_groups.begin_create_or_update(
        rg_name,
        nsg_name,
        parameters=NetworkSecurityGroup(
            location=location,
            properties=NetworkSecurityGroupPropertiesFormat(
                security_rules=security_rules,
            ),
        ))

    nsg_result = poller.result()
    return nsg_result

def create_network_interface( rg_name,location, ni_name, subnet_id, ip_id, nsg_id):
    from azure.mgmt.network.models import NetworkInterface
    from azure.mgmt.network.models import NetworkInterfaceIPConfiguration
    from azure.mgmt.network.models import NetworkInterfaceIPConfigurationPropertiesFormat
    from azure.mgmt.network.models import NetworkInterfacePropertiesFormat
    from azure.mgmt.network.models import NetworkSecurityGroup
    from azure.mgmt.network.models import PublicIPAddress
    from azure.mgmt.network.models import Subnet

    poller = get_network_client().network_interfaces.begin_create_or_update(
        rg_name,
        ni_name,
        NetworkInterface(
            location=location,
            properties=NetworkInterfacePropertiesFormat(
                ip_configurations=[
                    NetworkInterfaceIPConfiguration(
                        name=f"azr-scamper-{location}-ipconfig",
                        properties=NetworkInterfaceIPConfigurationPropertiesFormat(
                            subnet=Subnet(id=subnet_id),
                            public_ip_address=PublicIPAddress(id=ip_id),
                            private_ip_address=PRIVATE_IP,
                            private_ip_allocation_method="Static",
                        ),
                    )
                ],
                network_security_group=NetworkSecurityGroup(id=nsg_id),
            ),
        ),
    )
    nic_result = poller.result()
    return nic_result

def create_vm(rg_name,location,  vm_name, ni_id):
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.compute.models import HardwareProfile
    from azure.mgmt.compute.models import ImageReference
    from azure.mgmt.compute.models import LinuxConfiguration
    from azure.mgmt.compute.models import LinuxPatchSettings
    from azure.mgmt.compute.models import NetworkInterfaceReference
    from azure.mgmt.compute.models import NetworkProfile
    from azure.mgmt.compute.models import OSProfile
    from azure.mgmt.compute.models import SshConfiguration
    from azure.mgmt.compute.models import SshPublicKey
    from azure.mgmt.compute.models import StorageProfile
    from azure.mgmt.compute.models import VirtualMachine
    from azure.mgmt.compute.models import VirtualMachineProperties

    compute_client = ComputeManagementClient(
        get_azure_credential(),
        get_subscription_id(),
    )

    poller = compute_client.virtual_machines.begin_create_or_update(
        rg_name,
        vm_name,
        VirtualMachine(
            location=location,
            properties=VirtualMachineProperties(
                storage_profile=StorageProfile(
                    image_reference=ImageReference(
                        publisher=settings.AZR_IMAGE_PUBLISHER,
                        offer=settings.AZR_IMAGE_OFFER,
                        sku=settings.AZR_IMAGE_SKU,
                        version=settings.AZR_IMAGE_VERSION,
                    ),
                ),
                hardware_profile=HardwareProfile(vm_size=settings.AZR_VM_SIZE),
                os_profile=OSProfile(
                    computer_name=vm_name,
                    admin_username=settings.AZR_SCAMPER_USER,
                    linux_configuration=LinuxConfiguration(
                        disable_password_authentication=True,
                        ssh=SshConfiguration(
                            public_keys=[
                                SshPublicKey(
                                    path=f"/home/{settings.AZR_SCAMPER_USER}/.ssh/authorized_keys",
                                    key_data=read_azure_public_key(),
                                )
                            ],
                        ),
                        provision_vm_agent=True,
                        patch_settings=LinuxPatchSettings(
                            patch_mode="AutomaticByPlatform",
                            assessment_mode="ImageDefault",
                        ),
                    ),
                ),
                network_profile=NetworkProfile(
                    network_interfaces=[
                        NetworkInterfaceReference(id=ni_id),
                    ],
                ),
            ),
        ),
    )

    vm_result = poller.result()
    return vm_result

def launch_location(run_info):
    rg_name,location = run_info
    vm_name = f"azr-{location}"
    nsg_name = f"{vm_name}-nsg"
    ip_name = f"{vm_name}-ip"
    ni_name = f"{vm_name}-ni"
    vnet_name = f"{vm_name}-vnet"
    subnet_name  =f"{vm_name}-subnet"
    try:

        ip_result = create_ip(rg_name,location, ip_name)
        logging.info("Created %s", ip_name)

        create_vnet(rg_name,location, vnet_name)
        logging.info("Created %s", vnet_name)

        subnet_result = create_subnet(rg_name,vnet_name, subnet_name)
        logging.info("Created %s", subnet_name)

        nsg_result = create_nsg(rg_name,location, nsg_name)
        logging.info("Created %s", nsg_name)

        ni_result = create_network_interface(rg_name,location, ni_name, subnet_result.id, ip_result.id, nsg_result.id)
        logging.info("Created %s", ni_name)

        create_vm(rg_name,location,vm_name,ni_result.id)
        logging.info("Created %s", vm_name)
    except Exception:
        logging.exception("Fail to launch in %s", location)
        return None

    return (location, ip_result.ip_address)


def launch_locations(prefix, locations, max_instances=None):
    ips = []
    if max_instances is None:
        run_infos = [(prefix, location) for location in locations]
        if not run_infos:
            return ips
        with Pool(len(run_infos)) as p:
            launched = p.map(launch_location, run_infos)
        return [loc_ip for loc_ip in launched if loc_ip is not None]

    remaining_locations = list(locations)
    while remaining_locations and len(ips) < max_instances:
        remaining_count = max_instances - len(ips)
        batch_locations = remaining_locations[:remaining_count]
        remaining_locations = remaining_locations[remaining_count:]
        run_infos = [(prefix, location) for location in batch_locations]
        logging.info("Launching Azure locations:%s", batch_locations)
        with Pool(len(run_infos)) as p:
            launched = p.map(launch_location, run_infos)
        ips.extend(loc_ip for loc_ip in launched if loc_ip is not None)

    if len(ips) < max_instances:
        logging.warning(
            "Created %d of %d requested Azure instances after trying %d locations",
            len(ips),
            max_instances,
            len(locations),
        )
    return ips

def run_azr_scamper(
    log_dir,
    prefix,
    max_instances=None,
    max_targets=None,
    *,
    target_source=None,
    trace_target_source=None,
    rr_target_source=None,
    bucket_name=None,
    object_prefix=None,
    regions=None,
    trace_rate=100,
    rr_rate=10,
    rr_timeout=2.0,
    measurements=("trace", "rr"),
    probe_payload=None,
    measurement_contact=None,
    do_not_probe_file=None,
    skip_smoke=False,
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
    targets = None
    locations = []
    manifest_nodes = []
    logs = {}
    upload_logs = False
    resource_group_created = False
    try:
        if max_instances is None:
            max_instances = max_instances_from_env()
        if max_targets is None:
            max_targets = max_targets_from_env()
        if max_instances is not None:
            logging.info("Limiting Azure run to at most %d instances", max_instances)
        if max_targets is not None:
            logging.info("Limiting Azure run to at most %d targets", max_targets)

        target_source = target_source or settings.SCAMPER_IP_DST
        targets = prepare_target_sets(
            log_dir=log_dir,
            prefix=prefix,
            fallback_source=target_source,
            trace_target_source=trace_target_source,
            rr_target_source=rr_target_source,
            max_targets=max_targets,
            do_not_probe_file=do_not_probe_file,
        )
        target_files = [targets.trace.normalized_file, targets.rr.normalized_file]
        create_bucket(bucket_name)
        upload_logs = True
        if targets.do_not_probe_file:
            send_to_cloud_storage(
                targets.do_not_probe_file,
                bucket_name,
                f"{object_prefix}/do-not-probe.txt",
            )
        create_rg(prefix)
        resource_group_created = True

        locations = list(regions) if regions else get_locations()
        ips = launch_locations(prefix, locations, max_instances=max_instances)
        logging.info("Created following instances:%s", ips)
        record_expense_instances(len(ips))

        if not ips:
            raise RuntimeError("no Azure instances were created")

        for location, ip in ips:
            node = f"azr-{location}"
            output_prefix = f"{prefix}-{location}-{ip}"
            node_object_prefix = f"{object_prefix}/nodes/{location}/{node}"
            expected_objects = expected_campaign_artifacts(
                node_object_prefix, output_prefix, measurements
            )
            manifest_nodes.append(
                {
                    "node": node,
                    "location": location,
                    "public_ip": ip,
                    "object_prefix": node_object_prefix,
                    "expected_objects": expected_objects,
                    "status_object": f"{node_object_prefix}/{output_prefix}.status.json",
                    "complete": False,
                    "return_code": None,
                }
            )
        manifest_nodes_by_location = {
            node["location"]: node for node in manifest_nodes
        }

        wait_seconds = float(os.environ.get("SCAMPER_AZR_SSH_WAIT_SECONDS", "600"))
        for location, ip in ips:
            logs[location] = (open(os.path.join(log_dir, f"{prefix}-{location}-{ip}.log"), "w"))
            deadline = time.monotonic() + wait_seconds
            nc = subprocess.Popen(["nc", "-z", "-w", "1", ip, "22"],
                                  stdout=logs[location],
                                  stderr=logs[location])
            while nc.wait() != 0:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for ssh on {location} {ip}")
                logging.info("Retrying nc for %s", location)
                nc = subprocess.Popen(["nc", "-z", "-w", "1", ip, "22"],
                                      stdout=logs[location],
                                      stderr=logs[location])
                time.sleep(1)
            logging.info("Instance %s is ready for ssh", location)

        processes = []
        logging.info("Scp necessary files to instances")
        for location, ip in ips:
            logging.info("Scp files to %s", location)
            processes.append([location, ip, subprocess.Popen(init_cmd(target_files) + [f"{settings.AZR_SCAMPER_USER}@{ip}:~"],
                                                                   stdout=logs[location],
                                                                   stderr=logs[location])])

        scp_timeout = float(os.environ.get("SCAMPER_AZR_SCP_WAIT_SECONDS", "900"))
        for location, ip, process in processes:
            while wait_for_process(process, f"scp to {location}", scp_timeout) != 0:
                logging.info("Retrying scp for %s", location)
                process = subprocess.Popen(init_cmd(target_files) + [f"{settings.AZR_SCAMPER_USER}@{ip}:~"],
                                           stdout=logs[location],
                                           stderr=logs[location])

        logging.info("Scp Complete")

        processes = []
        logging.info("Start Scamper")
        for location, ip in ips:
            node_manifest = manifest_nodes_by_location[location]
            output_prefix = f"{prefix}-{location}-{ip}"
            cmd = remote_scamper_command(
                targets.trace.normalized_file,
                targets.rr.normalized_file,
                output_prefix,
                bucket_name,
                node_manifest["object_prefix"],
                location=location,
                node=node_manifest["node"],
                targets=targets,
                trace_rate=trace_rate,
                rr_rate=rr_rate,
                rr_timeout=rr_timeout,
                measurements=measurements,
                probe_payload=probe_payload,
                measurement_contact=measurement_contact,
                skip_smoke=skip_smoke,
            )

            processes.append(
                (
                    subprocess.Popen(
                        [
                            "ssh",
                            "-i",
                            settings.AZR_SCAMPER_SSH_KEY,
                            "-oStrictHostKeyChecking=no",
                            f"{settings.AZR_SCAMPER_USER}@{ip}",
                            cmd,
                            "2>&1",
                        ],
                        stdout=logs[location],
                        stderr=logs[location],
                    ),
                    node_manifest,
                )
            )
            logging.info("Instance %s started", location)
        exits = []
        for process, node_manifest in processes:
            exit_code = process.wait()
            exits.append(exit_code)
            node_manifest["return_code"] = exit_code
            node_manifest["complete"] = exit_code == 0
        logging.info("Scamper script exit codes: %s", exits)
        failed_exits = [exit_code for exit_code in exits if exit_code != 0]
        if failed_exits:
            raise RuntimeError(f"scamper failed on {len(failed_exits)} Azure instances")
        campaign_complete = True
    except Exception as err:
        campaign_failure = f"{type(err).__name__}: {err}"
        logging.exception("Azure scamper flow failed; cleaning up resource group")
        raise
    finally:
        close_instance_logs(logs)
        if resource_group_created:
            try:
                cleanup_resource_group(prefix)
            except Exception as err:
                logging.exception("Could not delete Azure resource group %s: %s", prefix, err)
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
                    target_sets=targets.as_manifest() if targets else {},
                    locations=locations,
                    measurements=measurements,
                    trace_rate=trace_rate,
                    rr_rate=rr_rate,
                    rr_timeout=rr_timeout,
                    probe_payload=probe_payload,
                    measurement_contact=measurement_contact,
                    do_not_probe_file=(targets.do_not_probe_file if targets else None),
                    do_not_probe_version=(targets.do_not_probe_version if targets else None),
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
                logging.exception("Could not upload Azure flow logs to %s: %s", bucket_name, err)

    return bucket_name

def build_plan(
    prefix,
    log_dir,
    max_instances=None,
    max_targets=None,
    *,
    target_source=None,
    trace_target_source=None,
    rr_target_source=None,
    bucket_name=None,
    object_prefix=None,
    regions=None,
    trace_rate=100,
    rr_rate=10,
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
        "provider": "azr",
        "prefix": prefix,
        "bucket": bucket_name or settings.SCAMPER_RESULTS_BUCKET,
        "object_prefix": normalized_object_prefix(object_prefix or f"runs/{prefix}"),
        "log_dir": log_dir,
        "resource_group": prefix,
        "target_sets": {
            "trace": trace_target_source or target_source or settings.SCAMPER_IP_DST,
            "rr": rr_target_source or target_source or settings.SCAMPER_IP_DST,
        },
        "do_not_probe_file": do_not_probe_file,
        "max_instances": max_instances,
        "max_targets": max_targets,
        "vm_script": settings.AZR_SCAMPER_VM_SCRIPT,
        "locations": list(regions) if regions else "all-available-locations",
        "measurements": list(measurements),
        "trace_rate_pps": trace_rate,
        "rr_rate_pps": rr_rate,
        "rr_timeout_seconds": rr_timeout,
        "probe_payload": probe_payload,
        "measurement_contact": measurement_contact,
        "skip_smoke": skip_smoke,
        "smoke_script": settings.SCAMPER_SMOKE_SCRIPT,
        "smoke_test": {"default_target": "8.8.8.8", "min_hops": 2},
        "subscription_id": get_subscription_id(required=False) or "not set",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the supported Azure regional scamper campaign."
    )
    parser.add_argument("--prefix", help="run prefix used for resource group and result names")
    parser.add_argument("--log-dir", help="local log directory")
    parser.add_argument(
        "--max-instances",
        type=positive_int,
        help="stop creating Azure VMs after this many locations",
    )
    parser.add_argument(
        "--max-targets",
        type=positive_int,
        help="copy only the first N targets into a canary target file",
    )
    parser.add_argument("--target-source", default=settings.SCAMPER_IP_DST)
    parser.add_argument("--trace-target-source")
    parser.add_argument("--rr-target-source")
    parser.add_argument(
        "--bucket-name",
        help=f"GCS bucket for all runs (default: {settings.SCAMPER_RESULTS_BUCKET})",
    )
    parser.add_argument(
        "--object-prefix",
        type=normalized_object_prefix,
        help="object path for this run (default: runs/PREFIX)",
    )
    parser.add_argument("--regions", type=csv_values)
    parser.add_argument("--measurements", type=csv_values, default=("trace", "rr"))
    parser.add_argument("--trace-rate", type=positive_int, default=100)
    parser.add_argument("--rr-rate", type=positive_int, default=10)
    parser.add_argument("--rr-timeout", type=positive_float, default=2.0)
    parser.add_argument("--probe-payload", type=probe_payload_text)
    parser.add_argument("--measurement-contact")
    parser.add_argument("--do-not-probe-file")
    parser.add_argument("--skip-smoke", action="store_true")
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

    prefix = args.prefix or f"azr-{int(time.time())}"
    log_dir = args.log_dir or f"{prefix}-logs"
    plan = build_plan(
        prefix,
        log_dir,
        max_instances=args.max_instances,
        max_targets=args.max_targets,
        target_source=args.target_source,
        trace_target_source=args.trace_target_source,
        rr_target_source=args.rr_target_source,
        bucket_name=args.bucket_name,
        object_prefix=args.object_prefix,
        regions=args.regions,
        trace_rate=args.trace_rate,
        rr_rate=args.rr_rate,
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
    assert_worker_assets("azr")

    run_azr_scamper(
        log_dir,
        prefix,
        max_instances=args.max_instances,
        max_targets=args.max_targets,
        target_source=args.target_source,
        trace_target_source=args.trace_target_source,
        rr_target_source=args.rr_target_source,
        bucket_name=args.bucket_name,
        object_prefix=args.object_prefix,
        regions=args.regions,
        trace_rate=args.trace_rate,
        rr_rate=args.rr_rate,
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
