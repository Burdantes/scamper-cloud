"""
azr_cleanup.py — poll upload progress on each Azure VM, delete resources as
                 each upload completes. Runs until all VMs are cleaned up.

Usage: python3 azr_cleanup.py [--dry-run]
"""

import os
import re
import sys
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor

from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient

# ── config ────────────────────────────────────────────────────────────────────

LOG_DIR       = "azr-1773102831-logs"
RESOURCE_GROUP = "azr-1773102831"
SSH_KEY       = "./credentials/azr-scamper-key-pair.pem"
USER          = "azureuser"
POLL_INTERVAL = 60   # seconds between checks

DRY_RUN = "--dry-run" in sys.argv

SSH_OPTS = ["-oStrictHostKeyChecking=no", "-oUserKnownHostsFile=/dev/null",
            "-oBatchMode=yes", "-oConnectTimeout=8"]
SSH_BASE = ["ssh", "-i", SSH_KEY] + SSH_OPTS

# ── Azure clients ─────────────────────────────────────────────────────────────

credential      = DefaultAzureCredential()
subscription_id = os.environ["AZURE_SUBSCRIPTION_ID"]
compute_client  = ComputeManagementClient(credential, subscription_id)
network_client  = NetworkManagementClient(credential, subscription_id)

# ── helpers ───────────────────────────────────────────────────────────────────

def ssh_output(ip, cmd, timeout=12):
    r = subprocess.run(
        SSH_BASE + [f"{USER}@{ip}", cmd],
        capture_output=True, text=True, timeout=timeout
    )
    return r.stdout.strip()


def upload_status(ip, loc):
    """Returns 'success', 'running', 'failed', or 'unreachable'."""
    try:
        log = ssh_output(ip, f"cat /tmp/upload_{loc}.log 2>/dev/null")
        if "Successfully uploaded" in log:
            return "success", log
        if "failed" in log.lower() or "error" in log.lower():
            return "failed", log
        # watcher still waiting for scamper, or upload in progress
        return "running", log
    except Exception as e:
        return "unreachable", str(e)


def delete_vm_resources(loc):
    """Delete VM and all associated network resources for a location."""
    rg       = RESOURCE_GROUP
    vm_name  = f"azr-{loc}"
    ni_name  = f"{vm_name}-ni"
    ip_name  = f"{vm_name}-ip"
    nsg_name = f"{vm_name}-nsg"
    vnet_name = f"{vm_name}-vnet"

    if DRY_RUN:
        print(f"  [dry-run] would delete {vm_name} and associated resources")
        return

    try:
        print(f"  [{loc}] Deleting VM...")
        compute_client.virtual_machines.begin_delete(rg, vm_name).result()

        print(f"  [{loc}] Deleting NIC...")
        network_client.network_interfaces.begin_delete(rg, ni_name).result()

        print(f"  [{loc}] Deleting public IP + NSG...")
        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(lambda: network_client.public_ip_addresses.begin_delete(rg, ip_name).result())
            f2 = ex.submit(lambda: network_client.network_security_groups.begin_delete(rg, nsg_name).result())
            f1.result(); f2.result()

        print(f"  [{loc}] Deleting VNet...")
        network_client.virtual_networks.begin_delete(rg, vnet_name).result()

        print(f"  [{loc}] All resources deleted.")
    except Exception as e:
        print(f"  [{loc}] Deletion error: {e}")


def notify(msg):
    subprocess.run(["osascript", "-e",
                    f'display notification "{msg}" with title "Azure Cleanup" sound name "Glass"'],
                   capture_output=True)

# ── discover VMs ──────────────────────────────────────────────────────────────

all_vms = {}   # loc -> ip
for f in sorted(os.listdir(LOG_DIR)):
    if not f.endswith(".log") or f == "azr-1773102831.log":
        continue
    m = re.search(r'azr-1773102831-(.+?)-(\d+\.\d+\.\d+\.\d+)\.log', f)
    if m:
        all_vms[m.group(1)] = m.group(2)

pending   = dict(all_vms)   # loc -> ip, still needs cleanup
completed = []
failed    = []

print(f"Monitoring {len(pending)} VMs. Poll every {POLL_INTERVAL}s.\n")
if DRY_RUN:
    print("*** DRY RUN — no deletions will happen ***\n")

# ── main loop ─────────────────────────────────────────────────────────────────

while pending:
    print(f"[{time.strftime('%H:%M:%S')}] Checking {len(pending)} remaining VMs...")

    to_delete = []

    with ThreadPoolExecutor(max_workers=30) as ex:
        futures = {ex.submit(upload_status, ip, loc): loc
                   for loc, ip in pending.items()}
        for fut in futures:
            loc = futures[fut]
            try:
                status, log_tail = fut.result()
            except Exception as e:
                status, log_tail = "unreachable", str(e)

            if status == "success":
                print(f"  ✓ {loc}: upload complete — queuing deletion")
                to_delete.append(loc)
            elif status == "failed":
                print(f"  ✗ {loc}: upload FAILED — skipping deletion")
                print(f"    {log_tail[-200:]}")
                failed.append(loc)
                to_delete.append(loc)   # remove from pending, don't retry
            else:
                print(f"  … {loc}: {status}")

    # Delete completed VMs (sequentially to avoid Azure throttling)
    for loc in to_delete:
        ip = pending.pop(loc)
        if loc not in failed:
            delete_vm_resources(loc)
            completed.append(loc)
        notify(f"{loc} uploaded and deleted ({len(completed)}/{len(all_vms)} done)")

    if pending:
        print(f"\n  {len(completed)} done, {len(failed)} failed, {len(pending)} remaining. "
              f"Sleeping {POLL_INTERVAL}s...\n")
        time.sleep(POLL_INTERVAL)

# ── summary ───────────────────────────────────────────────────────────────────

print("\n" + "=" * 60)
print(f"All VMs processed.")
print(f"  Deleted  : {len(completed)}")
print(f"  Failed   : {len(failed)}")
if failed:
    print(f"  Failed locations: {', '.join(failed)}")
print("=" * 60)
notify(f"Azure cleanup complete. {len(completed)} deleted, {len(failed)} failed.")
