# agentbench

Run one task with and without a tool, then report pass, tokens, calls and
wall time. A standalone CLI for the `arabica-plugin-v1` protocol. It does
not know about any harness, product, ticket, or model provider: the agent
command is external, passed with `--agent`, and this tool never calls a
model or a provider API itself.

## Example

```sh
pip install -e .

# A task directory: prompt.md (required), check.sh (required),
# setup.sh and files/ (optional), meta.json (optional).
agentbench run \
  --task ./task \
  --agent 'claude -p --output-format json' \
  --with ./bin/example-tool \
  --without \
  --repeat 3 \
  --out ./runs \
  --json
```

Example output (one JSON object):

```json
{"ok": true, "task": "/abs/task",
 "summary": {
   "with": {"pass_rate": 0.67, "tokens_median": 1234, "wall_median_s": 42.1, "runs": 3},
   "without": {"pass_rate": 0.33, "tokens_median": 2345, "wall_median_s": 55.0, "runs": 3}
 },
 "runs": [
   {"arm": "with", "repeat": 1, "pass": true, "exit": 0, "wall_s": 40.2,
    "tokens": 1200, "check": "ok",
    "stdout": "/abs/out/with/1/stdout.log", "stderr": "/abs/out/with/1/stderr.log"}
 ]}
```

Then compare two reports, or report on a whole output directory:

```sh
agentbench compare --a ./runs-a/run.json --b ./runs-b/run.json --json
agentbench report --out ./runs --json
agentbench self-test
agentbench doctor
agentbench --version   # agentbench 0.1.0
```

## Commands

| Command | What it does |
| --- | --- |
| `run --task <dir> --agent <cmd> --with <path> [--without] --repeat <n> --out <dir>` | Run the task per arm / repeat, write `<out>/run.json` |
| `compare --a <run.json> --b <run.json>` | Print the delta (a minus b) |
| `report --out <dir>` | Table (or JSON with `--json`) over all `run.json` files under `--out` |
| `self-test` | Run every fixture in `fixtures/` with a fake agent; exit 0 only if all match |
| `doctor` | Check Python (>= 3.11), the agent command's presence, and the output directory |
| `version` | Print `agentbench 0.1.0` |

Every command accepts `--json`. With `--json` it writes one JSON object
to stdout. `run` and `compare` write JSON either way; `report`,
`self-test`, `doctor` and `version` print human-readable text without
`--json`. One telemetry JSON line is written to stderr per invocation
(see `schema/telemetry.json`).

An adapter envelope may be used instead of flags:

```sh
agentbench --request ./request.json
```

```json
{"protocol": "arabica-plugin-v1", "command": "run",
 "args": {"task": "/abs/task", "agent": "claude -p --output-format json",
          "with": "/abs/bin/example-tool", "without": true, "repeat": 3,
          "out": "/abs/runs"}}
```

## Task directory

```text
task/
  prompt.md          required, the text given to the agent
  setup.sh           optional, run once in the workdir before the agent
  check.sh           required, run after the agent: exit 0 pass, non-zero fail
  files/             optional, copied into the workdir before setup
  meta.json          optional: {"timeout_s": 600, "repeat": 3}
```

`check.sh` may print one line explaining the result; the bench captures
its first line as `check`.

## How a run works

1. Validate the task (`prompt.md` and `check.sh` must exist).
2. For each arm (`with`, `without`) and each repeat: create
   `<out>/<arm>/<n>/work`, copy `files/` into it (plus `setup.sh` /
   `check.sh`, chmodded executable in the work copy), run `setup.sh`
   with a clean environment plus `PATH`, run the agent command in the
   workdir, run `check.sh`, and record wall seconds and tokens.
3. The prompt is passed on stdin, unless the `--agent` template
   contains `{prompt}`, in which case it is substituted (shell-quoted).
4. `--with <path>` prepends `<path>` to `PATH` for the agent command.
   With `--with` given, both arms run by default; `--without` makes
   the without-arm explicit. With no `--with`, only the `without` arm runs.
5. `timeout_s` (from `meta.json`, or `--timeout-s`) is enforced and the
   whole process group is killed on timeout; the run records
   `timed_out: true` and `pass: false`.
6. Tokens are parsed only from a JSON `usage` object in the agent's
   stdout (`input_tokens + output_tokens`, or `total_tokens`). A missing
   token count is `null`, never zero and never guessed.

## Exit codes and errors

- `0` success; `1` at least one run failed (not an error);
  `2` usage or task error; `3` tool failure.
- On failure, one JSON object is printed and the exit is non-zero:

```json
{"ok": false, "error": {"code": "TASK_INVALID", "blocked": "...",
 "fix": "...", "if_that_fails": "..."}}
```

## Setup and tests

Requires Python 3.11+; no other runtime dependencies.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"   # or: pip install -e . ; pip install pytest build
make check                # lint + full tests + self-test + doctor
make dist                 # wheel + sdist in dist/, with dist/SHA256SUMS
```

## Assumptions and corrections vs. the original draft

The task prompt was a draft, and a few of its instructions conflicted
with each other or with the repo. The choices made here, and why:

- **Target repo is `agentbench`, not `logreduce`.** The session prefix
  named `agentbench` in one place and `logreduce` in others; all work
  and commits go to `daxpublicfirst-arch/agentbench`, branch `main`.
- **`plugin.toml` example uses `./bin/example-tool`, not `./bin/logreduce`.**
  The draft example named another tool, which would violate the draft's
  own "no product name" constraint. `source` is the real repo URL.
- **`--without` default.** The draft says omitting `--without` still
  runs both arms, so with `--with` given both arms run by default.
- **`report` output.** A table *and* one JSON object cannot both be
  stdout. Without `--json` it prints a table; with `--json` it prints
  one JSON object.
- **Executable bit.** The GitHub file API does not reliably preserve
  `+x`, so the bench chmods its work copies of `setup.sh`/`check.sh`
  instead of hard-failing when the source task lacks the bit.
- **Clean environment.** Only `setup.sh`/`check.sh` get a clean env
  (`PATH` only). The agent inherits the caller's env so a real agent
  can still see its own credentials; stripping it would make real
  model agents unusable.
- **Release tag and CI.** There is no GitHub MCP tool to create a tag
  or a release, so `v0.1.0` must be created in the GitHub UI (or with a
  git client) from `main` after the code lands; the wheel/sdist and
  their `sha256sum` are built with `make dist`. The same app also
  cannot write `.github/workflows/ci.yml` (403, it lacks the
  `workflows` permission), so the CI file in this repo must be added
  from your own checkout or the web UI if the push of it is refused.
  The CI itself is standard: `pytest`, `compileall`, `self-test`,
  `doctor`, and a wheel build on Python 3.11 and 3.12.

## Release v0.1.0 (built 2026-11-08)

Built with `make dist` from the `main` head:

```text
bde5b4a7bc7234969d1c5c685ca04d59a826b37f6b2d8c7e991dc761bf000b4b  agentbench-0.1.0-py3-none-any.whl
029f593e03d40f5bb0571d7df962073e9bc66ff3f253f31457240348672ab024  agentbench-0.1.0.tar.gz
```

The wheel and tarball are build artifacts, not committed to git.

## Limitations

- Token parsing only understands a top-level JSON `usage` object in
  the agent's stdout. Agents that report usage in another shape (or
  only in stderr, or in a log file) report `tokens: null`.
- `setup.sh` / `check.sh` are run through `sh`; they must be
  POSIX-shell scripts.
- No MCP server is included, by design: the harness uses the CLI
  through an adapter.
