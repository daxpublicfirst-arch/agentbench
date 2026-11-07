# Changelog

All notable changes to agentbench will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-11-08

### Added

- Real-agent token parsing: Claude Code `--output-format json` /
  `stream-json` result envelopes (cache buckets counted as input,
  per-model `modelUsage` fallback, last aggregate wins) and pi
  `--mode json` per-turn `usage` events (summed across turns) plus
  pi-agent `token_usage` events. New fixtures `claude-json`,
  `claude-stream`, `pi-json` (self-test now 8/8).
- Limits are enforced, not just declared: task input capped at
  50 MB (`plugin.toml` `limits.input_mb`, checked before any file
  is read) and `--timeout-s` capped at 1800 s.
- Every run gets a `run_id`; `run.json` is written atomically and
  the full report is preserved as `runs/<run_id>.json`, which
  `report` also reads.
- `doctor` now reports the platform and fails on Windows.
- Reproducible builds: build backend switched from setuptools to
  hatchling (setuptools sdists are not reproducible even with
  `SOURCE_DATE_EPOCH`; hatchling wheel and sdist are byte-identical
  across builds). `make dist` sets `SOURCE_DATE_EPOCH` from the
  head commit; `make repro-check` builds twice and compares hashes.
- `ruff` lint (`check` + `format`) and a pytest coverage gate
  (>= 85%; measured 86.55%, 45 tests). CI matrix is Python 3.11
  and 3.12 and also runs the reproducibility check.
- `docs/DESIGN.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`
  documenting the why behind every decision, with sources.

### Fixed

- Relative `--agent` paths were resolved against the work
  directory, so the agent was never found (exit 127) while a
  pass-always fixture still reported `pass=true`; paths are now
  resolved against the caller's directory, and non-executable
  scripts run via `sh`. (`3cee013`)

## [0.1.0] - 2026-11-08

### Added

- Initial release: `run`, `compare`, `report`, `self-test`, `doctor` and
  `version` commands for the `arabica-plugin-v1` protocol.
- `run` executes one task with and without a tool, per repeat, with
  timeout (process-group kill), token parsing from a JSON `usage`
  object (`null` when absent), and a deterministic JSON report.
- `plugin.toml`, JSON schemas in `schema/`, fixtures in `fixtures/`,
  `Makefile`, README, MIT LICENSE, and CI running `--self-test` and
  `doctor`.
