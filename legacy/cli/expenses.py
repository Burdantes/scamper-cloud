from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


DEFAULT_EXPENSE_FILE = Path(".scamper/legacy-expenses.json")
DEFAULT_BUDGET_USD = 200.0

DEFAULT_PROVIDER_RATES_USD_PER_INSTANCE_HOUR: dict[str, float] = {
    "gcp": 0.0134,
    "aws": 0.0154,
    "azr": 0.0140,
}

DEFAULT_RATE_NOTES: dict[str, str] = {
    "gcp": "Approximate e2-micro plus in-use external IPv4 address.",
    "aws": "t3.micro Linux on-demand plus in-use public IPv4 address.",
    "azr": "Approximate Standard_B1s Linux plus Standard static public IPv4 address.",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def default_ledger(budget_usd: float = DEFAULT_BUDGET_USD) -> dict[str, Any]:
    return {
        "version": 1,
        "currency": "USD",
        "budget_usd": float(budget_usd),
        "rates": {
            provider: {
                "hourly_rate_usd": rate,
                "note": DEFAULT_RATE_NOTES[provider],
            }
            for provider, rate in DEFAULT_PROVIDER_RATES_USD_PER_INSTANCE_HOUR.items()
        },
        "runs": {},
        "adjustments": [],
        "summary": {},
    }


def load_ledger(path: Path, *, budget_usd: float = DEFAULT_BUDGET_USD) -> dict[str, Any]:
    if not path.exists():
        return default_ledger(budget_usd)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ValueError(f"expense ledger is not valid JSON: {path}") from err
    if not isinstance(raw, dict):
        raise ValueError(f"expense ledger must contain a JSON object: {path}")
    return normalize_ledger(raw, budget_usd=budget_usd)


def normalize_ledger(
    ledger: Mapping[str, Any],
    *,
    budget_usd: float = DEFAULT_BUDGET_USD,
) -> dict[str, Any]:
    normalized = deepcopy(dict(ledger))
    normalized.setdefault("version", 1)
    normalized.setdefault("currency", "USD")
    normalized.setdefault("budget_usd", float(budget_usd))
    normalized.setdefault("rates", {})
    normalized.setdefault("runs", {})
    normalized.setdefault("adjustments", [])
    normalized.setdefault("summary", {})

    rates = normalized["rates"]
    if not isinstance(rates, dict):
        rates = {}
        normalized["rates"] = rates
    for provider, rate in DEFAULT_PROVIDER_RATES_USD_PER_INSTANCE_HOUR.items():
        provider_rate = rates.setdefault(provider, {})
        if not isinstance(provider_rate, dict):
            provider_rate = {}
            rates[provider] = provider_rate
        provider_rate.setdefault("hourly_rate_usd", rate)
        provider_rate.setdefault("note", DEFAULT_RATE_NOTES[provider])

    if not isinstance(normalized["runs"], dict):
        normalized["runs"] = {}
    if not isinstance(normalized["adjustments"], list):
        normalized["adjustments"] = []
    return normalized


def provider_rates_from_overrides(
    overrides: Mapping[str, float | None] | None = None,
) -> dict[str, float]:
    rates = dict(DEFAULT_PROVIDER_RATES_USD_PER_INSTANCE_HOUR)
    for provider, value in (overrides or {}).items():
        if value is not None:
            rates[provider] = float(value)
    return rates


def set_budget(ledger: dict[str, Any], budget_usd: float) -> None:
    ledger["budget_usd"] = float(budget_usd)


def update_rates(ledger: dict[str, Any], rates: Mapping[str, float]) -> None:
    ledger_rates = ledger.setdefault("rates", {})
    for provider, rate in rates.items():
        provider_rate = ledger_rates.setdefault(provider, {})
        provider_rate["hourly_rate_usd"] = float(rate)
        provider_rate.setdefault(
            "note",
            DEFAULT_RATE_NOTES.get(provider, "Configured hourly rate per active instance."),
        )


def save_ledger(path: Path, ledger: dict[str, Any], *, now: datetime | None = None) -> None:
    current_time = now or utc_now()
    ledger["summary"] = summarize_ledger(ledger, now=current_time)["summary"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _ensure_run(
    ledger: dict[str, Any],
    run_id: str,
    *,
    budget_usd: float,
    now: datetime,
) -> dict[str, Any]:
    runs = ledger.setdefault("runs", {})
    run = runs.setdefault(
        run_id,
        {
            "run_id": run_id,
            "created_at": format_timestamp(now),
            "budget_usd": float(budget_usd),
            "providers": {},
        },
    )
    run.setdefault("budget_usd", float(budget_usd))
    run.setdefault("providers", {})
    return run


def begin_provider(
    path: Path,
    *,
    run_id: str,
    provider: str,
    prefix: str,
    command: list[str],
    hourly_rate_usd: float,
    budget_usd: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = now or utc_now()
    ledger = load_ledger(path, budget_usd=budget_usd)
    set_budget(ledger, budget_usd)
    update_rates(ledger, {provider: hourly_rate_usd})
    run = _ensure_run(ledger, run_id, budget_usd=budget_usd, now=current_time)
    providers = run.setdefault("providers", {})
    providers[provider] = {
        "provider": provider,
        "prefix": prefix,
        "status": "running",
        "started_at": format_timestamp(current_time),
        "finished_at": None,
        "instance_count": 0,
        "hourly_rate_usd": float(hourly_rate_usd),
        "command": command,
        "events": [
            {
                "at": format_timestamp(current_time),
                "type": "started",
                "detail": "provider flow started",
            }
        ],
    }
    save_ledger(path, ledger, now=current_time)
    return summarize_file(path, now=current_time)


def record_provider_instances(
    path: Path,
    *,
    run_id: str,
    provider: str,
    instance_count: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = now or utc_now()
    ledger = load_ledger(path)
    run = _ensure_run(
        ledger,
        run_id,
        budget_usd=float(ledger.get("budget_usd", DEFAULT_BUDGET_USD)),
        now=current_time,
    )
    providers = run.setdefault("providers", {})
    provider_entry = providers.setdefault(
        provider,
        {
            "provider": provider,
            "status": "running",
            "started_at": format_timestamp(current_time),
            "finished_at": None,
            "events": [],
        },
    )
    provider_entry["instance_count"] = int(instance_count)
    provider_entry.setdefault(
        "hourly_rate_usd",
        DEFAULT_PROVIDER_RATES_USD_PER_INSTANCE_HOUR.get(provider, 0.0),
    )
    provider_entry.setdefault("events", []).append(
        {
            "at": format_timestamp(current_time),
            "type": "instance_count",
            "detail": f"recorded {int(instance_count)} active instances",
        }
    )
    save_ledger(path, ledger, now=current_time)
    return summarize_file(path, now=current_time)


def record_provider_instances_from_env(
    provider: str,
    instance_count: int,
    *,
    now: datetime | None = None,
) -> bool:
    expense_file = os.environ.get("SCAMPER_LEGACY_EXPENSE_FILE")
    run_id = os.environ.get("SCAMPER_LEGACY_RUN_ID")
    if not expense_file or not run_id:
        return False
    env_provider = os.environ.get("SCAMPER_LEGACY_PROVIDER", provider)
    record_provider_instances(
        Path(expense_file),
        run_id=run_id,
        provider=env_provider,
        instance_count=instance_count,
        now=now,
    )
    return True


def finish_provider(
    path: Path,
    *,
    run_id: str,
    provider: str,
    returncode: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = now or utc_now()
    ledger = load_ledger(path)
    run = _ensure_run(
        ledger,
        run_id,
        budget_usd=float(ledger.get("budget_usd", DEFAULT_BUDGET_USD)),
        now=current_time,
    )
    provider_entry = run.setdefault("providers", {}).setdefault(
        provider,
        {
            "provider": provider,
            "started_at": format_timestamp(current_time),
            "events": [],
        },
    )
    provider_entry["finished_at"] = format_timestamp(current_time)
    provider_entry["returncode"] = int(returncode)
    provider_entry["status"] = "finished" if returncode == 0 else "failed"
    provider_entry.setdefault("events", []).append(
        {
            "at": format_timestamp(current_time),
            "type": "finished",
            "detail": f"provider flow exited with {int(returncode)}",
        }
    )
    save_ledger(path, ledger, now=current_time)
    return summarize_file(path, now=current_time)


def add_adjustment(
    path: Path,
    *,
    amount_usd: float,
    note: str,
    budget_usd: float = DEFAULT_BUDGET_USD,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = now or utc_now()
    ledger = load_ledger(path, budget_usd=budget_usd)
    set_budget(ledger, budget_usd)
    ledger.setdefault("adjustments", []).append(
        {
            "at": format_timestamp(current_time),
            "amount_usd": float(amount_usd),
            "note": note,
        }
    )
    save_ledger(path, ledger, now=current_time)
    return summarize_file(path, now=current_time)


def _provider_accrual(
    provider_entry: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    started_at = parse_timestamp(provider_entry.get("started_at"))
    finished_at = parse_timestamp(provider_entry.get("finished_at")) or now
    if started_at is None:
        elapsed_hours = 0.0
    else:
        elapsed_seconds = max(0.0, (finished_at - started_at).total_seconds())
        elapsed_hours = elapsed_seconds / 3600.0

    instance_count = int(provider_entry.get("instance_count") or 0)
    hourly_rate = float(provider_entry.get("hourly_rate_usd") or 0.0)
    accrued_usd = instance_count * hourly_rate * elapsed_hours
    return {
        "provider": provider_entry.get("provider", "unknown"),
        "status": provider_entry.get("status", "unknown"),
        "instance_count": instance_count,
        "hourly_rate_usd": hourly_rate,
        "elapsed_hours": round(elapsed_hours, 6),
        "accrued_usd": round(accrued_usd, 6),
        "started_at": provider_entry.get("started_at"),
        "finished_at": provider_entry.get("finished_at"),
    }


def summarize_ledger(
    ledger: Mapping[str, Any],
    *,
    now: datetime | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    current_time = now or utc_now()
    runs = ledger.get("runs", {})
    if not isinstance(runs, Mapping):
        runs = {}

    run_summaries = []
    provider_total = 0.0
    active_providers = []
    for candidate_run_id, run in sorted(runs.items()):
        if run_id is not None and candidate_run_id != run_id:
            continue
        if not isinstance(run, Mapping):
            continue
        provider_entries = run.get("providers", {})
        if not isinstance(provider_entries, Mapping):
            provider_entries = {}
        provider_summaries = [
            _provider_accrual(provider_entry, now=current_time)
            for provider_entry in provider_entries.values()
            if isinstance(provider_entry, Mapping)
        ]
        run_total = sum(item["accrued_usd"] for item in provider_summaries)
        provider_total += run_total
        active_providers.extend(
            f"{candidate_run_id}:{item['provider']}"
            for item in provider_summaries
            if item["status"] == "running"
        )
        run_budget = float(run.get("budget_usd") or ledger.get("budget_usd") or DEFAULT_BUDGET_USD)
        run_summaries.append(
            {
                "run_id": candidate_run_id,
                "budget_usd": run_budget,
                "accrued_usd": round(run_total, 6),
                "budget_exceeded": run_total > run_budget,
                "providers": provider_summaries,
            }
        )

    adjustments = ledger.get("adjustments", [])
    if not isinstance(adjustments, list):
        adjustments = []
    adjustment_total = float(
        sum(
            float(item.get("amount_usd") or 0.0)
            for item in adjustments
            if isinstance(item, Mapping)
        )
    )
    accrued_usd = provider_total + adjustment_total
    budget_usd = float(ledger.get("budget_usd") or DEFAULT_BUDGET_USD)
    return {
        "summary": {
            "as_of": format_timestamp(current_time),
            "budget_usd": budget_usd,
            "estimated_provider_accrued_usd": round(provider_total, 6),
            "manual_adjustments_usd": round(adjustment_total, 6),
            "estimated_accrued_usd": round(accrued_usd, 6),
            "remaining_budget_usd": round(budget_usd - accrued_usd, 6),
            "budget_exceeded": accrued_usd > budget_usd,
            "active_providers": active_providers,
        },
        "runs": run_summaries,
    }


def summarize_file(
    path: Path,
    *,
    now: datetime | None = None,
    run_id: str | None = None,
    budget_usd: float = DEFAULT_BUDGET_USD,
    persist: bool = False,
) -> dict[str, Any]:
    current_time = now or utc_now()
    ledger = load_ledger(path, budget_usd=budget_usd)
    summary = summarize_ledger(ledger, now=current_time, run_id=run_id)
    if persist:
        ledger["summary"] = summary["summary"]
        save_ledger(path, ledger, now=current_time)
    return {
        "expense_file": str(path),
        **summary,
    }
