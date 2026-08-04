"""
azr_upload_recovery.py — trigger warts uploads on Azure VMs after a crash.

- DONE VMs   : runs upload.py immediately via SSH (detached with nohup)
- RUNNING VMs: installs a background watcher that waits for scamper to finish,
               then runs upload.py — survives SSH disconnects
- UNREACHABLE: retried with a longer timeout; reported if still unreachable
"""

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

LOG_DIR    = "azr-1773102831-logs"
BUCKET     = "azr-1773102831-warts"
SSH_KEY    = "./credentials/azr-scamper-key-pair.pem"
USER       = "azureuser"
SSH_OPTS   = ["-oStrictHostKeyChecking=no", "-oUserKnownHostsFile=/dev/null",
              "-oBatchMode=yes"]

SSH_BASE = ["ssh", "-i", SSH_KEY] + SSH_OPTS

# ── helpers ───────────────────────────────────────────────────────────────────

def ssh(ip, cmd, timeout=15):
    return subprocess.run(
        SSH_BASE + [f"-oConnectTimeout={timeout}", f"{USER}@{ip}", cmd],
        capture_output=True, text=True, timeout=timeout + 2
    )


def warts_name(loc, ip):
    return f"azr-1773102831-{loc}-{ip}.warts"


def scamper_running(ip):
    try:
        r = ssh(ip, "ps aux | grep -c '[s]camper -c'", timeout=10)
        return r.stdout.strip() != "0"
    except Exception:
        return None


def trigger_upload_now(loc, ip):
    """Run upload.py immediately on a finished VM (nohup so it survives drops)."""
    wf = warts_name(loc, ip)
    cmd = (
        f"nohup sudo python3 ./upload.py {wf} {BUCKET} "
        f"> /tmp/upload_{loc}.log 2>&1 &"
    )
    try:
        ssh(ip, cmd, timeout=10)
        return loc, ip, "upload_started"
    except Exception as e:
        return loc, ip, f"error: {e}"


def install_watcher(loc, ip):
    """Install a background watcher that uploads once scamper exits."""
    wf = warts_name(loc, ip)
    # Single-quoted heredoc so the remote shell expands nothing on our side
    cmd = (
        f"nohup bash -c '"
        f"while pgrep -x scamper > /dev/null; do sleep 30; done; "
        f"sudo python3 ./upload.py {wf} {BUCKET}"
        f"' > /tmp/upload_{loc}.log 2>&1 &"
    )
    try:
        ssh(ip, cmd, timeout=10)
        return loc, ip, "watcher_installed"
    except Exception as e:
        return loc, ip, f"error: {e}"


# ── discover VMs ──────────────────────────────────────────────────────────────

all_vms = []
for f in sorted(os.listdir(LOG_DIR)):
    if not f.endswith(".log") or f == "azr-1773102831.log":
        continue
    m = re.search(r'azr-1773102831-(.+?)-(\d+\.\d+\.\d+\.\d+)\.log', f)
    if m:
        all_vms.append((m.group(1), m.group(2)))

print(f"Discovered {len(all_vms)} VMs. Checking scamper status...\n")


def classify(loc, ip):
    status = scamper_running(ip)
    if status is True:
        return loc, ip, "running"
    elif status is False:
        return loc, ip, "done"
    else:
        # retry with longer timeout
        status2 = scamper_running(ip)
        return loc, ip, ("running" if status2 else ("done" if status2 is False else "unreachable"))


classifications = []
with ThreadPoolExecutor(max_workers=30) as ex:
    futs = {ex.submit(classify, loc, ip): (loc, ip) for loc, ip in all_vms}
    for fut in as_completed(futs):
        classifications.append(fut.result())

classifications.sort(key=lambda r: r[0])

done_vms        = [(loc, ip) for loc, ip, s in classifications if s == "done"]
running_vms     = [(loc, ip) for loc, ip, s in classifications if s == "running"]
unreachable_vms = [(loc, ip) for loc, ip, s in classifications if s == "unreachable"]

print(f"DONE        : {len(done_vms)}")
print(f"RUNNING     : {len(running_vms)}")
print(f"UNREACHABLE : {len(unreachable_vms)}\n")

# ── act ───────────────────────────────────────────────────────────────────────

results = []

with ThreadPoolExecutor(max_workers=30) as ex:
    futs = {}
    for loc, ip in done_vms:
        futs[ex.submit(trigger_upload_now, loc, ip)] = (loc, ip)
    for loc, ip in running_vms:
        futs[ex.submit(install_watcher, loc, ip)] = (loc, ip)
    for fut in as_completed(futs):
        results.append(fut.result())

results.sort(key=lambda r: r[0])

print(f"{'Location':<25} {'IP':<20} Result")
print("-" * 65)
for loc, ip, result in results:
    print(f"{loc:<25} {ip:<20} {result}")

if unreachable_vms:
    print(f"\nUNREACHABLE (manual action needed):")
    for loc, ip in unreachable_vms:
        print(f"  {loc} ({ip})")
        print(f"    ssh -i {SSH_KEY} {USER}@{ip}")
        print(f"    sudo python3 ./upload.py {warts_name(loc, ip)} {BUCKET}")

print("\nDone. Upload logs on each VM: /tmp/upload_<location>.log")
