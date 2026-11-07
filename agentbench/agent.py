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


# Token buckets, counted once each. Cache tokens are input-side: in the
# Anthropic-style buckets ``input_tokens`` excludes cache reads and
# cache writes, so a faithful total is input + cache read + cache
# creation + output. Reasoning tokens are a *subset* of output (they
# are already inside ``output_tokens``), so they are deliberately NOT
# listed here -- adding them would double-count.
_INPUT_KEYS = (
    "input_tokens",
    "prompt_tokens",
    "inputTokens",
    "input",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "cacheCreationInputTokens",
    "cacheReadInputTokens",
    "cacheRead",
    "cacheWrite",
    "cacheReadTokens",
    "cacheWriteTokens",
)
_OUTPUT_KEYS = ("output_tokens", "completion_tokens", "outputTokens", "output")


def _as_int(value) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return None


def tokens_from_usage(usage) -> int | None:
    """Total tokens from a ``usage``-shaped object, or ``None``.

    Accepts the documented shapes (see docs/DECISIONS.md): Anthropic /
    Claude ``usage`` (snake_case, incl. cache buckets), OpenAI
    ``prompt_tokens`` / ``completion_tokens``, Claude ``modelUsage``
    values and pi ``token_usage`` events (camelCase), and pi
    ``usage`` (``input`` / ``output`` / ``cacheRead`` / ``cacheWrite``).
    A ``total_tokens`` / ``totalTokens`` / ``tokens`` field, when
    present, wins over summing the buckets. Anything else is ``None``.
    """
    if not isinstance(usage, dict):
        return None
    for key in ("total_tokens", "totalTokens", "tokens"):
        value = _as_int(usage.get(key))
        if value is not None:
            return value
    total = 0
    found = False
    for key in _INPUT_KEYS + _OUTPUT_KEYS:
        value = _as_int(usage.get(key))
        if value is not None:
            total += value
            found = True
    return total if found else None


def _is_pi_style_usage(usage: dict) -> bool:
    """True for pi's per-turn ``usage`` (``input``/``output`` buckets).

    Pi reports one ``usage`` object per turn and the harness sums them;
    Claude reports one aggregate ``usage`` object. The key sets do not
    overlap (pi uses bare ``input``/``output``, Claude uses
    ``input_tokens``/``output_tokens``), so the shape tells them apart.
    """
    return (
        bool(set(usage) & {"input", "output", "cacheRead", "cacheWrite"})
        and "input_tokens" not in usage
        and "output_tokens" not in usage
    )


def _usage_candidates(obj: object) -> list[tuple[str, int]]:
    """``(kind, tokens)`` pairs found in one parsed JSON object.

    Kind is ``"aggregate"`` for a whole-run total (a Claude result
    envelope, or a per-model ``modelUsage`` breakdown) and
    ``"per_turn"`` for one turn's usage (a pi ``message_end`` event, a
    Claude stream ``message.usage``, or a pi-agent ``token_usage``
    event). ``modelUsage`` is only used when there is no top-level
    ``usage`` -- the two describe the same run, and counting both
    would double-count.
    """
    if not isinstance(obj, dict):
        return []
    found: list[tuple[str, int]] = []
    if obj.get("type") == "token_usage":
        tokens = tokens_from_usage(obj)
        if tokens is not None:
            found.append(("per_turn", tokens))
        return found
    usage = obj.get("usage")
    if isinstance(usage, dict):
        tokens = tokens_from_usage(usage)
        if tokens is not None:
            kind = "per_turn" if _is_pi_style_usage(usage) else "aggregate"
            found.append((kind, tokens))
    elif isinstance(obj.get("modelUsage"), dict):
        total = 0
        any_tokens = False
        for per_model in obj["modelUsage"].values():
            tokens = tokens_from_usage(per_model)
            if tokens is not None:
                total += tokens
                any_tokens = True
        if any_tokens:
            found.append(("aggregate", total))
    message = obj.get("message")
    if isinstance(message, dict) and isinstance(message.get("usage"), dict):
        tokens = tokens_from_usage(message["usage"])
        if tokens is not None:
            found.append(("per_turn", tokens))
    return found


def parse_tokens(stdout_text: str) -> int | None:
    """Parse tokens from agent stdout.

    The stdout must be JSON: either one object, or one JSON object per
    line (NDJSON, as in ``claude --output-format stream-json`` and
    ``pi --mode json``). Supported shapes are the documented ones in
    ``_usage_candidates``. Combining rule: if any whole-run aggregate
    exists, the LAST one wins (a Claude stream's final result envelope
    is the aggregate); otherwise per-turn usages are SUMMED (pi
    reports one ``usage`` per turn). Anything else yields ``None`` --
    a missing token count is never guessed and never zero.
    """
    objects: list[object] = []
    stripped = stdout_text.strip()
    if stripped:
        try:
            objects.append(json.loads(stripped))
        except json.JSONDecodeError:
            for line in stripped.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        objects.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    aggregates: list[int] = []
    per_turn: list[int] = []
    for obj in objects:
        for kind, tokens in _usage_candidates(obj):
            if kind == "aggregate":
                aggregates.append(tokens)
            else:
                per_turn.append(tokens)
    if aggregates:
        return aggregates[-1]
    if per_turn:
        return sum(per_turn)
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
        stdout_text, stderr_text = proc.communicate(input=stdin_text, timeout=timeout_s)
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


_FIRST_TOKEN_RE = None  # built lazily to keep module import light


def _first_token(agent_cmd: str):
    """(start, end, unquoted) of the first token of a shell template."""
    import re

    match = re.match(r"\s*(\"[^\"]+\"|'[^']+'|[^\s]+)", agent_cmd)
    if not match:
        return None
    token = match.group(1)
    unquoted = token[1:-1] if len(token) >= 2 and token[0] in "\"'" else token
    return match.start(1), match.end(1), unquoted


def resolve_agent_program(agent_cmd: str) -> str:
    """Resolve a relative agent program path against the caller's cwd.

    The agent runs with cwd set to its workdir, so a relative path like
    ``./bin/agent.sh`` would otherwise be looked up inside the workdir
    and fail with "not found". Bare program names (no ``/``) are left
    alone so they are still found on ``PATH``.
    """
    found = _first_token(agent_cmd)
    if not found:
        return agent_cmd
    start, end, program = found
    if "/" not in program or "{prompt}" in program:
        return agent_cmd
    path = Path(program)
    if path.is_absolute() or not path.is_file():
        return agent_cmd
    absolute = (Path.cwd() / path).resolve()
    return agent_cmd[:start] + shlex.quote(str(absolute)) + agent_cmd[end:]


def _is_text_script(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            chunk = handle.read(512)
        return b"\x00" not in chunk
    except OSError:
        return False


def run_agent(
    agent_cmd: str, prompt_text: str, *, cwd: Path, env: dict, timeout_s: int
) -> CommandResult:
    """Run the external agent command in ``cwd``.

    If the template contains ``{prompt}`` it is replaced with the
    shell-quoted prompt text; otherwise the prompt is passed on stdin.
    A program that is an existing file without the executable bit (for
    example a fixture script checked out from GitHub, where the file
    API does not preserve ``+x``) is run through ``sh`` instead of
    failing with "permission denied".
    """
    if "{prompt}" in agent_cmd:
        cmd = agent_cmd.replace("{prompt}", shlex.quote(prompt_text))
        stdin_text = None
    else:
        cmd = agent_cmd
        stdin_text = prompt_text
    found = _first_token(cmd)
    if found:
        _start, _end, program = found
        path = Path(program)
        if (
            "/" in program
            and path.is_file()
            and not os.access(path, os.X_OK)
            and _is_text_script(path)
        ):
            cmd = "sh " + cmd
    return _run_process(cmd, cwd=cwd, env=env, stdin_text=stdin_text, timeout_s=timeout_s)


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
        "TOOL_FAILURE",
        blocked,
        fix,
        "Check the agent command and the output directory, then run again.",
        exit_code=3,
    )
