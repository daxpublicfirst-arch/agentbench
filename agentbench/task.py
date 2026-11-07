"""Task directory loading and validation.

A task directory is::

    task/
      prompt.md    required, the text given to the agent
      setup.sh     optional, run once in the workdir before the agent
      check.sh     required, run after the agent: exit 0 pass, non-zero fail
      files/       optional, copied into the workdir before setup
      meta.json    optional: {"timeout_s": 600, "repeat": 3}

Only ``--task`` and the files inside it are read. Nothing here writes
anywhere, and nothing here logs prompt text or secrets -- only paths.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import BenchError

DEFAULT_TIMEOUT_S = 600
DEFAULT_REPEAT = 1
MAX_TIMEOUT_S = 1800
# plugin.toml limits.input_mb -- a task larger than this is refused
# before any file is read, so a huge task cannot exhaust memory.
INPUT_MB_MAX = 50
MAX_INPUT_BYTES = INPUT_MB_MAX * 1024 * 1024


def _invalid(blocked, fix, if_that_fails="Re-check the task directory and run again."):
    return BenchError("TASK_INVALID", blocked, fix, if_that_fails, exit_code=2)


@dataclass
class Task:
    """A validated task directory."""

    directory: Path
    prompt_path: Path
    check_path: Path
    setup_path: Path | None
    files_dir: Path | None
    prompt_text: str
    timeout_s: int = DEFAULT_TIMEOUT_S
    repeat: int = DEFAULT_REPEAT
    meta: dict = field(default_factory=dict)

    @property
    def input_bytes(self) -> int:
        """Bytes read from the task directory (prompt + files)."""
        total = 0
        for path in self.directory.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        return total


def load_task(task_dir: str | Path) -> Task:
    """Validate ``task_dir`` and return a :class:`Task`.

    Raises :class:`BenchError` with code ``TASK_INVALID`` (exit 2) on any
    validation failure. The executable bit is *not* a hard failure: the
    GitHub file API does not reliably preserve it, so the bench chmods its
    own work copies of ``setup.sh`` / ``check.sh`` before running them
    (see :mod:`agentbench.run`). Existence as a regular file is what is
    validated here.
    """
    directory = Path(task_dir)
    if not directory.is_absolute():
        directory = (Path.cwd() / directory).resolve()
    if not directory.is_dir():
        raise _invalid(
            f"Task directory does not exist: {directory}",
            "Pass --task with the path of an existing task directory.",
        )

    # Size gate first, before reading any file: a task over the
    # plugin.toml input limit is refused, not loaded into memory.
    total_bytes = 0
    for path in directory.rglob("*"):
        if path.is_file():
            try:
                total_bytes += path.stat().st_size
            except OSError:
                pass
    if total_bytes > MAX_INPUT_BYTES:
        raise _invalid(
            f"Task is too large: {total_bytes} bytes "
            f"(limit {MAX_INPUT_BYTES} bytes / {INPUT_MB_MAX} MB).",
            "Split the task or remove large files from the task directory.",
        )

    prompt_path = directory / "prompt.md"
    if not prompt_path.is_file():
        raise _invalid(
            f"Task is missing prompt.md: {prompt_path}",
            "Add a prompt.md file to the task directory.",
        )
    try:
        prompt_text = prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _invalid(
            f"Task prompt.md cannot be read: {prompt_path}",
            "Make prompt.md a readable UTF-8 text file.",
        ) from exc
    if not prompt_text.strip():
        raise _invalid(
            f"Task prompt.md is empty: {prompt_path}",
            "Write the task text into prompt.md.",
        )

    check_path = directory / "check.sh"
    if not check_path.is_file():
        raise _invalid(
            f"Task is missing check.sh: {check_path}",
            "Add a check.sh file to the task directory; it must exit 0 on pass.",
        )

    setup_path = directory / "setup.sh"
    if not setup_path.is_file():
        setup_path = None
    files_dir = directory / "files"
    if not files_dir.is_dir():
        files_dir = None

    timeout_s = DEFAULT_TIMEOUT_S
    repeat = DEFAULT_REPEAT
    meta: dict = {}
    meta_path = directory / "meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise _invalid(
                f"Task meta.json is not valid JSON: {meta_path}",
                'Fix meta.json so it parses as JSON, for example {"timeout_s": 600}.',
            ) from exc
        if not isinstance(meta, dict):
            raise _invalid(
                f"Task meta.json must be a JSON object: {meta_path}",
                'Make meta.json an object, for example {"timeout_s": 600, "repeat": 3}.',
            )
        if "timeout_s" in meta:
            try:
                timeout_s = int(meta["timeout_s"])
            except (TypeError, ValueError) as exc:
                raise _invalid(
                    "Task meta.json timeout_s must be an integer number of seconds.",
                    "Set timeout_s to an integer, for example 600.",
                ) from exc
            if timeout_s <= 0 or timeout_s > MAX_TIMEOUT_S:
                raise _invalid(
                    f"Task meta.json timeout_s out of range: {timeout_s}",
                    f"Set timeout_s between 1 and {MAX_TIMEOUT_S}.",
                )
        if "repeat" in meta:
            try:
                repeat = int(meta["repeat"])
            except (TypeError, ValueError) as exc:
                raise _invalid(
                    "Task meta.json repeat must be an integer.",
                    "Set repeat to an integer, for example 3.",
                ) from exc
            if repeat < 1:
                raise _invalid(
                    f"Task meta.json repeat out of range: {repeat}",
                    "Set repeat to 1 or more.",
                )

    return Task(
        directory=directory,
        prompt_path=prompt_path,
        check_path=check_path,
        setup_path=setup_path,
        files_dir=files_dir,
        prompt_text=prompt_text,
        timeout_s=timeout_s,
        repeat=repeat,
        meta=meta,
    )
