"""Quick legacy test: spin up one Azure VM and run a capped probe set."""
import os, time, subprocess, logging
import settings
from azr import (credential, subscription_id, network_client, resource_client,
                 create_rg, delete_rg, create_ip, create_vnet, create_subnet,
                 create_nsg, create_network_interface, create_vm)
from azure.mgmt.compute import ComputeManagementClient
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

TEST_REGION   = "eastus"
RG_NAME       = "azr-test-scamper"
VM_NAME       = f"azr-test-{TEST_REGION}"
TEST_IP_FILE  = "./datasets/ipv4-test"
WARTS_OUT     = "test-output.warts"
BUCKET        = "azr-1773084090-warts"   # reuse existing bucket

INIT_CMD = ["scp", "-i", settings.AZR_SCAMPER_SSH_KEY,
            "-oStrictHostKeyChecking=no",
            settings.WARTS_STORAGE_CREDENTIALS,
            TEST_IP_FILE,
            settings.AZR_SCAMPER_VM_SCRIPT,
            settings.SCAMPER_UPLOAD_SCRIPT]

SCAMPER_CMD = "chmod +x {0}; sudo ./{0} {1} {2} {3}"

compute_client = ComputeManagementClient(credential, subscription_id)

logging.info("Creating resource group %s", RG_NAME)
create_rg(RG_NAME)

logging.info("Creating network resources in %s", TEST_REGION)
ip_result     = create_ip(RG_NAME, TEST_REGION, f"{VM_NAME}-ip")
vnet_result   = create_vnet(RG_NAME, TEST_REGION, f"{VM_NAME}-vnet")
subnet_result = create_subnet(RG_NAME, f"{VM_NAME}-vnet", f"{VM_NAME}-subnet")
nsg_result    = create_nsg(RG_NAME, TEST_REGION, f"{VM_NAME}-nsg")
ni_result     = create_network_interface(RG_NAME, TEST_REGION, f"{VM_NAME}-ni",
                                         subnet_result.id, ip_result.id, nsg_result.id)

logging.info("Creating VM %s", VM_NAME)
create_vm(RG_NAME, TEST_REGION, VM_NAME, ni_result.id)

ip = ip_result.ip_address
logging.info("VM IP: %s — waiting for SSH", ip)

import socket
for _ in range(60):
    try:
        s = socket.create_connection((ip, 22), timeout=2)
        s.close()
        break
    except Exception:
        time.sleep(5)
else:
    raise RuntimeError("SSH never became available")

logging.info("SSH ready. SCPing files...")
subprocess.run(INIT_CMD + [f"{settings.AZR_SCAMPER_USER}@{ip}:~"], check=True)

logging.info("Running scamper (UDP-Paris, 10 IPs)...")
cmd = SCAMPER_CMD.format(
    Path(settings.AZR_SCAMPER_VM_SCRIPT).name,
    Path(TEST_IP_FILE).name,
    WARTS_OUT,
    BUCKET
)
result = subprocess.run(
    ["ssh", "-i", settings.AZR_SCAMPER_SSH_KEY, "-oStrictHostKeyChecking=no",
     f"{settings.AZR_SCAMPER_USER}@{ip}", cmd],
    capture_output=True, text=True
)
logging.info("Exit code: %d", result.returncode)
logging.info("stdout:\n%s", result.stdout if result.stdout else "(empty)")
if result.stderr:
    logging.info("stderr:\n%s", result.stderr[-1000:])

logging.info("Deleting test resource group %s", RG_NAME)
delete_rg(RG_NAME)
logging.info("Done.")
