"""Running the agent command, setup.sh and check.sh.

The agent command is external and is passed with ``--agent``. This module
never calls a model or a provider API; it only starts the agent command
the user supplied.

Rules implemented here:

* The prompt is passed on stdin, unless the ``--agent`` template contains
  ``{prompt}``, in which case that placeholder is replaced with the
  (shell-quoted) prompt text.
* ``timeout_s`` is enforced; the whole process group is killed on timeout.
* Token counts are parsed only from a JSON ``usage`` object in the agent's
  stdout. A missing token count is ``None``, never zero, and never guessed.
* ``setup.sh`` / ``check.sh`` run in the workdir with a clean environment
  (``PATH`` only). The agent command inherits the caller's environment
  (so a real agent can still see its own API keys) with ``PATH`` modified
  for the ``with`` arm. Stripping the agent's environment would make real
  model agents unusable, so only the reproducible setup/check steps get
  the clean environment.
* Only paths are logged, never prompt text or command output.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import BenchError


@dataclass
class CommandResult:
    exit_code: int | None  # None when the command was killed on timeout
    wall_s: float
    timed_out: bool
    stdout_text: str
    stderr_text: str


def tokens_from_usage(usage) -> int | None:
    """Total tokens from a ``usage`` object, or ``None`` if unknowable."""
    if not isinstance(usage, dict):
        return None
    for key in ("total_tokens", "tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    total = 0
    found = False
    for pair in (("input_tokens", "output_tokens"),
                 ("prompt_tokens", "completion_tokens")):
        a, b = pair
        if a in usage or b in usage:
            for key in pair:
                value = usage.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    total += int(value)
                    found = True
    return total if found else None


def parse_tokens(stdout_text: str) -> int | None:
    """Parse tokens from agent stdout.

    The stdout must be JSON (as a whole, or one JSON object per line)
    with a top-level ``usage`` object. Anything else yields ``None``.
    """
    candidates: list[object] = []
    stripped = stdout_text.strip()
    if stripped:
        try:
            candidates.append(json.loads(stripped))
        except json.JSONDecodeError:
            pass
        for line in stripped.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    candidates.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    for candidate in candidates:
        if isinstance(candidate, dict) and "usage" in candidate:
            tokens = tokens_from_usage(candidate["usage"])
            if tokens is not None:
                return tokens
    return None


def _run_process(cmd, *, cwd, env, stdin_text, timeout_s):
    """Run ``cmd`` (shell string) in ``cwd``, killing its group on timeout."""
    start = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout_text, stderr_text = proc.communicate(
            input=stdin_text, timeout=timeout_s
        )
        timed_out = False
        exit_code: int | None = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
        stdout_text, stderr_text = proc.communicate()
        exit_code = None
    wall_s = time.monotonic() - start
    return CommandResult(
        exit_code=exit_code,
        wall_s=wall_s,
        timed_out=timed_out,
        stdout_text=stdout_text or "",
        stderr_text=stderr_text or "",
    )


def run_script(path: Path, *, cwd: Path, path_value: str, timeout_s: int) -> CommandResult:
    """Run a task script (``setup.sh`` / ``check.sh``) with a clean env.

    The script is invoked through ``sh`` so it runs even if the
    executable bit was lost when the task was stored (see task.py).
    """
    env = {"PATH": path_value}
    return _run_process(
        f"sh {shlex.quote(str(path))}",
        cwd=cwd,
        env=env,
        stdin_text=None,
        timeout_s=timeout_s,
    )


def run_agent(agent_cmd: str, prompt_text: str, *, cwd: Path,
              env: dict, timeout_s: int) -> CommandResult:
    """Run the external agent command in ``cwd``.

    If the template contains ``{prompt}`` it is replaced with the
    shell-quoted prompt text; otherwise the prompt is passed on stdin.
    """
    if "{prompt}" in agent_cmd:
        cmd = agent_cmd.replace("{prompt}", shlex.quote(prompt_text))
        stdin_text = None
    else:
        cmd = agent_cmd
        stdin_text = prompt_text
    return _run_process(cmd, cwd=cwd, env=env, stdin_text=stdin_text,
                        timeout_s=timeout_s)


def agent_program(agent_cmd: str) -> str:
    """First token of the agent template, for ``doctor`` presence checks."""
    stripped = agent_cmd.strip()
    if not stripped:
        return ""
    try:
        parts = shlex.split(stripped)
    except ValueError:
        parts = stripped.split()
    return parts[0] if parts else ""


def check_agent_present(agent_cmd: str) -> tuple[bool, str]:
    """Check whether the agent program can be found.

    Returns ``(present, detail)``. A bare program name is looked up on
    ``PATH``; an absolute / relative path must exist and be executable
    (or be a readable shell script, which the runner invokes via ``sh``).
    """
    program = agent_program(agent_cmd)
    if not program:
        return False, "no agent command given"
    if "/" in program:
        path = Path(program)
        if path.is_file():
            return True, f"found agent program at {program}"
        return False, f"agent program not found: {program}"
    import shutil

    found = shutil.which(program)
    if found:
        return True, f"found agent program {program} at {found}"
    return False, f"agent program not found on PATH: {program}"


def tool_failure(blocked, fix):
    return BenchError(
        "TOOL_FAILURE", blocked, fix,
        "Check the agent command and the output directory, then run again.",
        exit_code=3,
    )
