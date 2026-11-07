# Decisions: agentbench

Architecture Decision Records. Each entry states the decision, the
reasoning, the alternatives considered, and the trade-off. A local
agent reading a fresh clone should be able to reconstruct *why* the
code looks the way it does without asking the maintainer.

## 1. Standard library only at runtime

**Decision:** no runtime dependencies; `pyproject.toml` declares
only `dev` extras (`pytest`, `pytest-cov`, `ruff`, `build`).

**Why:** the tool's job is to run other programs and count tokens.
Every dependency is a supply-chain risk and a version to pin, and
the tool must install in seconds on a bare interpreter. The
standard library already has everything it needs (`subprocess`,
`json`, `pathlib`, `statistics`).

**Alternative:** add `click` / `pydantic` / `typer`. Rejected: nicer
CLI ergonomics, but a heavier install and a larger attack surface
for a tool whose whole point is to be a neutral measuring device.

**Trade-off:** hand-rolled `argparse` and hand-written JSON Schema
validation are more code to maintain than a framework would be.

## 2. The agent is external; the tool never calls a model

**Decision:** the agent is a shell command passed with `--agent`.
The tool never imports an SDK, never holds an API key, and never
makes a network call of its own (the `network = true` permission in
`plugin.toml` is for the *agent*, not the bench).

**Why:** the bench must stay neutral between agents. If it knew
about one agent's API, the comparison would be biased toward that
agent, and every new agent would need a code change. A shell
command is the narrowest interface that every agent already has.

**Alternative:** bundle a Claude Code / pi client. Rejected: it
would tie the tool to one vendor's auth, pricing, and output format.

## 3. Clean environment for setup/check, inherited environment for the agent

**Decision:** `setup.sh` and `check.sh` run with a clean environment
(`PATH` only). The agent inherits the caller's environment, with
`PATH` modified for the `with` arm.

**Why:** setup and check are part of the *task definition*, so they
must be reproducible: the same task must set up and check the same
way on any machine. The agent is a *real program* the user runs;
stripping its environment would break API keys, config files, and
proxy settings, and would make the bench's numbers unrepresentative
of real use.

**Trade-off:** a task whose check depends on a caller environment
variable will fail, by design. That is a task bug, and the error
tells the user to put the dependency in `setup.sh` or `files/`.

## 4. Token parsing: shapes, cache buckets, and the combining rule

**Decision:** `parse_tokens` (in `agent.py`) understands the
documented output shapes of the two agent families the tool is
meant to measure, and combines them by shape:

- Claude Code `--output-format json`: one result envelope with
  `usage: {input_tokens, output_tokens, cache_creation_input_tokens,
  cache_read_input_tokens}` and an optional per-model `modelUsage`
  breakdown. Sources: the Claude Code CLI reference
  (<https://github.com/memgrafter/flatmachines/blob/HEAD/docs/claude-code-cli-reference.md>)
  and the headless-mode research
  (<https://github.com/marcomd/disputatio/blob/HEAD/research/claude-code-headless.md>,
  <https://github.com/unisoma/aishell/blob/HEAD/docs/research/claude-code-headless-cli.md>).
- Claude Code `stream-json`: NDJSON; the last line is the result
  envelope, earlier lines are per-message events with
  `message.usage`.
- pi `--mode json` / `--print --mode json`: NDJSON event stream;
  every `message_end` event carries that turn's
  `usage: {input, output, cacheRead, cacheWrite}`, and the harness
  **sums** usage across turns. Source: the pi harness contract
  (<https://junior.mchep.dev/agent_backends/pi/>) and the multi-CLI
  token research
  (<https://github.com/Magnus-Gille/tokenatlas/blob/HEAD/research/multi-cli-support.md>),
  which states "usage = Σ per-turn usage (input+cacheRead+cacheWrite
  / output)" and "cache reads/writes count as input".
- pi-agent `--json`: `{"type": "token_usage", "inputTokens",
  "outputTokens", "totalTokens", "cacheReadTokens",
  "cacheWriteTokens"}` events.

**Why cache buckets count as input:** in Anthropic-style usage,
`input_tokens` excludes cache reads and cache writes, so
`input_tokens + output_tokens` understates what the run consumed.
The pi documentation says the same thing in its own words
("cache reads/writes count as input"). The total is therefore
input + cache read + cache creation + output.

**Why reasoning tokens are not added:** reasoning tokens are a
subset of `output_tokens` (the tokenatlas research states this
explicitly), so adding them would double-count.

**Why the combining rule is "last aggregate wins, otherwise sum
per-turn":** the two families disagree. Claude's final envelope is
an aggregate of the whole run; earlier per-message usages are
partial and must not be added to it. Pi has no aggregate: each
`message_end` is one turn, and the total is the sum. The parser
distinguishes them by shape (pi's `usage` uses bare `input` /
`output` keys; Claude's uses `input_tokens` / `output_tokens`),
never by agent name, so it keeps working if either agent changes
its command name or if a new agent reuses one of these shapes.

**Why `modelUsage` is only used when there is no top-level
`usage`:** the two describe the same run. Counting both would
double-count.

**Trade-off:** shapes are a whitelist. An agent that reports tokens
in a shape not listed here yields `tokens: null` -- honest, but
less useful than a guess. Adding a new shape is a deliberate
change: add the shape, a fixture, and a unit test, and document the
source here.

## 5. Reproducible builds: hatchling, not setuptools + SOURCE_DATE_EPOCH

**Decision:** the build backend is `hatchling`
(`[build-system] requires = ["hatchling"]`), and `make dist` sets
`SOURCE_DATE_EPOCH` to the head commit's timestamp anyway.

**Evidence (measured 2026-11-08 in this VM, setuptools from the
`build` isolated environment):**

- setuptools + `SOURCE_DATE_EPOCH=1760000000`: wheel byte-identical
  across two builds (`abda1a17…`), sdist **different**
  (`3d1adbc4…` vs `146f78b4…`). Setting the variable is not
  sufficient for sdists with setuptools; this matches the
  `setuptools-reproducible` project's own statement
  (<https://pypi.org/project/setuptools-reproducible/0.1/>).
- hatchling, same setup: wheel **and** sdist byte-identical across
  two builds (`0e8da944…` / `711ec76c…`), editable install works,
  pytest 22/22 passes.

**Alternatives considered:**

- Keep setuptools and add a normalize script (canonicalize tar
  member ordering, timestamps, uid/gid, gzip header, as in the
  cellucid release process). Rejected: more custom code to maintain
  for a problem a standard backend already solves.
- `setuptools-reproducible` as the backend. Rejected: a niche
  wrapper around setuptools; hatchling is a mainstream backend with
  the same result and no extra moving parts.
- Accept non-reproducible sdists and document it. Rejected: the
  whole point of publishing hashes is that anyone can re-derive
  them; a hash that changes on every build is not verifiable.

**Trade-off:** hatchling's sdist includes more files than
setuptools' did (it packages the whole project tree, including
fixtures and docs). That is acceptable -- arguably better, since
the fixtures travel with the source -- but the sdist is larger.
`make repro-check` builds twice and fails the build if the hashes
differ, so a future regression is caught, not silently shipped.

## 6. Python 3.11 is tested, via a standalone CPython build

**Decision:** CI tests on 3.11 and 3.12. The 3.11 interpreter in
the cloud VM is CPython 3.11.18 from the python-build-standalone
project (now at astral-sh/python-build-standalone), extracted to
`~/workspace/python311` and used only for testing. It is never
installed over the system `/usr/bin/python3`.

**Why:** the tool declares `requires-python = ">=3.11"`, so the
lowest supported version must actually be exercised; testing only
on 3.12 would leave 3.11 unproven. Ubuntu 24.04 ships Python 3.12,
so 3.11 has to come from somewhere else.

**What was tried first (and failed):** the deadsnakes PPA, the
usual source for this (`sudo add-apt-repository ppa:deadsnakes/ppa
&& sudo apt install python3.11 python3.11-venv`). It failed in
this VM: `add-apt-repository` died with `PermissionError` writing
`/etc/apt/sources.list.d/ubuntu.sources` (that file is owned by
`nobody:nogroup`, mode 644, and the tooling here cannot overwrite
it), leaving behind an empty `deadsnakes-ubuntu-ppa-noble.sources`.
So the PPA route is documented here as *not* working in this
environment, and the standalone build was used instead.

**Trade-off (stated plainly):** a standalone build is a binary
from a third party, not a distribution package. Its integrity was
checked by the download size matching the release metadata exactly
(30,574,046 bytes) and by gzip validity on extraction -- there was
no separate `.sha256` asset for that release, which is a weaker
check than a published hash and is recorded here as such. The 3.11
interpreter is a *test* interpreter in a throwaway VM, never a
production runtime, and it runs nothing except the test suite. If
a project's policy forbids third-party binaries, the alternative
is to compile CPython 3.11 from source (python.org) -- slower,
but fully under the project's control.

## 7. Ruff + coverage gate

**Decision:** `make lint` runs `compileall`, `ruff check`, and
`ruff format --check` (line length 100, target `py311`); `make
test` runs pytest with `--cov-fail-under=85`. Measured coverage at
0.1.1 is 86.55% (45 tests).

**Why:** the 0.1.1 work added `ruff` because the retest report
found that `make lint` silently degraded to `compileall` when ruff
was not installed -- a lint step that does nothing is worse than no
lint step, because it looks like a check. The coverage gate exists
so the next change cannot silently drop test coverage; 85% was
chosen because it is just below what the suite actually achieves
(86.55%), so the gate bites on real regressions without forcing
tests for the sake of tests.

**Trade-off:** a coverage number is not a quality measure. The
remaining uncovered lines are mostly error paths that are hard to
trigger (e.g. `OSError` while writing output) and the `__main__`
guard. Chasing 100% would add brittle tests; the gate is set where
it is on purpose.

## 8. Concurrency: run_id + preserved copies, distinct --out per run

**Decision:** every run gets a `run_id` (uuid4 hex). `run.json` is
written atomically (temp file + `os.replace`), and the full report
is also preserved as `runs/<run_id>.json`. `report` reads both
`run.json` files and `runs/*.json`.

**Why:** two runs to the same `--out` will race on `run.json`
(last writer wins). An atomic write at least guarantees no
half-written file, and the preserved copy guarantees no run's
report is lost. The per-arm/per-repeat logs still share
`<out>/<arm>/<n>/` by design (that layout is the documented spec),
so concurrent runs to the same `--out` can clobber each other's
logs -- the documented recommendation is a distinct `--out` per
concurrent run.

**Alternative:** a file lock around the whole run. Rejected: a lock
would serialize concurrent runs and add deadlock risk for a
marginal gain; the preserved-copy design keeps runs independent
and makes the race visible (two files in `runs/`) instead of
hiding it behind a lock.

## 9. Limits are enforced, not just declared

**Decision:** the 50 MB input limit (`plugin.toml`
`limits.input_mb`) and the 1800 s timeout cap
(`limits.timeout_s`) are enforced in code, not just written in
`plugin.toml`.

**Why:** the 0.1.0 retest found that `plugin.toml` declared both
limits while the code enforced neither -- a task larger than 50 MB
was loaded into memory, and `--timeout-s 99999` was accepted. A
declared-but-unenforced limit is a false promise to whoever reads
the plugin file. The size check runs before any file is read
(`load_task` sums `stat().st_size` first), so a huge task cannot
exhaust memory before being refused.

**Trade-off:** the limits are hard, with no override flag. If a
legitimate task needs more than 50 MB or 1800 s, the limit belongs
in `plugin.toml` *and* in `task.py` together -- a one-line change in
both places, deliberately coupled so they cannot drift apart again.

## 10. Platform: Linux, macOS in principle, Windows not supported

**Decision:** the runner needs a POSIX shell (`sh`) and process
groups (`start_new_session` + `os.killpg`). `doctor` reports the
platform and fails on Windows.

**Why:** timeouts must kill the agent *and its children* -- an
agent that spawns a background process and then hangs would
otherwise outlive the bench. POSIX process groups are the mechanism
for that; Windows has a different one
(`CREATE_NEW_PROCESS_GROUP`) that this codebase does not implement.

**Status (honest):** tested on Linux only. macOS is POSIX and
should work, but has not been verified here; the `doctor` platform
check reports it as POSIX without claiming verification.

## 11. No tag / Release / CI push from this tooling

**Decision:** releases (git tag, GitHub Release) and the CI file
are out of scope for the Muse GitHub App. The CI workflow exists
in the working copy (`.github/workflows/ci.yml`) and is kept in
sync, but the App cannot push it: pushing a workflow file returns
`403 Resource not accessible by integration` (the App lacks the
`workflows` permission), and the App has no create-tag/release
tool. This was verified for this repo on 2026-11-08.

**Why it stays this way:** the maintainer asked for `main`-only
pushes via the App and no browser / web-login step. A tag or
Release therefore has to come from the maintainer's own checkout
or the web UI, when he asks for it.

## 12. No live model call in the test suite

**Decision:** the test suite never calls a real model. Real-agent
coverage comes from fixtures (`claude-json`, `claude-stream`,
`pi-json`) that reproduce the *documented* output shapes of
Claude Code and pi, byte-for-byte in the fields the parser reads.

**Why:** a live model call needs credentials, costs money, is
non-deterministic (so it cannot be a regression test), and would
make `make check` unusable offline. The fixtures are the honest
substitute: they prove the parser handles the real shapes, and
each fixture cites its source in decision 4 above.

**What is still unproven:** that a live `claude -p
--output-format json` run actually produces the envelope the docs
describe (docs and CLI versions drift), and that a live pi run's
per-turn usage sums to the session total. Both need a one-off
manual run with real credentials; neither can be automated here.

## 13. Version 0.1.1 (not 0.1.0)

**Decision:** the retest follow-up work shipped as 0.1.1, not as a
silent patch to 0.1.0.

**Why:** 0.1.0 was already built, committed, and reported. The
follow-up changed behaviour (limits are now enforced, token
parsing counts cache buckets, builds are reproducible, the build
backend changed) -- that is a new release, and pretending it is
still 0.1.0 would make the published 0.1.0 hashes and the
reported test results refer to two different things.

## 14. Scripts run through `sh` when they lack the executable bit

**Decision:** `run_script` (setup/check) and `run_agent` invoke a
text script through `sh` if it is not executable.

**Why:** files pushed to GitHub through the API lose the
executable bit, so a checkout of this repo has every `.sh` at
644. A tool that refused to run its own fixtures on a fresh clone
would be broken by its own distribution channel. The `sh`
fallback makes the tool work with the files as they actually
arrive, not as they were on the author's machine.

## 15. A relative agent path is resolved against the caller's directory

**Decision:** `resolve_agent_program` turns a relative agent path
into an absolute path using the caller's current directory, before
the runner changes into the work directory.

**Why:** a user types a path relative to where *they* are standing.
The runner's work directory is an implementation detail the user
never sees. Resolving against the work directory (the 0.1.0 bug)
meant the agent was "not found" and, worse, a fixture whose check
always passes still reported `pass=true` -- the failure was
invisible. The fix is in `3cee013`.

## 16. `report` reads the preserved `runs/*.json` copies

**Decision:** see decision 8. `find_run_reports` collects
`run.json` at any depth plus `runs/*.json`, deduplicated and
sorted, so `report` and `compare` see every run that was preserved
even if `run.json` was overwritten by a later run.

**Trade-off:** a report over `--out` after two runs to the same
directory now aggregates both runs. That is the intended semantic
("everything under this `--out`"), but it means `--out` is a
collection, not a single run -- keep one `--out` per comparison
if that is not what you want.

## 17. The sdist must never package a virtualenv

**Decision:** `.gitignore` ignores `.venv*/` (not just `.venv/`),
and `pyproject.toml` has an explicit hatchling `exclude` list for
venvs, build output, and coverage files.

**Why:** during the 0.1.1 retest, `make repro-check` failed in a
clean clone with `ERROR 'agentbench-0.1.1/.venv312/bin/python3' is
a link to an absolute path`. The clone's test venv (`.venv312`)
lived inside the project directory, was not covered by
`.gitignore`, and hatchling tried to put it in the sdist -- and
refuses symlinks to absolute paths. A build that breaks because a
developer happened to name their venv `.venv312` instead of
`.venv` is a trap, not a user error.

**Trade-off:** the exclude list is a second copy of the ignore
rules. It is kept deliberately: `.gitignore` is about the working
tree, the hatchling list is about the artifact, and the two can
drift without either one failing loudly. The `repro-check` target
is the backstop -- it builds from the real project directory, venv
and all.
