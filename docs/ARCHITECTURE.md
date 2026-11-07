# Architecture: agentbench

Pure Python 3.11+, standard library only at runtime. No runtime
dependencies by design (see `docs/DECISIONS.md`).

## Module map

```
agentbench/
  __init__.py   __version__, BenchError (the one error type)
  cli.py        argparse front end, --request envelope, self-test,
                doctor, telemetry (one JSON line to stderr per run)
  task.py       Task loading + validation (size gate, meta.json)
  agent.py      agent execution, token parsing, shell helpers
  run.py        run_benchmark: arms x repeats, workdir setup,
                report writing (atomic run.json + runs/<run_id>.json)
  report.py     load_report, aggregate, compare_reports,
                find_run_reports, render_table
plugin.toml     Arabica plugin (v1) declaration
schema/         JSON Schemas: request, response, error, telemetry
fixtures/       self-test fixtures (pass, fail, tokens, timeout,
                path, claude-json, claude-stream, pi-json)
tests/          pytest suite (45 tests, coverage >= 85%)
docs/           DESIGN.md, ARCHITECTURE.md (this file), DECISIONS.md
```

## Data flow

```
task dir ──> load_task ──> Task(prompt, check, setup, meta, input_bytes)
                                  │
agent cmd ──> run_benchmark ──> per arm/repeat:
                                  │  build workdir (files/ + setup.sh)
                                  │  run_agent (prompt on stdin, timeout)
                                  │  run_script(check.sh) -> pass?
                                  │  parse_tokens(stdout) -> tokens
                                  ▼
                            report dict ──> run.json (atomic)
                                           runs/<run_id>.json (copy)
                                  │
compare / report ──> find_run_reports ──> aggregate / render_table
```

## Invariants

- **The bench never writes outside `--out`.** Task directories are
  read-only inputs; the only writes are under `--out`. Verified by
  the end-to-end test (the task directory is unchanged after a run).
- **The check is the only judge of pass/fail.** Agent exit codes
  are recorded, not trusted: `pass = check_exit == 0 and not
  timed_out and setup ok`.
- **Tokens are never guessed.** Missing usage means `tokens: null`.
  A run with `tokens: null` still counts for pass rate; medians skip
  nulls.
- **Errors are loud and structured.** Every failure path raises
  `BenchError` with `code`, `blocked`, `fix`, `if_that_fails` and an
  exit code; `main` prints exactly one JSON object to stderr.
- **Deterministic output.** Report JSON uses sorted keys; `compare`
  and `report` sort the reports they read, so the same inputs give
  the same outputs.
- **Self-test never mutates the checkout.** Fixtures are copied to a
  temp directory before running, and no fixture file is chmodded in
  place (a 0.1.0 bug did exactly that; fixed in `3cee013`).

## Environment model

- `setup.sh` and `check.sh` run with a **clean environment**
  (`PATH` only, via `sh`). The task must not depend on the caller's
  environment to set up or to check.
- The **agent inherits the caller's environment** (except `PATH`,
  which the `with` arm modifies). Real agents need their API keys
  and config, and the bench has no business stripping them. The
  trade-off is documented in `docs/DECISIONS.md`.
- The `with` arm prepends the tool directory to `PATH`; the
  `without` arm leaves `PATH` alone. Nothing else differs between
  the arms.

## Shell model

- Agent commands are shell templates: the first token is the
  program, the rest is passed through, `{prompt}` is replaced by
  the prompt file path. A relative agent path is resolved against
  the **caller's** current directory before the runner changes into
  the work directory (a 0.1.0 bug resolved it against the work
  directory, so the agent was never found).
- Scripts are run through `sh` when they are text files without the
  executable bit, because a checkout from GitHub loses `+x` on
  pushed files. A bare program name is looked up on `PATH`.
- Timeouts kill the whole process group (`start_new_session` +
  `os.killpg`), so a runaway agent cannot leave orphan children
  running after the bench gives up.

## Testing

- `make test` -- pytest with coverage (`--cov-fail-under=85`).
- `make lint` -- `compileall` + `ruff check` + `ruff format --check`.
- `make check` -- lint + test + self-test + doctor.
- `make repro-check` -- build twice and require byte-identical
  wheel and sdist.
- `make dist` -- build with `SOURCE_DATE_EPOCH` from the head
  commit, write `dist/SHA256SUMS`.
- The suite runs on Python 3.11 and 3.12 (CI matrix). The 3.11
  interpreter in the cloud VM comes from the deadsnakes PPA; see
  `docs/DECISIONS.md`.
