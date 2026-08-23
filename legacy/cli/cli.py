from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Protocol, Sequence

from providers import settings

from providers import expenses
from scamperctl.runner import CommandFailed, CommandResult, SubprocessRunner


logger = logging.getLogger("scamper-legacy")

PROVIDER_ORDER = ("gcp", "aws", "azr")
PROVIDER_ALIASES = {
    "azure": "azr",
}


class Runner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "required": self.required,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ProviderPlan:
    provider: str
    prefix: str
    bucket: str
    object_prefix: str
    log_dir: str
    hourly_rate_usd: float
    command: tuple[str, ...]
    checks: tuple[Check, ...]
    warnings: tuple[str, ...] = ()
    max_instances: int | None = None
    max_targets: int | None = None

    @property
    def ready(self) -> bool:
        return all(check.ok or not check.required for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "prefix": self.prefix,
            "bucket": self.bucket,
            "object_prefix": self.object_prefix,
            "log_dir": self.log_dir,
            "hourly_rate_usd": self.hourly_rate_usd,
            "command": list(self.command),
            "max_instances": self.max_instances,
            "max_targets": self.max_targets,
            "ready": self.ready,
            "checks": [check.to_dict() for check in self.checks],
            "warnings": list(self.warnings),
        }


def safe_run_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9-]+", "-", value).strip("-").lower()
    if not cleaned:
        raise ValueError("run id must contain at least one letter or number")
    return cleaned


def default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("legacy-%Y%m%dT%H%M%SZ").lower()


def parse_providers(value: str) -> tuple[str, ...]:
    providers = []
    for raw_item in value.split(","):
        item = raw_item.strip().lower()
        if not item:
            continue
        provider = PROVIDER_ALIASES.get(item, item)
        if provider not in PROVIDER_ORDER:
            raise argparse.ArgumentTypeError(
                f"unknown provider {raw_item!r}; choose from gcp, aws, azr"
            )
        providers.append(provider)
    if not providers:
        raise argparse.ArgumentTypeError("provide at least one provider")
    return tuple(dict.fromkeys(providers))


def _path(value: str) -> Path:
    return Path(value).expanduser()


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than 0")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than 0")
    return parsed


def _file_check(name: str, value: str, *, required: bool = True) -> Check:
    path = _path(value)
    return Check(
        name=name,
        ok=path.is_file(),
        required=required,
        detail=str(path),
    )


def _nonempty_check(name: str, value: object, *, required: bool = True) -> Check:
    return Check(
        name=name,
        ok=bool(value),
        required=required,
        detail=str(value) if value else "not configured",
    )


def _env_check(name: str, *, required: bool = True) -> Check:
    value = os.environ.get(name, "")
    return Check(
        name=f"env:{name}",
        ok=bool(value),
        required=required,
        detail="set" if value else "not set",
    )


def _module_check(name: str, *, required: bool = True) -> Check:
    try:
        installed = importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        installed = False
    return Check(
        name=f"python:{name}",
        ok=installed,
        required=required,
        detail="installed" if installed else "not installed",
    )


def _import_attribute_check(
    module_name: str,
    attribute_name: str,
    *,
    required: bool = True,
) -> Check:
    try:
        module = importlib.import_module(module_name)
        installed = hasattr(module, attribute_name)
    except (ImportError, ModuleNotFoundError):
        installed = False
    return Check(
        name=f"python:{module_name}.{attribute_name}",
        ok=installed,
        required=required,
        detail="installed" if installed else "not installed",
    )


def _common_checks() -> tuple[Check, ...]:
    return (
        _file_check("target file", settings.SCAMPER_IP_DST),
        _file_check("upload script", settings.SCAMPER_UPLOAD_SCRIPT),
        _file_check("smoke-test script", settings.SCAMPER_SMOKE_SCRIPT),
        _file_check("campaign runner", settings.SCAMPER_CAMPAIGN_RUNNER),
        _file_check("GCS service-account key", settings.WARTS_STORAGE_CREDENTIALS),
        _module_check("google.cloud.storage"),
        _module_check("googleapiclient.discovery"),
        _module_check("google.oauth2.service_account"),
    )


def _provider_checks(provider: str) -> tuple[Check, ...]:
    if provider == "gcp":
        return (
            *_common_checks(),
            _file_check("GCP VM script", settings.GCP_SCAMPER_SCRIPT),
            _file_check("GCP SSH private key", settings.GCP_SCAMPER_SSH_KEY),
            _nonempty_check("GCP project", settings.GCP_PROJECT),
            _nonempty_check("GCP service account", settings.GCP_SERVICE_ACCOUNT),
        )
    if provider == "aws":
        return (
            *_common_checks(),
            _file_check("AWS VM script", settings.AWS_SCAMPER_VM_SCRIPT),
            _file_check("AWS SSH private key", settings.AWS_SCAMPER_SSH_KEY),
            _module_check("boto3"),
            _module_check("botocore"),
            _env_check("AWS_PROFILE", required=False),
        )
    if provider == "azr":
        public_key = f"{settings.AZR_SCAMPER_SSH_KEY}.pub"
        return (
            *_common_checks(),
            _file_check("Azure VM script", settings.AZR_SCAMPER_VM_SCRIPT),
            _file_check("Azure SSH private key", settings.AZR_SCAMPER_SSH_KEY),
            _file_check("Azure SSH public key", public_key),
            _module_check("azure.identity"),
            _module_check("azure.mgmt.compute"),
            _module_check("azure.mgmt.network"),
            _import_attribute_check(
                "azure.mgmt.resource.resources",
                "ResourceManagementClient",
            ),
            _module_check("azure.mgmt.subscription"),
            _env_check("AZURE_SUBSCRIPTION_ID"),
            _env_check("AZURE_TENANT_ID", required=False),
            _env_check("AZURE_CLIENT_ID", required=False),
            _env_check("AZURE_CLIENT_SECRET", required=False),
        )
    raise AssertionError(f"unhandled provider: {provider}")


def _provider_warnings(provider: str) -> tuple[str, ...]:
    if provider == "gcp":
        return (
            "Legacy GCP flow copies a service-account key to each VM; prefer scamperctl for new GCP-only runs.",
        )
    if provider == "aws":
        return (
            "Legacy AWS flow stores results in a GCS bucket and expects Google storage credentials locally.",
        )
    if provider == "azr":
        return (
            "Legacy Azure flow creates one resource group per run and deletes that group after measurement.",
        )
    raise AssertionError(f"unhandled provider: {provider}")


def build_provider_plan(
    provider: str,
    *,
    run_id: str,
    python_executable: str,
    hourly_rate_usd: float,
    max_instances: int | None = None,
    max_targets: int | None = None,
) -> ProviderPlan:
    prefix = f"{provider}-{run_id}"
    command_items = [
        python_executable,
        f"{provider if provider != 'azr' else 'azr'}.py",
        "--prefix",
        prefix,
        "--log-dir",
        f"{prefix}-logs",
        "--apply",
    ]
    if max_instances is not None:
        command_items.extend(["--max-instances", str(max_instances)])
    if max_targets is not None:
        command_items.extend(["--max-targets", str(max_targets)])

    return ProviderPlan(
        provider=provider,
        prefix=prefix,
        bucket=settings.SCAMPER_RESULTS_BUCKET,
        object_prefix=f"runs/{prefix}",
        log_dir=f"{prefix}-logs",
        hourly_rate_usd=hourly_rate_usd,
        command=tuple(command_items),
        checks=_provider_checks(provider),
        warnings=_provider_warnings(provider),
        max_instances=max_instances,
        max_targets=max_targets,
    )


def build_plan(
    providers: Iterable[str],
    *,
    run_id: str | None = None,
    python_executable: str | None = None,
    budget_usd: float = expenses.DEFAULT_BUDGET_USD,
    provider_rates: Mapping[str, float] | None = None,
    max_instances: int | None = None,
    max_targets: int | None = None,
) -> dict[str, object]:
    resolved_run_id = safe_run_id(run_id or default_run_id())
    executable = python_executable or sys.executable
    rates = provider_rates or expenses.DEFAULT_PROVIDER_RATES_USD_PER_INSTANCE_HOUR
    provider_plans = tuple(
        build_provider_plan(
            provider,
            run_id=resolved_run_id,
            python_executable=executable,
            hourly_rate_usd=rates[provider],
            max_instances=max_instances,
            max_targets=max_targets,
        )
        for provider in providers
    )
    return {
        "action": "legacy-flow",
        "run_id": resolved_run_id,
        "budget_usd": float(budget_usd),
        "max_instances": max_instances,
        "max_targets": max_targets,
        "ready": all(plan.ready for plan in provider_plans),
        "providers": [plan.to_dict() for plan in provider_plans],
    }


def run_plan(
    plan: dict[str, object],
    runner: Runner,
    *,
    expense_file: Path = expenses.DEFAULT_EXPENSE_FILE,
    allow_over_budget: bool = False,
) -> list[dict[str, object]]:
    if not bool(plan["ready"]):
        raise ValueError("preflight checks failed; fix required checks before --apply")

    results: list[dict[str, object]] = []
    for provider_plan in plan["providers"]:
        provider = str(provider_plan["provider"])
        command = tuple(str(item) for item in provider_plan["command"])
        expense_snapshot = expenses.summarize_file(
            expense_file,
            budget_usd=float(plan["budget_usd"]),
            persist=True,
        )
        if expense_snapshot["summary"]["budget_exceeded"] and not allow_over_budget:
            results.append(
                {
                    "provider": provider,
                    "command": list(command),
                    "skipped": True,
                    "reason": "expense budget exceeded",
                    "expenses": expense_snapshot["summary"],
                }
            )
            logger.error(
                "expense budget exceeded: estimated $%.2f of $%.2f; skipping %s",
                expense_snapshot["summary"]["estimated_accrued_usd"],
                expense_snapshot["summary"]["budget_usd"],
                provider,
            )
            break

        logger.info("running %s legacy flow", provider)
        expenses.begin_provider(
            expense_file,
            run_id=str(plan["run_id"]),
            provider=provider,
            prefix=str(provider_plan["prefix"]),
            command=list(command),
            hourly_rate_usd=float(provider_plan["hourly_rate_usd"]),
            budget_usd=float(plan["budget_usd"]),
        )
        provider_env = {
            "SCAMPER_LEGACY_EXPENSE_FILE": str(expense_file),
            "SCAMPER_LEGACY_RUN_ID": str(plan["run_id"]),
            "SCAMPER_LEGACY_PROVIDER": provider,
        }
        max_instances = provider_plan.get("max_instances")
        if max_instances is not None:
            provider_env["SCAMPER_LEGACY_MAX_INSTANCES"] = str(max_instances)
        max_targets = provider_plan.get("max_targets")
        if max_targets is not None:
            provider_env["SCAMPER_LEGACY_MAX_TARGETS"] = str(max_targets)
        try:
            result = runner.run(command, check=False, env=provider_env)
        except Exception:
            expenses.finish_provider(
                expense_file,
                run_id=str(plan["run_id"]),
                provider=provider,
                returncode=1,
            )
            raise
        expense_snapshot = expenses.finish_provider(
            expense_file,
            run_id=str(plan["run_id"]),
            provider=provider,
            returncode=result.returncode,
        )
        if expense_snapshot["summary"]["budget_exceeded"]:
            logger.error(
                "expense budget exceeded: estimated $%.2f of $%.2f",
                expense_snapshot["summary"]["estimated_accrued_usd"],
                expense_snapshot["summary"]["budget_usd"],
            )
        results.append(
            {
                "provider": provider,
                "command": list(command),
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "expenses": expense_snapshot["summary"],
            }
        )
        if result.returncode != 0:
            logger.error("%s legacy flow failed with exit code %d", provider, result.returncode)
            break
    return results


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2))


def provider_rate_overrides(args: argparse.Namespace) -> dict[str, float | None]:
    return {
        "gcp": getattr(args, "gcp_hourly_usd", None),
        "aws": getattr(args, "aws_hourly_usd", None),
        "azr": getattr(args, "azr_hourly_usd", None),
    }


def add_expense_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--expense-file",
        type=_path,
        default=expenses.DEFAULT_EXPENSE_FILE,
        help="local JSON expense ledger (default: .scamper/legacy-expenses.json)",
    )
    parser.add_argument(
        "--budget-usd",
        type=_positive_float,
        default=expenses.DEFAULT_BUDGET_USD,
        help="local budget threshold for the ledger (default: 200)",
    )
    parser.add_argument(
        "--gcp-hourly-usd",
        type=_positive_float,
        help="override GCP hourly USD per active instance",
    )
    parser.add_argument(
        "--aws-hourly-usd",
        type=_positive_float,
        help="override AWS hourly USD per active instance",
    )
    parser.add_argument(
        "--azr-hourly-usd",
        type=_positive_float,
        help="override Azure hourly USD per active instance",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scamper-legacy",
        description="Plan, preflight, and run the legacy GCP/AWS/Azure VM flows.",
    )
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command, help_text in (
        ("preflight", "check local prerequisites and print the execution plan"),
        ("run", "print the plan, or run it with --apply"),
    ):
        subparser = subparsers.add_parser(command, help=help_text)
        subparser.add_argument(
            "--providers",
            type=parse_providers,
            default=PROVIDER_ORDER,
            help="comma-separated providers: gcp,aws,azr (default: all)",
        )
        subparser.add_argument(
            "--run-id",
            help="shared run id; defaults to a UTC timestamp",
        )
        subparser.add_argument(
            "--max-instances",
            type=_positive_int,
            help="cap VM launches per supported provider; currently applied to all providers",
        )
        subparser.add_argument(
            "--max-targets",
            type=_positive_int,
            help="cap targets per VM for supported providers; currently applied to all providers",
        )
        add_expense_arguments(subparser)
        if command == "run":
            subparser.add_argument(
                "--apply",
                action="store_true",
                help="execute the provider scripts; otherwise only print the plan",
            )
            subparser.add_argument(
                "--allow-over-budget",
                action="store_true",
                help="continue launching providers even if the expense ledger is above budget",
            )

    expenses_parser = subparsers.add_parser(
        "expenses",
        help="print or update the local legacy expense ledger",
    )
    expenses_parser.add_argument(
        "--expense-file",
        type=_path,
        default=expenses.DEFAULT_EXPENSE_FILE,
        help="local JSON expense ledger (default: .scamper/legacy-expenses.json)",
    )
    expenses_parser.add_argument(
        "--budget-usd",
        type=_positive_float,
        default=expenses.DEFAULT_BUDGET_USD,
        help="budget threshold to initialize or update the ledger (default: 200)",
    )
    expenses_parser.add_argument(
        "--run-id",
        help="only include one run in the printed run summaries",
    )
    expenses_parser.add_argument(
        "--add-adjustment-usd",
        type=float,
        help="add a manual USD adjustment, such as reconciled storage or IP charges",
    )
    expenses_parser.add_argument(
        "--note",
        default="manual adjustment",
        help="note used with --add-adjustment-usd",
    )

    return parser


def execute(args: argparse.Namespace, runner: Runner | None = None) -> int:
    if args.command == "expenses":
        if args.add_adjustment_usd is not None:
            print_json(
                expenses.add_adjustment(
                    args.expense_file,
                    amount_usd=args.add_adjustment_usd,
                    note=args.note,
                    budget_usd=args.budget_usd,
                )
            )
            return 0
        ledger = expenses.load_ledger(args.expense_file, budget_usd=args.budget_usd)
        expenses.set_budget(ledger, args.budget_usd)
        expenses.save_ledger(args.expense_file, ledger)
        print_json(
            expenses.summarize_file(
                args.expense_file,
                run_id=args.run_id,
                budget_usd=args.budget_usd,
                persist=True,
            )
        )
        return 0

    rates = expenses.provider_rates_from_overrides(provider_rate_overrides(args))
    plan = build_plan(
        args.providers,
        run_id=args.run_id,
        budget_usd=args.budget_usd,
        provider_rates=rates,
        max_instances=args.max_instances,
        max_targets=args.max_targets,
    )
    if args.command == "preflight":
        print_json(plan)
        return 0 if bool(plan["ready"]) else 2

    if args.command == "run":
        if not args.apply:
            print_json(plan)
            return 0
        results = run_plan(
            plan,
            runner or SubprocessRunner(),
            expense_file=args.expense_file,
            allow_over_budget=args.allow_over_budget,
        )
        expense_snapshot = expenses.summarize_file(
            args.expense_file,
            budget_usd=args.budget_usd,
            persist=True,
        )
        print_json(
            {
                "run_id": plan["run_id"],
                "expense_file": str(args.expense_file),
                "expenses": expense_snapshot["summary"],
                "results": results,
            }
        )
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    try:
        return execute(args)
    except (CommandFailed, FileNotFoundError, ValueError) as err:
        logger.error("%s", err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
