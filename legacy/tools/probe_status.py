"""
probe_status.py  –  Check scamper progress across all VMs for a running pipeline.

Usage:
    python probe_status.py [prefix]

    prefix  – GCP instance prefix, e.g. gcp-1772989332
              If omitted, auto-detects the most recent active run.
"""

import subprocess
import sys
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import settings
from googleapiclient import discovery
from google.oauth2 import service_account

# ── GCP client ────────────────────────────────────────────────────────────────
credentials = service_account.Credentials.from_service_account_file(
    settings.WARTS_STORAGE_CREDENTIALS)
compute = discovery.build('compute', 'v1', credentials=credentials)

TOTAL_TARGETS = sum(1 for _ in open(settings.SCAMPER_IP_DST))

SSH_CMD = [
    "ssh", "-i", settings.GCP_SCAMPER_SSH_KEY,
    "-oStrictHostKeyChecking=no",
    "-oUserKnownHostsFile=/dev/null",
    "-oConnectTimeout=5",
    "-oBatchMode=yes",
]

REMOTE_CMD = r"""
python3 - <<'EOF'
import os, glob, subprocess, sys, time

CACHE = os.path.expanduser('~/.warts_count')
CACHE_TTL = 90  # seconds

def scamper_elapsed():
    """Elapsed seconds for the running scamper process via /proc."""
    try:
        clk = os.sysconf('SC_CLK_TCK')
        uptime = float(open('/proc/uptime').read().split()[0])
        for cf in glob.glob('/proc/[0-9]*/cmdline'):
            try:
                cmd = open(cf, 'rb').read().replace(b'\x00', b' ').decode(errors='ignore')
                if 'scamper' in cmd and '-c' in cmd:
                    pid = cf.split('/')[2]
                    st = open(f'/proc/{pid}/stat').read().split()
                    return int(uptime - int(st[21]) / clk)
            except Exception:
                pass
    except Exception:
        pass
    return 0

ps_out = subprocess.run(['ps', 'aux'], capture_output=True, text=True).stdout
running = any('scamper -c' in l for l in ps_out.splitlines())

warts = glob.glob('*.warts')
n_traces = 0
elapsed_s = 0

if warts:
    w = warts[0]
    now = time.time()
    cache_mtime = os.path.getmtime(CACHE) if os.path.exists(CACHE) else 0
    cache_age = now - cache_mtime

    if cache_age < CACHE_TTL:
        try:
            parts = open(CACHE).read().strip().split('|')
            n_traces  = int(parts[0])
            elapsed_s = float(parts[1]) + cache_age  # adjust for time since cache was written
        except Exception:
            pass
    else:
        # Spawn background counter; it measures elapsed just before writing cache
        counter = (
            "import struct, os, glob, time\n"
            "count = 0\n"
            "f = open(" + repr(w) + ", 'rb', buffering=1048576)\n"
            "while True:\n"
            " h = f.read(8)\n"
            " if len(h) < 8: break\n"
            " import struct; mg, tp, ln = struct.unpack('>HHI', h)\n"
            " if mg != 0x1205: break\n"
            " if tp == 6: count += 1\n"
            " f.seek(ln, 1)\n"
            "try:\n"
            " clk = os.sysconf('SC_CLK_TCK')\n"
            " uptime = float(open('/proc/uptime').read().split()[0])\n"
            " el = 0\n"
            " for cf in glob.glob('/proc/[0-9]*/cmdline'):\n"
            "  try:\n"
            "   cmd = open(cf,'rb').read().replace(b'\\x00',b' ').decode(errors='ignore')\n"
            "   if 'scamper' in cmd and '-c' in cmd:\n"
            "    pid = cf.split('/')[2]\n"
            "    st = open(f'/proc/{pid}/stat').read().split()\n"
            "    el = int(uptime - int(st[21]) / clk); break\n"
            "  except: pass\n"
            "except: el = 0\n"
            "open(" + repr(CACHE) + ", 'w').write(f'{count}|{el}')\n"
        )
        subprocess.Popen([sys.executable, '-c', counter],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        # Return stale cache while counter runs
        try:
            parts = open(CACHE).read().strip().split('|')
            n_traces  = int(parts[0])
            elapsed_s = float(parts[1]) + cache_age
        except Exception:
            elapsed_s = scamper_elapsed()

if elapsed_s == 0:
    elapsed_s = scamper_elapsed()

print(f"{running}|{n_traces}|{elapsed_s}")
EOF
"""


def fmt_duration(seconds):
    seconds = int(seconds)
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def query_vm(name, ip):
    try:
        result = subprocess.run(
            SSH_CMD + [f"{settings.GCP_SCAMPER_USER}@{ip}", REMOTE_CMD],
            capture_output=True, text=True, timeout=15
        )
        line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        running_str, n_str, elapsed_str = line.split("|")
        running   = running_str == "True"
        n_traces  = int(n_str)
        elapsed_s = float(elapsed_str)
        return name, ip, running, n_traces, elapsed_s, None
    except Exception as e:
        return name, ip, None, None, None, str(e)


def get_instances(prefix):
    zones_resp = compute.zones().list(project=settings.GCP_PROJECT).execute()
    zones = [z['name'] for z in zones_resp['items']]
    instances = []
    for zone in zones:
        resp = compute.instances().list(project=settings.GCP_PROJECT, zone=zone).execute()
        for inst in resp.get('items', []):
            if prefix in inst['name'] and inst['status'] == 'RUNNING':
                ip = inst['networkInterfaces'][0]['accessConfigs'][0]['natIP']
                instances.append((inst['name'], ip))
    return instances


def probe_status(prefix=None):
    if prefix is None:
        log_dirs = sorted(
            [d for d in os.listdir('.') if re.match(r'gcp-\d+-logs', d)],
            reverse=True
        )
        if not log_dirs:
            print("No gcp-*-logs directories found. Pass a prefix explicitly.")
            return
        prefix = log_dirs[0].replace('-logs', '')
        print(f"Auto-detected prefix: {prefix}")

    print(f"Fetching instance list for {prefix}...")
    instances = get_instances(prefix)
    if not instances:
        print("No RUNNING instances found.")
        return

    print(f"Querying {len(instances)} VMs in parallel...\n")

    results = []
    with ThreadPoolExecutor(max_workers=32) as ex:
        futures = {ex.submit(query_vm, name, ip): (name, ip) for name, ip in instances}
        for fut in as_completed(futures):
            results.append(fut.result())

    results.sort(key=lambda r: r[0])

    ok      = [r for r in results if r[2] is not None]
    failed  = [r for r in results if r[2] is None]
    running = [r for r in ok if r[2]]
    done    = [r for r in ok if not r[2]]

    # ── per-VM table ──────────────────────────────────────────────────────────
    col = "{:<48} {:>10} {:>7} {:>10} {:>7}"
    print(col.format("VM", "Probed", "%", "Rate(/s)", "Status"))
    print("-" * 85)

    etr_seconds = []
    for name, ip, is_running, n_traces, elapsed_s, err in results:
        short = name.replace(prefix + "-", "")
        if err:
            print(col.format(short, "-", "-", "-", "ERR"))
            continue

        pct     = 100.0 * n_traces / TOTAL_TARGETS
        elapsed = max(elapsed_s, 1)
        rate    = n_traces / elapsed  # traces/sec

        if is_running and rate > 0:
            remaining = (TOTAL_TARGETS - n_traces) / rate
            etr_seconds.append(remaining)
            etr_str = fmt_duration(remaining)
        elif not is_running:
            etr_str = "DONE"
        else:
            etr_str = "?"

        status = f"ETR {etr_str}" if is_running else "DONE"
        print(col.format(
            short,
            f"{n_traces:,}",
            f"{pct:.1f}%",
            f"{rate:.1f}",
            status,
        ))

    # ── summary ───────────────────────────────────────────────────────────────
    if ok:
        traces_list = [r[3] for r in ok]
        pcts = [100.0 * n / TOTAL_TARGETS for n in traces_list]
        min_pct  = min(pcts)
        avg_pct  = sum(pcts) / len(pcts)
        max_pct  = max(pcts)
        # Pipeline finishes when the slowest VM finishes → ETR = max remaining
        overall_etr = fmt_duration(max(etr_seconds)) if etr_seconds else "—"

    print()
    print("=" * 85)
    print(f"  Total targets : {TOTAL_TARGETS:,}  ({settings.SCAMPER_IP_DST})")
    print(f"  VMs           : {len(instances)} total  |  {len(running)} running  |  {len(done)} done  |  {len(failed)} unreachable")
    if ok:
        print(f"  Progress      : min {min_pct:.1f}%  avg {avg_pct:.1f}%  max {max_pct:.1f}%")
        print(f"  Pipeline ETR  : {overall_etr}  (slowest VM sets the pace)")
    if failed:
        print(f"\n  Unreachable:")
        for name, ip, *_, err in failed:
            print(f"    {name.replace(prefix+'-','')} ({ip}): {err}")
    print("=" * 85)


if __name__ == "__main__":
    prefix = sys.argv[1] if len(sys.argv) > 1 else None
    probe_status(prefix)
