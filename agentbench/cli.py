"""Command-line interface for agentbench.

Every command supports ``--json`` and, with it, writes one JSON object
to stdout. Without ``--json``, ``run`` / ``compare`` still write JSON
(their primary machine output), while ``report`` / ``self-test`` /
``doctor`` / ``version`` print human-readable text.

Exit codes: 0 success, 1 at least one run failed (not an error),
2 usage or task error, 3 tool failure.

Telemetry: one JSON line to stderr per invocation::

    {"tool":"agentbench","version":"0.1.0","command":"run",
     "duration_ms":123,"input_bytes":4567,"output_bytes":890,"ok":true}

An adapter envelope may be passed with ``--request <path>`` instead of
flags::

    {"protocol":"arabica-plugin-v1","command":"run",
     "args":{"task":"/abs/task","agent":"claude -p --output-format json",
             "with":"/abs/bin/example-tool","without":true,"repeat":3,
             "out":"/abs/runs"}}
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import BenchError, __version__
from .agent import check_agent_present
from .report import build_report, compare_reports, render_table
from .run import run_benchmark
from .task import DEFAULT_TIMEOUT_S, load_task

PROTOCOL = "arabica-plugin-v1"
COMMANDS = ["run", "compare", "report", "self-test", "doctor", "version"]


def dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=2)


_OUTPUT_BYTES = 0


def _emit(text: str) -> int:
    global _OUTPUT_BYTES
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()
    _OUTPUT_BYTES += len(text.encode("utf-8"))
    return _OUTPUT_BYTES


# ---------------------------------------------------------------- fixtures

# Embedded fallback fixtures, identical in content to fixtures/ in the
# repo. Used by --self-test when the tool is installed as a wheel and
# the repo fixtures/ directory is not next to the package.
EMBEDDED_FIXTURES = {
    "pass": {
        "prompt.md": "Do nothing. This task always passes.\n",
        "check.sh": "#!/bin/sh\nexit 0\n",
        "agent.sh": "#!/bin/sh\ncat > /dev/null\nexit 0\n",
        "expected.json": {"with": {"pass": True, "tokens": None},
                          "without": {"pass": True, "tokens": None}},
    },
    "fail": {
        "prompt.md": "Do nothing. This task always fails.\n",
        "check.sh": "#!/bin/sh\nexit 1\n",
        "agent.sh": "#!/bin/sh\ncat > /dev/null\nexit 0\n",
        "expected.json": {"with": {"pass": False, "tokens": None},
                          "without": {"pass": False, "tokens": None}},
    },
    "tokens": {
        "prompt.md": "Report your token usage as JSON.\n",
        "check.sh": "#!/bin/sh\nexit 0\n",
        "agent.sh": "#!/bin/sh\ncat > /dev/null\n"
                    "printf '%s\\n' '{\"usage\":{\"input_tokens\":10,"
                    "\"output_tokens\":20}}'\nexit 0\n",
        "expected.json": {"with": {"pass": True, "tokens": 30},
                          "without": {"pass": True, "tokens": 30}},
    },
    "timeout": {
        "prompt.md": "Take too long.\n",
        "check.sh": "#!/bin/sh\nexit 0\n",
        "agent.sh": "#!/bin/sh\nsleep 10\nexit 0\n",
        "meta.json": json.dumps({"timeout_s": 2}),
        "expected.json": {"with": {"pass": False, "tokens": None, "timed_out": True},
                          "without": {"pass": False, "tokens": None, "timed_out": True}},
    },
    "path": {
        "prompt.md": "Use the tool on PATH if it is there.\n",
        "check.sh": "#!/bin/sh\ntest -f tool_marker.txt\n",
        "agent.sh": "#!/bin/sh\ncat > /dev/null\n"
                    "case \":$PATH:\" in *toolbin*) touch tool_marker.txt ;;\n"
                    "esac\nexit 0\n",
        "expected.json": {"with": {"pass": True, "tokens": None},
                          "without": {"pass": False, "tokens": None}},
    },
}


def find_fixtures_dir() -> Path | None:
    """Locate the repo fixtures/ directory, if this is a checkout."""
    here = Path(__file__).resolve()
    for candidate in (here.parent.parent / "fixtures",
                      here.parent / "fixtures",
                      Path.cwd() / "fixtures"):
        if (candidate / "pass" / "task" / "prompt.md").is_file():
            return candidate
    return None


def _materialise_embedded(tmp: Path) -> Path:
    root = tmp / "fixtures"
    for name, files in EMBEDDED_FIXTURES.items():
        task_dir = root / name / "task"
        task_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            if filename == "expected.json":
                (root / name / "expected.json").write_text(
                    json.dumps(content, sort_keys=True, indent=2), encoding="utf-8")
                continue
            dest = task_dir / filename
            dest.write_text(content, encoding="utf-8")
            if filename.endswith(".sh"):
                dest.chmod(0o755)
        if name == "path":
            toolbin = task_dir / "toolbin"
            toolbin.mkdir(exist_ok=True)
            tool = toolbin / "example-tool"
            tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            tool.chmod(0o755)
    return root


def run_self_test(fixtures_dir: str | Path | None = None) -> dict:
    """Run every fixture and return a self-test report.

    Exits 0 (``ok`` true) only when every expected report matches.
    A fake agent command (a small shell script) is used throughout;
    no real model is ever called.
    """
    if fixtures_dir:
        root = Path(fixtures_dir)
        if not root.is_dir():
            raise BenchError(
                "TASK_INVALID", f"Fixtures directory does not exist: {root}",
                "Omit --fixtures to use the bundled fixtures.",
                "Check the fixtures path and run again.", exit_code=2)
    else:
        root = find_fixtures_dir()

    results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="agentbench-self-test-") as tmp:
        tmp_path = Path(tmp)
        if root is None:
            root = _materialise_embedded(tmp_path)
        # A dummy tool directory for fixtures that have no toolbin of
        # their own, so both arms still run for every fixture.
        dummy_tool = tmp_path / "dummy-toolbin"
        dummy_tool.mkdir(exist_ok=True)
        dummy = dummy_tool / "example-tool"
        dummy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        dummy.chmod(0o755)

        for fixture in sorted(p for p in root.iterdir() if p.is_dir()):
            task_dir = fixture / "task"
            expected_path = fixture / "expected.json"
            if not task_dir.is_dir():
                continue
            agent = task_dir / "agent.sh"
            if not agent.is_file():
                results.append({"fixture": fixture.name, "ok": False,
                                "error": "missing agent.sh"})
                continue
            # Do NOT chmod the fixture in place: that would mutate the
            # user's checkout. run_agent runs a non-executable script
            # through sh, so a 644 fixture works as checked out.
            expected: dict = {}
            if expected_path.is_file():
                expected = json.loads(expected_path.read_text(encoding="utf-8"))
            toolbin = task_dir / "toolbin"
            with_path = str(toolbin if toolbin.is_dir() else dummy_tool)
            out_dir = tmp_path / "out" / fixture.name
            try:
                task = load_task(task_dir)
                report = run_benchmark(
                    task, agent_cmd=str(agent), with_path=with_path,
                    without=True, repeat=1, out_dir=out_dir)
            except BenchError as exc:
                results.append({"fixture": fixture.name, "ok": False,
                                "error": exc.blocked})
                continue
            by_arm = {r["arm"]: r for r in report["runs"]}
            problems: list[str] = []
            for arm, want in expected.items():
                got = by_arm.get(arm)
                if got is None:
                    problems.append(f"missing {arm} run")
                    continue
                if got["pass"] != want.get("pass"):
                    problems.append(
                        f"{arm}: pass={got['pass']} expected {want.get('pass')}")
                if "tokens" in want and got["tokens"] != want["tokens"]:
                    problems.append(
                        f"{arm}: tokens={got['tokens']} expected {want['tokens']}")
                if "timed_out" in want and got["timed_out"] != want["timed_out"]:
                    problems.append(
                        f"{arm}: timed_out={got['timed_out']} "
                        f"expected {want['timed_out']}")
            results.append({"fixture": fixture.name,
                            "ok": not problems,
                            "problems": problems})
    ok = bool(results) and all(r["ok"] for r in results)
    return {"ok": ok, "fixtures": results}


def run_doctor(agent_cmd: str | None = None, out_dir: str | Path | None = None) -> dict:
    """Check Python, the agent command's presence, and the output dir."""
    checks: dict = {}
    python_ok = sys.version_info >= (3, 11)
    checks["python"] = {
        "ok": python_ok,
        "detail": f"Python {sys.version.split()[0]} "
                  f"({'ok' if python_ok else 'needs 3.11+'})",
    }
    if agent_cmd:
        present, detail = check_agent_present(agent_cmd)
        checks["agent"] = {"ok": present, "detail": detail}
    else:
        checks["agent"] = {"ok": True, "detail": "no agent command given; skipped"}
    if out_dir:
        out = Path(out_dir)
        try:
            out.mkdir(parents=True, exist_ok=True)
            probe = out / ".agentbench-doctor-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            checks["out"] = {"ok": True, "detail": f"output directory writable: {out}"}
        except OSError as exc:
            checks["out"] = {"ok": False, "detail": f"output directory not writable: {exc}"}
    else:
        with tempfile.TemporaryDirectory(prefix="agentbench-doctor-") as tmp:
            checks["out"] = {"ok": True,
                             "detail": f"temporary output directory writable: {tmp}"}
    fixtures = find_fixtures_dir()
    checks["fixtures"] = {
        "ok": True,
        "detail": f"fixtures found at {fixtures}" if fixtures
                  else "fixtures not in checkout; embedded fixtures will be used",
    }
    ok = all(c["ok"] for c in checks.values())
    return {"ok": ok, "checks": checks}


# ---------------------------------------------------------------- envelope

def load_envelope(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchError(
            "TASK_INVALID", f"Request envelope is not valid JSON: {path}",
            "Pass --request with a JSON envelope file.",
            "Check the envelope file and run again.", exit_code=2) from exc
    if not isinstance(data, dict) or data.get("protocol") != PROTOCOL:
        raise BenchError(
            "TASK_INVALID", "Request envelope protocol must be 'arabica-plugin-v1'.",
            "Set protocol to 'arabica-plugin-v1' in the envelope.",
            "Check the envelope and run again.", exit_code=2)
    if data.get("command") not in COMMANDS:
        raise BenchError(
            "TASK_INVALID", f"Unknown command in envelope: {data.get('command')!r}",
            f"Use one of: {', '.join(COMMANDS)}.",
            "Check the envelope and run again.", exit_code=2)
    args = data.get("args") or {}
    if not isinstance(args, dict):
        raise BenchError(
            "TASK_INVALID", "Request envelope args must be a JSON object.",
            "Make args an object, for example {\"task\": \"/abs/task\"}.",
            "Check the envelope and run again.", exit_code=2)
    return {"command": data["command"], "args": args,
            "json": bool(data.get("json") or args.get("json"))}


def _dispatch_envelope(envelope: dict):
    command = envelope["command"]
    args = envelope["args"]
    as_json = envelope["json"]
    if command == "run":
        return _do_run(args, as_json)
    if command == "compare":
        return _do_compare(args, as_json)
    if command == "report":
        return _do_report(args, as_json)
    if command == "self-test":
        return _do_self_test(args, as_json)
    if command == "doctor":
        return _do_doctor(args, as_json)
    if command == "version":
        return _do_version(as_json)
    raise BenchError("TASK_INVALID", f"Unknown command: {command}",
                     f"Use one of: {', '.join(COMMANDS)}.",
                     "Check the command and run again.", exit_code=2)


# ---------------------------------------------------------------- commands

def _do_run(args: dict, as_json: bool):
    task = load_task(args["task"])
    report = run_benchmark(
        task,
        agent_cmd=args["agent"],
        with_path=args.get("with"),
        without=bool(args.get("without", False)),
        repeat=args.get("repeat"),
        out_dir=args["out"],
        timeout_s=args.get("timeout_s"),
    )
    failed = any(not r["pass"] for r in report["runs"])
    if as_json:
        _emit(dump(report))
    else:
        _emit(dump(report))
    return (1 if failed else 0), report, task.input_bytes


def _do_compare(args: dict, as_json: bool):
    result = compare_reports(args["a"], args["b"])
    _emit(dump(result))
    return 0, result, 0


def _do_report(args: dict, as_json: bool):
    result = build_report(args["out"])
    if as_json:
        _emit(dump(result))
    else:
        _emit(render_table(result))
    return 0, result, 0


def _do_self_test(args: dict, as_json: bool):
    result = run_self_test(args.get("fixtures"))
    if as_json:
        _emit(dump(result))
    else:
        for item in result["fixtures"]:
            status = "PASS" if item["ok"] else "FAIL"
            line = f"{status} {item['fixture']}"
            if item.get("problems"):
                line += ": " + "; ".join(item["problems"])
            if item.get("error"):
                line += f": {item['error']}"
            _emit(line)
        _emit("self-test: " + ("ok" if result["ok"] else "FAILED"))
    return (0 if result["ok"] else 1), result, 0


def _do_doctor(args: dict, as_json: bool):
    result = run_doctor(args.get("agent"), args.get("out"))
    if as_json:
        _emit(dump(result))
    else:
        for name, check in result["checks"].items():
            _emit(f"{'ok' if check['ok'] else 'FAIL'} {name}: {check['detail']}")
        _emit("doctor: " + ("ok" if result["ok"] else "FAILED"))
    return (0 if result["ok"] else 1), result, 0


def _do_version(as_json: bool):
    if as_json:
        result = {"ok": True, "name": "agentbench", "version": __version__}
        _emit(dump(result))
        return 0, result, 0
    _emit(f"agentbench {__version__}")
    return 0, {"ok": True, "version": __version__}, 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentbench",
        description="Run one task with and without a tool, then report "
                    "pass, tokens, calls and wall time.")
    parser.add_argument("--version", action="store_true",
                        help="Print agentbench version and exit.")
    parser.add_argument("--self-test", action="store_true",
                        help="Run every fixture in fixtures/ and exit.")
    parser.add_argument("--json", action="store_true",
                        help="Write one JSON object to stdout.")
    parser.add_argument("--request", metavar="PATH",
                        help="Read an adapter envelope JSON file instead of flags.")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="Run one task with and without a tool.")
    run_p.add_argument("--task", required=True)
    run_p.add_argument("--agent", required=True)
    run_p.add_argument("--with", dest="with_", default=None)
    run_p.add_argument("--without", action="store_true")
    run_p.add_argument("--repeat", type=int, default=None)
    run_p.add_argument("--out", required=True)
    run_p.add_argument("--timeout-s", dest="timeout_s", type=int, default=None)
    run_p.add_argument("--json", action="store_true")
    run_p.add_argument("--request", metavar="PATH", default=None)

    cmp_p = sub.add_parser("compare", help="Print the delta between two run reports.")
    cmp_p.add_argument("--a", required=True)
    cmp_p.add_argument("--b", required=True)
    cmp_p.add_argument("--json", action="store_true")
    cmp_p.add_argument("--request", metavar="PATH", default=None)

    rep_p = sub.add_parser("report", help="Report on all run.json files under --out.")
    rep_p.add_argument("--out", required=True)
    rep_p.add_argument("--json", action="store_true")
    rep_p.add_argument("--request", metavar="PATH", default=None)

    st_p = sub.add_parser("self-test", help="Run every fixture and check the reports.")
    st_p.add_argument("--json", action="store_true")
    st_p.add_argument("--fixtures", default=None)
    st_p.add_argument("--request", metavar="PATH", default=None)

    doc_p = sub.add_parser("doctor", help="Check Python, the agent, and the output dir.")
    doc_p.add_argument("--agent", default=None)
    doc_p.add_argument("--out", default=None)
    doc_p.add_argument("--json", action="store_true")
    doc_p.add_argument("--request", metavar="PATH", default=None)

    ver_p = sub.add_parser("version", help="Print agentbench version.")
    ver_p.add_argument("--json", action="store_true")
    ver_p.add_argument("--request", metavar="PATH", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    start = time.monotonic()
    command = "unknown"
    input_bytes = 0
    output_bytes = 0
    ok = True
    exit_code = 0
    try:
        args_list = list(sys.argv[1:] if argv is None else argv)
        # --request may appear anywhere; handle the envelope first.
        request_path = None
        for i, token in enumerate(args_list):
            if token == "--request" and i + 1 < len(args_list):
                request_path = args_list[i + 1]
            elif token.startswith("--request="):
                request_path = token.split("=", 1)[1]
        if request_path:
            envelope = load_envelope(request_path)
            command = envelope["command"]
            # Re-dispatch; _emit inside returns byte count via stdout only
            # for telemetry we approximate output_bytes below.
            exit_code, _result, input_bytes = _dispatch_envelope(envelope)
            ok = exit_code == 0
            return exit_code

        parser = build_parser()
        ns = parser.parse_args(args_list)
        as_json = bool(getattr(ns, "json", False))

        if ns.command is None:
            if ns.version:
                command = "version"
                exit_code, _r, input_bytes = _do_version(as_json)
            elif ns.self_test:
                command = "self-test"
                exit_code, _r, input_bytes = _do_self_test({}, as_json)
            else:
                parser.print_help(sys.stderr)
                return 2
            ok = exit_code == 0
            return exit_code

        command = ns.command
        if command == "run":
            exit_code, _r, input_bytes = _do_run(
                {"task": ns.task, "agent": ns.agent, "with": ns.with_,
                 "without": ns.without, "repeat": ns.repeat, "out": ns.out,
                 "timeout_s": ns.timeout_s}, as_json)
        elif command == "compare":
            exit_code, _r, input_bytes = _do_compare(
                {"a": ns.a, "b": ns.b}, as_json)
        elif command == "report":
            exit_code, _r, input_bytes = _do_report({"out": ns.out}, as_json)
        elif command == "self-test":
            exit_code, _r, input_bytes = _do_self_test(
                {"fixtures": ns.fixtures}, as_json)
        elif command == "doctor":
            exit_code, _r, input_bytes = _do_doctor(
                {"agent": ns.agent, "out": ns.out}, as_json)
        elif command == "version":
            exit_code, _r, input_bytes = _do_version(as_json)
        else:
            parser.print_help(sys.stderr)
            return 2
        ok = exit_code == 0
        return exit_code
    except BenchError as exc:
        ok = False
        exit_code = exc.exit_code
        output_bytes = _emit(dump(exc.as_dict()))
        return exit_code
    except BrokenPipeError:
        return 3
    except Exception as exc:  # fail loudly, never with a bare traceback
        ok = False
        exit_code = 3
        err = BenchError(
            "TOOL_FAILURE", f"agentbench failed: {exc}",
            "Check the command, the task directory and the output directory.",
            "Run agentbench doctor, then run the command again.",
            exit_code=3)
        output_bytes = _emit(dump(err.as_dict()))
        return exit_code
    finally:
        duration_ms = int((time.monotonic() - start) * 1000)
        telemetry = {
            "tool": "agentbench",
            "version": __version__,
            "command": command,
            "duration_ms": duration_ms,
            "input_bytes": int(input_bytes),
            "output_bytes": int(output_bytes or _OUTPUT_BYTES),
            "ok": bool(ok),
        }
        try:
            sys.stderr.write(json.dumps(telemetry, sort_keys=True) + "\n")
            sys.stderr.flush()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
