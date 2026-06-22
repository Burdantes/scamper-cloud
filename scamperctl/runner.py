from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Protocol, Sequence


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


class Runner(Protocol):
    def run(self, args: Sequence[str], *, check: bool = True) -> CommandResult: ...


class CommandFailed(RuntimeError):
    pass


class SubprocessRunner:
    def run(self, args: Sequence[str], *, check: bool = True) -> CommandResult:
        command = [str(arg) for arg in args]
        logger.debug("running command: %s", command)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as err:
            raise CommandFailed(
                f"required command {command[0]!r} was not found on PATH"
            ) from err

        result = CommandResult(
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise CommandFailed(f"command failed ({result.returncode}): {detail}")
        return result
