"""Read-only web dashboard for controller campaign state."""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

STATE_ROOT = Path("/var/lib/scamper-controller")
RELEASE_ROOT = Path("/opt/scamper-cloud/current")
PROVIDERS = ("aws", "azure", "gcp")
CREATE_PATTERNS = (
    re.compile(r"Creating Instance in ([a-z0-9-]+)"),
    re.compile(r"Creating [^ ]+ in ([a-z0-9-]+)"),
    re.compile(r"Created azr-([a-z0-9-]+)"),
)
MONTHLY_RUN_PATTERN = re.compile(r"monthly-(aws|azure|gcp)-(\d{8})")
READY_PATTERN = re.compile(r"Instance ([^ ]+) is ready for ssh")
STARTED_PATTERN = re.compile(r"Instance ([^ ]+) started")
# AWS logs "Waiting for N AWS artifacts to upload"; GCP logs "Waiting for N GCP
# campaign artifacts across M workers".
WAITING_PATTERN = re.compile(r"Waiting for ([0-9]+) [A-Z]+ (?:campaign )?artifacts")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def command_option(command: list[Any], name: str) -> str | None:
    try:
        value = command[command.index(name) + 1]
    except (ValueError, IndexError):
        return None
    return str(value)


def systemd_state(unit: str) -> dict[str, str]:
    properties = ("ActiveState", "SubState", "ActiveEnterTimestamp", "ExecMainStatus")
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, *(f"--property={item}" for item in properties)],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"active": "unknown", "sub": "unknown", "since": "", "exit_code": ""}
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return {
        "active": values.get("ActiveState", "unknown"),
        "sub": values.get("SubState", "unknown"),
        "since": values.get("ActiveEnterTimestamp", ""),
        "exit_code": values.get("ExecMainStatus", ""),
    }


def clean_log_lines(path: Path, limit: int = 40) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    cleaned = []
    for line in lines:
        message = line.split(" - ", 2)[-1] if " - " in line else line
        if message and not message.startswith(("$ ", "Starting scamper-campaign")):
            cleaned.append(message[:320])
    return cleaned[-limit:]


def milestones(lines: list[str]) -> dict[str, Any]:
    created: set[str] = set()
    ready: set[str] = set()
    started: set[str] = set()
    artifacts_remaining: int | None = None
    errors = []
    for line in lines:
        for pattern in CREATE_PATTERNS:
            match = pattern.search(line)
            if match:
                created.add(match.group(1))
                break
        if match := READY_PATTERN.search(line):
            ready.add(match.group(1))
        if match := STARTED_PATTERN.search(line):
            started.add(match.group(1))
        if match := WAITING_PATTERN.search(line):
            artifacts_remaining = int(match.group(1))
        if "ERROR" in line or "Traceback" in line or "failed" in line.lower():
            errors.append(line[:240])
    return {
        "created": len(created),
        "ready": len(ready),
        "started": len(started),
        "artifacts_remaining": artifacts_remaining,
        "errors": errors[-5:],
    }


def newest_cycle(root: Path = STATE_ROOT) -> str:
    cycles = []
    for path in (root / "jobs").glob("monthly-*-*/job.json"):
        if match := MONTHLY_RUN_PATTERN.fullmatch(path.parent.name):
            cycles.append(match.group(2))
    for path in (root / "monthly").glob("*.json"):
        if re.fullmatch(r"\d{8}", path.stem):
            cycles.append(path.stem)
    return max(cycles, default=datetime.now(timezone.utc).strftime("%Y%m%d"))


def submitted_runs(root: Path = STATE_ROOT) -> list[str]:
    """Every run ID that has a job record, regardless of naming convention."""
    return sorted({path.parent.name for path in (root / "jobs").glob("*/job.json")})


def run_provider(run_id: str, job: dict[str, Any]) -> str:
    provider = job.get("provider")
    if provider in PROVIDERS:
        return str(provider)
    for name in PROVIDERS:
        if re.search(rf"(?:^|[-_]){name}(?:[-_]|$)", run_id):
            return name
    return "unknown"


def run_cycle(run_id: str, job: dict[str, Any]) -> str:
    match = MONTHLY_RUN_PATTERN.fullmatch(run_id)
    if match:
        return match.group(2)
    submitted = str(job.get("submitted_at") or "")[:10]
    return submitted.replace("-", "") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", submitted) else ""


def recorded_result(monthly: dict[str, Any], run_id: str, provider: str) -> dict[str, Any]:
    results = [item for item in monthly.get("results", []) if isinstance(item, dict)]
    for item in results:
        if item.get("run_id") == run_id:
            return item
    for item in results:
        if item.get("provider") == provider and not item.get("run_id"):
            return item
    return {}


def run_status(run_id: str, root: Path, readiness: dict[str, Any], result: dict[str, Any],
               scope_cycle: str | None = None) -> dict[str, Any]:
    job_dir = root / "jobs" / run_id
    job = read_json(job_dir / "job.json")
    provider = run_provider(run_id, job)
    cycle = run_cycle(run_id, job)
    service = systemd_state(f"scamper-campaign-{run_id}.service")
    log_path = job_dir / "logs" / f"{run_id}.log"
    lines = clean_log_lines(log_path, 500)
    summary = read_json(job_dir / "summary.json")
    if scope_cycle is not None and cycle != scope_cycle:
        readiness, result = {}, {}
    blocked = result.get("blocked", {}).get(provider) or readiness.get("providers", {}).get(provider, {}).get("errors", [])
    monthly = read_json(root / "monthly" / f"{cycle}.json") if cycle else {}
    recorded = recorded_result(monthly, run_id, provider)
    recorded_status = recorded.get("status") or summary.get("status")
    exit_code = str(service.get("exit_code") or "")
    ran = bool(service.get("since"))
    if service["active"] in {"active", "activating"}:
        state = "running"
    elif recorded_status in {"completed", "complete", "success"}:
        state = "complete"
    elif recorded_status == "failed" or (job and service["active"] == "failed"):
        state = "failed"
    elif blocked:
        state = "blocked"
    elif job and ran and exit_code not in {"", "0"}:
        state = "failed"
    elif job and ran and exit_code == "0":
        state = "complete"
    elif job:
        state = "submitted"
    else:
        state = "queued"
    command = job.get("campaign_command", [])
    regions = (command_option(command, "--regions") or "").split(",") if command else []
    measurements = (command_option(command, "--measurements") or "").split(",") if command else []
    return {
        "provider": provider,
        "run_id": run_id,
        "cycle": cycle,
        "state": state,
        "service": service,
        "submitted_at": job.get("submitted_at"),
        "regions": len([item for item in regions if item]),
        "measurements": [item for item in measurements if item],
        "milestones": milestones(lines),
        "blocked_reasons": blocked[:6],
        "blocked_count": len(blocked),
        "exit_code": recorded.get("exit_code") or service.get("exit_code"),
        "failure_reason": summary.get("failure_reason") or recorded.get("failure_reason"),
        "recent": lines[-12:],
    }


def provider_status(provider: str, cycle: str, root: Path, readiness: dict[str, Any],
                    result: dict[str, Any]) -> dict[str, Any]:
    return run_status(f"monthly-{provider}-{cycle}", root, readiness, result)


def dashboard_state(cycle: str | None = None, root: Path = STATE_ROOT,
                    release_root: Path = RELEASE_ROOT) -> dict[str, Any]:
    selected = cycle or newest_cycle(root)
    readiness = read_json(root / "september15" / "readiness.json") if selected == "20260915" else {}
    result = read_json(root / "september15" / "result.json") if selected == "20260915" else {}
    release = read_json(release_root / "release.json")
    known = submitted_runs(root)
    # The in-flight monthly cycle always lists its three clouds so queued and
    # preflight-blocked providers stay visible before a job record exists. Older
    # cycles list only the runs that were really submitted.
    placeholders = [f"monthly-{name}-{selected}" for name in PROVIDERS] if selected == newest_cycle(root) else []
    identifiers = list(dict.fromkeys(known + placeholders))
    runs = [run_status(item, root, readiness, result, selected) for item in identifiers]
    if cycle:
        runs = [item for item in runs if item["cycle"] == cycle]
    runs.sort(key=lambda item: item["run_id"])
    runs.sort(key=lambda item: (item["cycle"] or "", item["submitted_at"] or ""), reverse=True)
    current = [item for item in runs if item["cycle"] == selected] or runs
    active = next((item["run_id"] for item in runs if item["state"] == "running"), None)
    active_provider = next((item["provider"] for item in runs if item["state"] == "running"), None)
    targets = {
        name: value.get("target_count")
        for name, value in readiness.get("targets", {}).items()
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cycle": selected,
        "cycle_filter": cycle,
        "run_count": len(runs),
        "overall": "running" if active else ("complete" if current and all(item["state"] == "complete" for item in current) else "attention"),
        "active_run": active,
        "active_provider": active_provider,
        "orchestrator": systemd_state("scamper-once-20260915.service") if selected == "20260915" else {},
        "runs": runs,
        "targets": targets,
        "release": release,
    }


INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scamper run status</title><style>
*{box-sizing:border-box}body{margin:0;background:#fff;color:#222;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}.page{max-width:1100px;margin:0 auto;padding:32px 20px 48px}header{border-bottom:1px solid #ddd;padding-bottom:16px;margin-bottom:24px}h1{font-size:24px;margin:0 0 6px}.muted,.meta{color:#666}.meta{font-size:13px}table{width:100%;border-collapse:collapse}th,td{text-align:left;vertical-align:top;padding:11px 10px;border-bottom:1px solid #ddd}th{font-size:12px;color:#555;background:#f7f7f7}tbody tr{cursor:pointer}tbody tr.selected{background:#eef3fb}.run{font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;word-break:break-all}.cloud{font-weight:600}.state{font-weight:600}.running,.complete{color:#176b3a}.failed,.blocked{color:#a32626}.queued,.submitted{color:#745600}.details{min-width:240px}.events{margin-top:30px}.events h2{font-size:16px;margin:0 0 10px}.log{height:260px;overflow:auto;white-space:pre-wrap;background:#f7f7f7;border:1px solid #ddd;padding:12px;font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace}.footer{display:flex;justify-content:space-between;gap:16px;margin-top:12px;color:#777;font-size:12px}.error{display:none;background:#fff3f3;color:#8a1f1f;padding:10px;margin-bottom:16px;border:1px solid #e5bcbc}@media(max-width:720px){.page{padding:20px 12px}table{display:block;overflow-x:auto}.footer{display:block}.footer span{display:block;margin-top:4px}}
</style></head><body><main class="page"><header><h1>Scamper run status</h1><div id="summary" class="meta">Loading…</div></header><div id="error" class="error"></div><table><thead><tr><th>Run</th><th>Cloud</th><th>Cycle</th><th>Status</th><th>Regions</th><th>Created</th><th>Ready</th><th>Artifacts left</th><th>Details</th></tr></thead><tbody id="runs"></tbody></table><section class="events"><h2>Recent events — <span id="event-run">none</span></h2><div class="log" id="log"></div></section><div class="footer"><span id="release">Release —</span><span id="updated">Updated —</span></div></main><script>
const esc=s=>String(s??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=n=>n==null?'—':Number(n).toLocaleString();
let latest=null,selected=null;
function detailOf(p){return p.failure_reason||(p.blocked_count?`${p.blocked_count} preflight findings. ${p.blocked_reasons[0]}`:(p.state==='failed'?`Exited with code ${p.exit_code}`:'—'));}
function shown(d){const runs=d.runs||[];return runs.find(p=>p.run_id===selected)||runs.find(p=>p.state==='running')||runs.find(p=>p.recent?.length)||runs[0];}
function draw(d){latest=d;const runs=d.runs||[];const total=Object.values(d.targets||{}).reduce((a,n)=>a+(n||0),0);const scope=d.cycle_filter?`cycle ${d.cycle_filter}`:'all runs';document.querySelector('#summary').textContent=`${num(d.run_count)} runs (${scope}) · newest cycle ${d.cycle} · ${d.overall} · active run: ${d.active_run||'none'} · ${num(total)} targets per cloud · refreshes every 15 seconds`;const current=shown(d);document.querySelector('#runs').innerHTML=runs.map(p=>`<tr data-run="${esc(p.run_id)}" class="${p.run_id===current?.run_id?'selected':''}"><td class="run">${esc(p.run_id)}</td><td class="cloud">${esc(p.provider.toUpperCase())}</td><td>${esc(p.cycle||'—')}</td><td class="state ${esc(p.state)}">${esc(p.state)}</td><td>${num(p.regions)}</td><td>${num(p.milestones?.created)}</td><td>${num(p.milestones?.ready)}</td><td>${num(p.milestones?.artifacts_remaining)}</td><td class="details">${esc(detailOf(p))}</td></tr>`).join('');document.querySelector('#event-run').textContent=current?.run_id||'none';document.querySelector('#log').textContent=(current?.recent||[]).join('\\n')||'No recent events.';document.querySelector('#release').textContent=`Release ${d.release?.release||'unknown'} · ${(d.release?.bundle_sha256||d.release?.sha256||'').slice(0,12)}`;document.querySelector('#updated').textContent=`Updated ${new Date(d.generated_at).toLocaleString()}`;}
document.querySelector('#runs').addEventListener('click',e=>{const row=e.target.closest('tr[data-run]');if(!row||!latest)return;selected=row.dataset.run;draw(latest);});
async function refresh(){try{const cycle=new URLSearchParams(location.search).get('cycle')||'';const r=await fetch('/api/status?cycle='+encodeURIComponent(cycle),{cache:'no-store'});if(!r.ok)throw Error('status '+r.status);draw(await r.json());document.querySelector('#error').style.display='none'}catch(e){const n=document.querySelector('#error');n.textContent='Dashboard refresh failed: '+e.message;n.style.display='block'}}
refresh();setInterval(refresh,15000);
</script></body></html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.respond(INDEX.encode(), "text/html; charset=utf-8")
            return
        if parsed.path == "/healthz":
            self.respond(b"ok\n", "text/plain; charset=utf-8")
            return
        if parsed.path == "/api/status":
            cycle = parse_qs(parsed.query).get("cycle", [None])[0] or None
            if cycle is not None and not re.fullmatch(r"\d{8}", cycle):
                self.respond(b'{"error":"invalid cycle"}', "application/json", HTTPStatus.BAD_REQUEST)
                return
            payload = json.dumps(dashboard_state(cycle), separators=(",", ":")).encode()
            self.respond(payload, "application/json")
            return
        self.respond(b"not found\n", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)

    def respond(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
