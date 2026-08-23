from pathlib import Path
from typing import Sequence

import pytest

from legacy.cli import cli
from providers import expenses
from scamperctl.runner import CommandResult


class FakeRunner:
    def __init__(self, instance_counts: dict[str, int] | None = None) -> None:
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.instance_counts = instance_counts or {}

    def run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        self.commands.append(list(args))
        self.envs.append(dict(env or {}))
        provider = Path(args[1]).stem
        if env is not None and provider in self.instance_counts:
            expenses.record_provider_instances(
                Path(env["SCAMPER_LEGACY_EXPENSE_FILE"]),
                run_id=env["SCAMPER_LEGACY_RUN_ID"],
                provider=env["SCAMPER_LEGACY_PROVIDER"],
                instance_count=self.instance_counts[provider],
            )
        return CommandResult(stdout=f"{provider} done\n")


def write_file(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("representative\n", encoding="utf-8")
    return str(path)


def configure_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def installed_module_check(name: str, *, required: bool = True) -> cli.Check:
        return cli.Check(
            name=f"python:{name}",
            ok=True,
            required=required,
            detail="installed",
        )

    monkeypatch.setattr(cli, "_module_check", installed_module_check)
    monkeypatch.setattr(cli.settings, "SCAMPER_IP_DST", write_file(tmp_path / "targets.txt"))
    monkeypatch.setattr(cli.settings, "SCAMPER_UPLOAD_SCRIPT", write_file(tmp_path / "upload.py"))
    monkeypatch.setattr(cli.settings, "SCAMPER_SMOKE_SCRIPT", write_file(tmp_path / "scamper-smoke.sh"))
    monkeypatch.setattr(
        cli.settings,
        "SCAMPER_CAMPAIGN_RUNNER",
        write_file(tmp_path / "run-scamper-campaign.py"),
    )
    monkeypatch.setattr(cli.settings, "WARTS_STORAGE_CREDENTIALS", write_file(tmp_path / "key.json"))
    monkeypatch.setattr(cli.settings, "GCP_SCAMPER_SCRIPT", write_file(tmp_path / "run-gcp.sh"))
    monkeypatch.setattr(cli.settings, "AWS_SCAMPER_VM_SCRIPT", write_file(tmp_path / "run-aws.sh"))
    monkeypatch.setattr(cli.settings, "AZR_SCAMPER_VM_SCRIPT", write_file(tmp_path / "run-azr.sh"))
    monkeypatch.setattr(cli.settings, "GCP_SCAMPER_SSH_KEY", write_file(tmp_path / "gcp-key"))
    monkeypatch.setattr(cli.settings, "AWS_SCAMPER_SSH_KEY", write_file(tmp_path / "aws-key"))
    azr_key = tmp_path / "azr-key"
    monkeypatch.setattr(cli.settings, "AZR_SCAMPER_SSH_KEY", write_file(azr_key))
    write_file(Path(f"{azr_key}.pub"))
    monkeypatch.setattr(cli.settings, "GCP_PROJECT", "example-project")
    monkeypatch.setattr(cli.settings, "GCP_SERVICE_ACCOUNT", "svc@example-project.iam.gserviceaccount.com")
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "00000000-0000-0000-0000-000000000000")


def test_parse_providers_accepts_azure_alias() -> None:
    assert cli.parse_providers("gcp,azure,aws,gcp") == ("gcp", "azr", "aws")


def test_build_plan_reports_ready_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configure_files(tmp_path, monkeypatch)

    plan = cli.build_plan(
        ("gcp", "aws", "azr"),
        run_id="Tomorrow_Run",
        python_executable="python",
    )

    assert plan["run_id"] == "tomorrow-run"
    assert plan["ready"] is True
    providers = {provider["provider"]: provider for provider in plan["providers"]}
    assert set(providers) == set(cli.PROVIDER_ORDER)
    for provider in cli.PROVIDER_ORDER:
        assert providers[provider]["bucket"] == cli.settings.SCAMPER_RESULTS_BUCKET
        assert providers[provider]["object_prefix"] == (
            f"runs/{provider}-tomorrow-run"
        )
    commands = [provider["command"] for provider in plan["providers"]]
    assert commands == [
        ["python", "gcp.py", "--prefix", "gcp-tomorrow-run", "--log-dir", "gcp-tomorrow-run-logs", "--apply"],
        ["python", "aws.py", "--prefix", "aws-tomorrow-run", "--log-dir", "aws-tomorrow-run-logs", "--apply"],
        ["python", "azr.py", "--prefix", "azr-tomorrow-run", "--log-dir", "azr-tomorrow-run-logs", "--apply"],
    ]


def test_build_plan_caps_gcp_and_aws_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_files(tmp_path, monkeypatch)

    plan = cli.build_plan(
        ("gcp", "aws"),
        run_id="Ten_VM",
        python_executable="python",
        max_instances=10,
    )

    assert plan["max_instances"] == 10
    commands = {provider["provider"]: provider["command"] for provider in plan["providers"]}
    assert commands["gcp"] == [
        "python",
        "gcp.py",
        "--prefix",
        "gcp-ten-vm",
        "--log-dir",
        "gcp-ten-vm-logs",
        "--apply",
        "--max-instances",
        "10",
    ]
    assert commands["aws"] == [
        "python",
        "aws.py",
        "--prefix",
        "aws-ten-vm",
        "--log-dir",
        "aws-ten-vm-logs",
        "--apply",
        "--max-instances",
        "10",
    ]


def test_build_plan_caps_gcp_instances_and_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_files(tmp_path, monkeypatch)

    plan = cli.build_plan(
        ("gcp",),
        run_id="GCP_Canary",
        python_executable="python",
        max_instances=10,
        max_targets=10000,
    )

    provider = plan["providers"][0]
    assert plan["max_instances"] == 10
    assert plan["max_targets"] == 10000
    assert provider["max_instances"] == 10
    assert provider["max_targets"] == 10000
    assert provider["command"] == [
        "python",
        "gcp.py",
        "--prefix",
        "gcp-gcp-canary",
        "--log-dir",
        "gcp-gcp-canary-logs",
        "--apply",
        "--max-instances",
        "10",
        "--max-targets",
        "10000",
    ]


def test_build_plan_caps_aws_instances_and_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_files(tmp_path, monkeypatch)

    plan = cli.build_plan(
        ("aws",),
        run_id="AWS_Canary",
        python_executable="python",
        max_instances=10,
        max_targets=10000,
    )

    provider = plan["providers"][0]
    assert plan["max_instances"] == 10
    assert plan["max_targets"] == 10000
    assert provider["max_instances"] == 10
    assert provider["max_targets"] == 10000
    assert provider["command"] == [
        "python",
        "aws.py",
        "--prefix",
        "aws-aws-canary",
        "--log-dir",
        "aws-aws-canary-logs",
        "--apply",
        "--max-instances",
        "10",
        "--max-targets",
        "10000",
    ]


def test_build_plan_caps_azure_instances_and_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_files(tmp_path, monkeypatch)

    plan = cli.build_plan(
        ("azr",),
        run_id="Azure_Canary",
        python_executable="python",
        max_instances=10,
        max_targets=10000,
    )

    provider = plan["providers"][0]
    assert plan["max_instances"] == 10
    assert plan["max_targets"] == 10000
    assert provider["max_instances"] == 10
    assert provider["max_targets"] == 10000
    assert provider["command"] == [
        "python",
        "azr.py",
        "--prefix",
        "azr-azure-canary",
        "--log-dir",
        "azr-azure-canary-logs",
        "--apply",
        "--max-instances",
        "10",
        "--max-targets",
        "10000",
    ]


def test_run_plan_rejects_failed_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configure_files(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.settings, "GCP_SCAMPER_SSH_KEY", str(tmp_path / "missing-key"))
    plan = cli.build_plan(("gcp",), run_id="tomorrow", python_executable="python")

    with pytest.raises(ValueError, match="preflight checks failed"):
        cli.run_plan(plan, FakeRunner(), expense_file=tmp_path / "expenses.json")


def test_preflight_requires_smoke_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configure_files(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.settings, "SCAMPER_SMOKE_SCRIPT", str(tmp_path / "missing-smoke.sh"))

    plan = cli.build_plan(("gcp",), run_id="tomorrow", python_executable="python")

    assert plan["ready"] is False
    checks = plan["providers"][0]["checks"]
    smoke_check = next(check for check in checks if check["name"] == "smoke-test script")
    assert smoke_check["ok"] is False


def test_run_plan_executes_provider_commands_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_files(tmp_path, monkeypatch)
    plan = cli.build_plan(("gcp", "aws"), run_id="tomorrow", python_executable="python")
    runner = FakeRunner({"gcp": 2, "aws": 3})

    results = cli.run_plan(plan, runner, expense_file=tmp_path / "expenses.json")

    assert [command[1] for command in runner.commands] == ["gcp.py", "aws.py"]
    assert [env["SCAMPER_LEGACY_PROVIDER"] for env in runner.envs] == ["gcp", "aws"]
    assert [result["provider"] for result in results] == ["gcp", "aws"]
    ledger = expenses.load_ledger(tmp_path / "expenses.json")
    providers = ledger["runs"]["tomorrow"]["providers"]
    assert providers["gcp"]["instance_count"] == 2
    assert providers["aws"]["instance_count"] == 3


def test_run_plan_sets_max_instances_environment_for_aws(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_files(tmp_path, monkeypatch)
    plan = cli.build_plan(
        ("aws",),
        run_id="ten",
        python_executable="python",
        max_instances=10,
        max_targets=10000,
    )
    runner = FakeRunner({"aws": 10})

    cli.run_plan(plan, runner, expense_file=tmp_path / "expenses.json")

    assert runner.envs[0]["SCAMPER_LEGACY_MAX_INSTANCES"] == "10"
    assert runner.envs[0]["SCAMPER_LEGACY_MAX_TARGETS"] == "10000"


def test_run_plan_sets_canary_environment_for_gcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_files(tmp_path, monkeypatch)
    plan = cli.build_plan(
        ("gcp",),
        run_id="canary",
        python_executable="python",
        max_instances=10,
        max_targets=10000,
    )
    runner = FakeRunner({"gcp": 10})

    cli.run_plan(plan, runner, expense_file=tmp_path / "expenses.json")

    assert runner.envs[0]["SCAMPER_LEGACY_MAX_INSTANCES"] == "10"
    assert runner.envs[0]["SCAMPER_LEGACY_MAX_TARGETS"] == "10000"


def test_run_plan_sets_canary_environment_for_azure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_files(tmp_path, monkeypatch)
    plan = cli.build_plan(
        ("azr",),
        run_id="canary",
        python_executable="python",
        max_instances=10,
        max_targets=10000,
    )
    runner = FakeRunner({"azr": 10})

    cli.run_plan(plan, runner, expense_file=tmp_path / "expenses.json")

    assert runner.envs[0]["SCAMPER_LEGACY_MAX_INSTANCES"] == "10"
    assert runner.envs[0]["SCAMPER_LEGACY_MAX_TARGETS"] == "10000"


def test_expense_ledger_flags_budget_threshold(tmp_path: Path) -> None:
    expense_file = tmp_path / "expenses.json"
    start = expenses.parse_timestamp("2026-07-12T00:00:00Z")
    end = expenses.parse_timestamp("2026-07-12T02:00:00Z")
    assert start is not None
    assert end is not None

    expenses.begin_provider(
        expense_file,
        run_id="tomorrow",
        provider="aws",
        prefix="aws-tomorrow",
        command=["python", "aws.py", "--apply"],
        hourly_rate_usd=60.0,
        budget_usd=100.0,
        now=start,
    )
    expenses.record_provider_instances(
        expense_file,
        run_id="tomorrow",
        provider="aws",
        instance_count=1,
        now=start,
    )

    summary = expenses.summarize_file(expense_file, now=end, budget_usd=100.0)

    assert summary["summary"]["estimated_accrued_usd"] == 120.0
    assert summary["summary"]["budget_exceeded"] is True


def test_run_plan_skips_provider_when_expense_budget_is_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_files(tmp_path, monkeypatch)
    expense_file = tmp_path / "expenses.json"
    expenses.add_adjustment(
        expense_file,
        amount_usd=201.0,
        note="already accrued",
    )
    plan = cli.build_plan(("gcp", "aws"), run_id="tomorrow", python_executable="python")
    runner = FakeRunner()

    results = cli.run_plan(plan, runner, expense_file=expense_file)

    assert runner.commands == []
    assert results[0]["provider"] == "gcp"
    assert results[0]["command"] == [
        "python",
        "gcp.py",
        "--prefix",
        "gcp-tomorrow",
        "--log-dir",
        "gcp-tomorrow-logs",
        "--apply",
    ]
    assert results[0]["skipped"] is True
    assert results[0]["reason"] == "expense budget exceeded"
    assert results[0]["expenses"]["budget_exceeded"] is True
