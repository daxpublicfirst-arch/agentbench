"""The ``run`` command: one task, twice (with / without a tool).

For each arm (``with``, ``without``) and each repeat:

1. create ``<out>/<arm>/<n>/work``;
2. copy ``files/`` into ``work/``, plus ``setup.sh`` / ``check.sh`` (the
   work copies are chmodded executable -- see task.py);
3. run ``setup.sh`` in ``work/`` with a clean environment plus ``PATH``;
4. run the agent command in ``work/`` (prompt on stdin, or substituted
   for ``{prompt}`` in the template);
5. enforce ``timeout_s``; kill the process group on timeout;
6. run ``check.sh`` in ``work/``; record exit code and first line;
7. record wall seconds and parse tokens from the agent's stdout.

Only ``--task`` and the files inside it are read. Only ``--out`` is
written to. The report is deterministic for the same run data: keys are
sorted on output and runs are in arm / repeat order.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import time
from pathlib import Path

from . import BenchError
from .agent import parse_tokens, resolve_agent_program, run_agent, run_script
from .task import Task


def _median(values: list) -> float | int | None:
    if not values:
        return None
    med = statistics.median(values)
    if isinstance(med, float) and med.is_integer():
        return int(med)
    return med


def summarize_runs(runs: list[dict]) -> dict:
    """Aggregate one arm's runs: pass rate, token / wall medians, count."""
    if not runs:
        return {"pass_rate": 0.0, "tokens_median": None,
                "wall_median_s": None, "runs": 0}
    passes = sum(1 for r in runs if r.get("pass"))
    tokens = [r["tokens"] for r in runs if r.get("tokens") is not None]
    walls = [r["wall_s"] for r in runs if r.get("wall_s") is not None]
    wall_median = _median(walls)
    if isinstance(wall_median, float):
        wall_median = round(wall_median, 3)
    return {
        "pass_rate": round(passes / len(runs), 4),
        "tokens_median": _median(tokens),
        "wall_median_s": wall_median,
        "runs": len(runs),
    }


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_one_repeat(task: Task, *, agent_cmd: str, arm: str,
                   repeat: int, out_dir: Path, with_path: str | None) -> dict:
    """Run a single (arm, repeat) and return its run record."""
    run_dir = out_dir / arm / str(repeat)
    work = run_dir / "work"
    work.mkdir(parents=True, exist_ok=True)

    # Copy task files into the workdir. setup.sh / check.sh are copied
    # too (they run *in* the workdir) and chmodded executable in the copy.
    if task.files_dir is not None:
        shutil.copytree(task.files_dir, work, dirs_exist_ok=True)
    for source in (task.setup_path, task.check_path):
        if source is not None:
            dest = work / source.name
            shutil.copy2(source, dest)
            try:
                dest.chmod(0o755)
            except OSError:
                pass

    path_value = os.environ.get("PATH", "")
    if arm == "with" and with_path:
        path_value = f"{with_path}{os.pathsep}{path_value}"

    setup_exit: int | None = None
    setup_timed_out = False
    if task.setup_path is not None:
        setup = run_script(work / "setup.sh", cwd=work,
                           path_value=path_value, timeout_s=task.timeout_s)
        setup_exit = setup.exit_code
        setup_timed_out = setup.timed_out
        _write_text(run_dir / "setup_stdout.log", setup.stdout_text)
        _write_text(run_dir / "setup_stderr.log", setup.stderr_text)

    agent_result = None
    timed_out = bool(setup_timed_out)
    if setup_exit not in (None, 0) or setup_timed_out:
        # Setup failed: do not run the agent on a broken workdir.
        agent_exit = None
        tokens = None
        wall_s = 0.0
        _write_text(run_dir / "stdout.log", "")
        _write_text(run_dir / "stderr.log", "")
    else:
        agent_env = dict(os.environ)
        agent_env["PATH"] = path_value
        agent_result = run_agent(agent_cmd, task.prompt_text, cwd=work,
                                 env=agent_env, timeout_s=task.timeout_s)
        agent_exit = agent_result.exit_code
        tokens = parse_tokens(agent_result.stdout_text)
        wall_s = round(agent_result.wall_s, 3)
        timed_out = bool(agent_result.timed_out)
        _write_text(run_dir / "stdout.log", agent_result.stdout_text)
        _write_text(run_dir / "stderr.log", agent_result.stderr_text)

    check_exit: int | None = None
    check_line = ""
    if setup_timed_out:
        check_line = "setup timed out"
    else:
        check = run_script(work / "check.sh", cwd=work,
                           path_value=path_value, timeout_s=task.timeout_s)
        check_exit = check.exit_code
        check_line = _first_line(check.stdout_text) or _first_line(check.stderr_text)
        _write_text(run_dir / "check_stdout.log", check.stdout_text)
        _write_text(run_dir / "check_stderr.log", check.stderr_text)
        if check.timed_out:
            timed_out = True
            if not check_line:
                check_line = "check timed out"

    if timed_out and not check_line:
        check_line = "timed out"

    passed = (
        check_exit == 0
        and not timed_out
        and setup_exit in (None, 0)
        and not setup_timed_out
    )

    return {
        "arm": arm,
        "repeat": repeat,
        "pass": passed,
        "exit": agent_exit,
        "wall_s": wall_s,
        "tokens": tokens,
        "check": check_line,
        "stdout": str((run_dir / "stdout.log").resolve()),
        "stderr": str((run_dir / "stderr.log").resolve()),
        "timed_out": timed_out,
        "setup_exit": setup_exit,
        "check_exit": check_exit,
    }


def run_benchmark(task: Task, *, agent_cmd: str, with_path: str | None,
                  without: bool, repeat: int | None, out_dir: str | Path,
                  timeout_s: int | None = None) -> dict:
    """Run the benchmark and write ``<out>/run.json``.

    Arms: when ``with_path`` is given the default is *both* arms (the
    prompt's own rule: omitting ``--without`` still runs both arms);
    ``--without`` makes that explicit. With no ``--with`` path there is
    only the ``without`` arm. ``repeat`` defaults to the task's
    ``meta.json`` repeat, and ``timeout_s`` (when given) overrides
    ``meta.json``.
    """
    if not agent_cmd or not agent_cmd.strip():
        raise BenchError(
            "TASK_INVALID", "No agent command given.",
            "Pass --agent with the agent command, for example "
            "'claude -p --output-format json'.",
            "Run agentbench doctor to check the agent command.",
            exit_code=2,
        )
    # Resolve a relative agent path now, while cwd is still the
    # caller's directory (the agent later runs inside its workdir).
    agent_cmd = resolve_agent_program(agent_cmd)
    out = Path(out_dir)
    if not out.is_absolute():
        out = (Path.cwd() / out).resolve()
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BenchError(
            "TOOL_FAILURE", f"Output directory cannot be created: {out}",
            "Pass --out with a writable directory.",
            "Check permissions on the parent directory and run again.",
            exit_code=3,
        ) from exc
    if not os.access(out, os.W_OK):
        raise BenchError(
            "TOOL_FAILURE", f"Output directory is not writable: {out}",
            "Pass --out with a writable directory.",
            "Check permissions on the output directory and run again.",
            exit_code=3,
        )

    n_repeat = repeat if repeat is not None else task.repeat
    if n_repeat < 1:
        raise BenchError(
            "TASK_INVALID", f"Repeat must be 1 or more, got {n_repeat}.",
            "Pass --repeat with a positive integer.",
            "Omit --repeat to use the task meta.json repeat.",
            exit_code=2,
        )
    if timeout_s is not None:
        if timeout_s <= 0:
            raise BenchError(
                "TASK_INVALID", f"Timeout must be positive, got {timeout_s}.",
                "Pass --timeout-s with a positive integer number of seconds.",
                "Omit --timeout-s to use the task meta.json timeout_s.",
                exit_code=2,
            )
        task.timeout_s = timeout_s

    if with_path:
        with_abs = Path(with_path)
        if not with_abs.is_absolute():
            with_abs = (Path.cwd() / with_abs).resolve()
        if not with_abs.is_dir():
            raise BenchError(
                "TASK_INVALID", f"--with path does not exist: {with_abs}",
                "Pass --with with the directory containing the tool under test.",
                "Check the tool path and run again.",
                exit_code=2,
            )
        with_path = str(with_abs)
        arms = ["with", "without"]
    else:
        arms = ["without"]
    _ = without  # explicit flag; both arms are already the default with --with

    started = time.monotonic()
    runs: list[dict] = []
    for arm in arms:
        for n in range(1, n_repeat + 1):
            runs.append(run_one_repeat(
                task, agent_cmd=agent_cmd, arm=arm, repeat=n,
                out_dir=out, with_path=with_path,
            ))
    _ = started

    summary = {}
    for arm in arms:
        summary[arm] = summarize_runs([r for r in runs if r["arm"] == arm])

    report = {
        "ok": True,
        "task": str(task.directory.resolve()),
        "summary": summary,
        "runs": runs,
    }
    _write_text(out / "run.json",
                json.dumps(report, sort_keys=True, indent=2) + "\n")
    return report
