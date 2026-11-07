# Design: agentbench

## What this tool is

agentbench answers one question: **does a tool help an agent do a
task better?** It runs one self-contained task twice -- once with the
tool available on `PATH`, once without -- and reports pass/fail,
token usage, and wall time for each run. That is the whole product.
It does not choose tasks, does not grade answers, does not call any
model itself, and does not talk to any harness or product. The agent
command is external, supplied by the caller with `--agent`.

## Concepts

- **Task.** A directory with `prompt.md` (the instruction, required),
  `check.sh` (the pass/fail test, required), optionally `setup.sh`
  (prepares the working directory) and `meta.json`
  (`timeout_s`, `repeat`), and optionally `files/` (seed files copied
  into the working directory). A task is self-contained: everything
  the agent needs is inside the directory, and the check decides
  pass/fail by exit code.
- **Arm.** One side of the comparison: `with` (the tool directory is
  prepended to `PATH`) or `without`. Each arm runs the task `repeat`
  times (default 1, from `meta.json` or `--repeat`).
- **Run.** One agent invocation. A run is `pass` when the check exits
  0, the agent did not time out, and setup succeeded. Tokens come
  only from the agent's stdout (see "Token parsing" below); a missing
  count is `null`, never 0 and never guessed.
- **Report.** `run.json` under `--out`, plus a preserved copy under
  `runs/<run_id>.json`. `compare` diffs two reports, `report`
  aggregates every `run.json` it finds under `--out`.

## Run lifecycle

1. `load_task` validates the task directory: it exists, `prompt.md`
   is non-empty, `meta.json` (if present) is an object with sane
   `timeout_s` (1..1800) and `repeat` (>= 1), and the total size of
   the directory is at most 50 MB (`plugin.toml` `limits.input_mb`).
   The size check happens **before** any file is read, so a huge task
   cannot exhaust memory. A violation is `TASK_INVALID` (exit 2).
2. For each arm and repeat, a fresh working directory
   `<out>/<arm>/<n>/work` is built: `files/` is copied in, then
   `setup.sh` runs (clean environment, `PATH` only). If setup fails,
   the run fails with `setup_exit` recorded.
3. The agent runs in the work directory with the prompt on stdin.
   The agent inherits the caller's environment (so real agents can
   see their API keys), except `PATH`, which the `with` arm
   prepends with the tool directory. The agent is killed, as a whole
   process group, when it exceeds the timeout.
4. `check.sh` runs (clean environment, `PATH` only) and decides
   pass/fail by exit code. The check is the only judge: an agent
   that exits 0 but fails the check does not pass, and an agent that
   exits non-zero but passes the check does pass.
5. Tokens are parsed from the agent's stdout, the run record is
   written (stdout/stderr logs under `<out>/<arm>/<n>/`), and the
   report is written atomically (`run.json` via temp file + rename,
   plus the `runs/<run_id>.json` copy).

## Token parsing

`parse_tokens` accepts JSON stdout in one of the documented shapes
(full rationale in `docs/DECISIONS.md`):

| Shape | Where it comes from | How it combines |
|---|---|---|
| `{"usage": {"input_tokens", "output_tokens", ...}}` | Claude Code `--output-format json`, Anthropic API | Last aggregate wins |
| `{"usage": {"prompt_tokens", "completion_tokens"}}` | OpenAI-style | Last aggregate wins |
| `{"modelUsage": {"model": {"inputTokens", ...}}}` | Claude Code per-model breakdown | Summed, only when no top-level `usage` exists |
| `{"message": {"usage": {...}}}` per line | Claude Code `stream-json` assistant messages | Per-turn (ignored when an aggregate exists) |
| `{"type": "message_end", "usage": {"input", "output", "cacheRead", "cacheWrite"}}` per line | pi `--mode json` | **Summed** across turns |
| `{"type": "token_usage", "inputTokens", "outputTokens", "totalTokens", ...}` | pi-agent `--json` | Summed across events |

Cache buckets (`cache_read_input_tokens`, `cache_creation_input_tokens`,
`cacheRead`, `cacheWrite`, and their camelCase forms) count as input:
in Anthropic-style usage `input_tokens` excludes cache reads and
cache writes, so the faithful total is input + cache + output.
Reasoning tokens are a subset of output and are deliberately **not**
added on top -- that would double-count. A `total_tokens` /
`totalTokens` field, when present, wins over summing buckets.

Combining rule: if any whole-run aggregate exists, the **last** one
wins (a Claude stream's final result envelope is the aggregate, and
earlier per-message usages are partial). Otherwise per-turn usages
are **summed** (pi reports one `usage` per turn). This is the only
place where two agent families disagree, and the parser resolves it
by shape, not by agent name -- the tool never sniffs which agent is
running.

## Errors and exit codes

- `0` -- success (for `run`: every run passed; a failing task is
  exit 1, not 0).
- `1` -- at least one run failed its check (run), or self-test
  failed.
- `2` -- the request was invalid: bad task, bad envelope, missing
  report. The error is one loud JSON object to stderr:
  `{"ok": false, "error": {"code", "blocked", "fix",
  "if_that_fails"}}`. The `blocked` field says what is wrong,
  `fix` says the most likely repair, `if_that_fails` the next step.
- `3` -- the tool itself failed unexpectedly (`TOOL_FAILURE`); the
  same JSON shape is emitted, never a bare traceback.

## Determinism

Report JSON is written with sorted keys and a fixed shape, so the
same runs produce the same bytes. Wall times and run IDs are the
only non-deterministic fields in a report. Builds are reproducible:
see `docs/DECISIONS.md` ("Reproducible builds").

## Limits

- Input: one task, at most 50 MB total (enforced).
- Timeout: at most 1800 s per run (enforced for `meta.json` and for
  `--timeout-s`).
- Concurrency: runs to *different* `--out` directories are
  independent. Two runs to the *same* `--out` race on `run.json`
  (last writer wins, each run's full report is preserved under
  `runs/<run_id>.json`), and can clobber each other's per-repeat
  logs. Use a distinct `--out` per concurrent run.
- Platform: Linux, and in principle macOS (POSIX shell and process
  groups). Windows is not supported: the runner needs `sh` and
  `os.killpg`. `doctor` reports the platform and fails on Windows.
- Agent stdout is captured in memory, so an agent that prints
  gigabytes will use gigabytes of memory. The 50 MB input limit
  does not bound agent output; do not point the bench at an agent
  that streams unbounded output.
