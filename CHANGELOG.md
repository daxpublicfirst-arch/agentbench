# Changelog

All notable changes to agentbench will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
