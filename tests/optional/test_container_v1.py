import importlib.util
import json
from pathlib import Path
from types import ModuleType

from scamperctl.artifacts import source_ip_from_warts_name


def load_runner() -> ModuleType:
    path = Path(__file__).parents[2] / "legacy" / "container_v1" / "run_scamper.py"
    spec = importlib.util.spec_from_file_location("container_run_scamper", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_container_runner_writes_traces_and_metadata(tmp_path: Path, monkeypatch) -> None:
    module = load_runner()
    targets = tmp_path / "targets.txt"
    results = tmp_path / "results"
    targets.write_text("8.8.8.8\n", encoding="utf-8")
    observed: dict[str, object] = {}

    class Completed:
        def __init__(self, returncode: int = 0, stderr: str = "") -> None:
            self.returncode = returncode
            self.stderr = stderr

    def fake_run(command, check, **kwargs):
        observed.setdefault("commands", []).append(command)
        observed.setdefault("checks", []).append(check)
        if command[0] == "scamper":
            warts_path = Path(command[command.index("-o") + 1])
            warts_path.write_bytes(b"representative-warts")
        else:
            kwargs["stdout"].write(
                json.dumps(
                    {
                        "type": "trace",
                        "src": "192.0.2.9",
                        "dst": "8.8.8.8",
                        "hops": [{"addr": "192.0.2.1", "probe_ttl": 1}],
                    }
                )
                + "\n"
            )
        return Completed()

    monkeypatch.setenv("TARGET_FILE", str(targets))
    monkeypatch.setenv("OUTPUT_DIR", str(results))
    monkeypatch.setenv("PROBE_NAME", "probe-1")
    monkeypatch.setenv("PROBE_IP", "192.0.2.9")
    monkeypatch.setenv("EXPERIMENT_NAME", "icmp")
    monkeypatch.setenv("SCAMPER_ARGS", '-c "trace -P ICMP" -p 1000')
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 0
    metadata_path = next(results.glob("*.metadata.json"))
    traces_path = next(results.glob("*.traces.jsonl"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    traces = [
        json.loads(line)
        for line in traces_path.read_text(encoding="utf-8").splitlines()
    ]

    assert metadata["probe"] == "probe-1"
    assert metadata["probe_ip"] == "192.0.2.9"
    assert metadata["experiment"] == "icmp"
    assert source_ip_from_warts_name(metadata["warts_file"]) == "192.0.2.9"
    assert metadata["traces_file"] == str(traces_path)
    assert metadata["traces_format"] == "json-lines"
    assert metadata["scamper_return_code"] == 0
    assert metadata["converter_return_code"] == 0
    assert traces[0]["type"] == "trace"
    assert traces[0]["dst"] == "8.8.8.8"
    assert observed["commands"][0][0] == "scamper"
    assert observed["commands"][1][0] == "sc_warts2json"
    assert observed["checks"] == [False, False]


def test_container_runner_reports_converter_failure(tmp_path: Path, monkeypatch) -> None:
    module = load_runner()
    targets = tmp_path / "targets.txt"
    results = tmp_path / "results"
    targets.write_text("8.8.8.8\n", encoding="utf-8")

    class Completed:
        def __init__(self, returncode: int, stderr: str = "") -> None:
            self.returncode = returncode
            self.stderr = stderr

    def fake_run(command, check, **kwargs):
        if command[0] == "scamper":
            warts_path = Path(command[command.index("-o") + 1])
            warts_path.write_bytes(b"invalid-warts")
            return Completed(0)
        return Completed(3, "conversion failed")

    monkeypatch.setenv("TARGET_FILE", str(targets))
    monkeypatch.setenv("OUTPUT_DIR", str(results))
    monkeypatch.setenv("PROBE_NAME", "probe-1")
    monkeypatch.setenv("PROBE_IP", "192.0.2.9")
    monkeypatch.setenv("EXPERIMENT_NAME", "icmp")
    monkeypatch.setenv("SCAMPER_ARGS", '-c "trace -P ICMP" -p 1000')
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 3
    metadata_path = next(results.glob("*.metadata.json"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert metadata["converter_return_code"] == 3
    assert metadata["converter_stderr"] == "conversion failed"
    assert metadata["return_code"] == 3
    assert not list(results.glob("*.traces.jsonl"))
