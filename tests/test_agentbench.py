"""Tests for agentbench: token parsing, runs, compare, report, errors."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentbench import BenchError, __version__
from agentbench import cli as cli_mod
from agentbench.agent import (
    check_agent_present,
    parse_tokens,
    resolve_agent_program,
    run_script,
)
from agentbench.cli import main as cli_main
from agentbench.cli import run_doctor, run_self_test
from agentbench.report import aggregate, build_report, compare_reports, load_report, render_table
from agentbench.run import _write_text_atomic, run_benchmark, summarize_runs
from agentbench.task import load_task

# ---------------------------------------------------------------- helpers


def make_task(
    tmp_path, *, prompt="Do the thing.\n", check="exit 0\n", setup=None, meta=None, files=None
):
    task = tmp_path / "task"
    task.mkdir(parents=True)
    (task / "prompt.md").write_text(prompt)
    (task / "check.sh").write_text("#!/bin/sh\n" + check)
    (task / "check.sh").chmod(0o755)
    if setup is not None:
        (task / "setup.sh").write_text("#!/bin/sh\n" + setup)
        (task / "setup.sh").chmod(0o755)
    if meta is not None:
        (task / "meta.json").write_text(json.dumps(meta))
    if files:
        files_dir = task / "files"
        files_dir.mkdir()
        for name, content in files.items():
            dest = files_dir / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
    return task


def make_agent(tmp_path, name="agent.sh", body="cat > /dev/null\nexit 0\n"):
    agent = tmp_path / name
    agent.write_text("#!/bin/sh\n" + body)
    agent.chmod(0o755)
    return str(agent)


def run_cli(*argv):
    return cli_main(list(argv))


# ---------------------------------------------------------------- tokens


def test_parse_tokens_json_usage():
    assert parse_tokens('{"usage":{"input_tokens":10,"output_tokens":20}}') == 30
    assert parse_tokens('{"usage":{"total_tokens":42}}') == 42
    assert parse_tokens('{"usage":{"prompt_tokens":5,"completion_tokens":7}}') == 12


def test_parse_tokens_absent_is_null_not_zero():
    assert parse_tokens("hello world\n") is None
    assert parse_tokens("") is None
    assert parse_tokens('{"usage":{}}') is None
    assert parse_tokens('{"no_usage":1}') is None


def test_parse_tokens_json_per_line():
    assert parse_tokens('log line\n{"usage":{"input_tokens":1,"output_tokens":2}}\n') == 3


def test_parse_tokens_claude_cache_buckets_counted():
    # Claude Code result envelope: cache tokens are input-side buckets
    # on top of input_tokens, so the total includes them.
    text = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "result": "done",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 500,
            },
            "modelUsage": {
                "claude-sonnet-4-6": {
                    "inputTokens": 100,
                    "outputTokens": 50,
                    "cacheCreationInputTokens": 200,
                    "cacheReadInputTokens": 500,
                }
            },
        }
    )
    assert parse_tokens(text) == 850


def test_parse_tokens_model_usage_only():
    text = json.dumps(
        {
            "type": "result",
            "modelUsage": {
                "model-a": {"inputTokens": 10, "outputTokens": 5},
                "model-b": {"inputTokens": 20, "outputTokens": 5},
            },
        }
    )
    assert parse_tokens(text) == 40


def test_parse_tokens_claude_stream_last_aggregate_wins():
    lines = [
        {"type": "assistant", "message": {"usage": {"input_tokens": 10, "output_tokens": 5}}},
        {
            "type": "result",
            "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 500},
        },
    ]
    text = "\n".join(json.dumps(line) for line in lines)
    assert parse_tokens(text) == 650


def test_parse_tokens_pi_per_turn_usage_is_summed():
    lines = [
        {
            "type": "message_end",
            "usage": {"input": 40, "output": 10, "cacheRead": 20, "cacheWrite": 5},
        },
        {
            "type": "message_end",
            "usage": {"input": 60, "output": 20, "cacheRead": 0, "cacheWrite": 10},
        },
    ]
    text = "\n".join(json.dumps(line) for line in lines)
    assert parse_tokens(text) == 165


def test_parse_tokens_pi_token_usage_event():
    text = json.dumps(
        {
            "type": "token_usage",
            "inputTokens": 10,
            "outputTokens": 5,
            "totalTokens": 15,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
        }
    )
    assert parse_tokens(text) == 15


def test_parse_tokens_total_wins_and_reasoning_not_double_counted():
    # totalTokens beats summing buckets; reasoning tokens are a subset
    # of output_tokens and must not be added on top.
    assert parse_tokens('{"usage":{"total_tokens":42,"input_tokens":10,"output_tokens":20}}') == 42
    assert (
        parse_tokens(
            '{"usage":{"input_tokens":10,"output_tokens":20,"reasoning_output_tokens":20}}'
        )
        == 30
    )


# ---------------------------------------------------------------- summary


def test_summarize_runs():
    runs = [
        {"pass": True, "tokens": 10, "wall_s": 1.0},
        {"pass": False, "tokens": None, "wall_s": 3.0},
        {"pass": True, "tokens": 30, "wall_s": 2.0},
    ]
    summary = summarize_runs(runs)
    assert summary["pass_rate"] == pytest.approx(2 / 3, abs=0.001)
    assert summary["tokens_median"] == 20
    assert summary["wall_median_s"] == 2.0
    assert summary["runs"] == 3
    assert summarize_runs([]) == {
        "pass_rate": 0.0,
        "tokens_median": None,
        "wall_median_s": None,
        "runs": 0,
    }


# ---------------------------------------------------------------- run


def test_run_pass_and_fail(tmp_path):
    task = make_task(tmp_path / "t1")
    agent = make_agent(tmp_path)
    report = run_benchmark(
        load_task(task),
        agent_cmd=agent,
        with_path=None,
        without=True,
        repeat=1,
        out_dir=tmp_path / "out",
    )
    assert report["ok"] is True
    assert report["runs"][0]["pass"] is True
    assert report["runs"][0]["tokens"] is None
    assert (tmp_path / "out" / "run.json").is_file()

    task2 = make_task(tmp_path / "t2", check="exit 1\n")
    report2 = run_benchmark(
        load_task(task2),
        agent_cmd=agent,
        with_path=None,
        without=True,
        repeat=1,
        out_dir=tmp_path / "out2",
    )
    assert report2["runs"][0]["pass"] is False


def test_run_repeat_two_produces_two_runs_per_arm(tmp_path):
    task = make_task(tmp_path / "task")
    agent = make_agent(tmp_path)
    toolbin = tmp_path / "toolbin"
    toolbin.mkdir()
    report = run_benchmark(
        load_task(task),
        agent_cmd=agent,
        with_path=str(toolbin),
        without=True,
        repeat=2,
        out_dir=tmp_path / "out",
    )
    assert len(report["runs"]) == 4
    assert [r["arm"] for r in report["runs"]] == ["with", "with", "without", "without"]
    assert [r["repeat"] for r in report["runs"]] == [1, 2, 1, 2]
    assert report["summary"]["with"]["runs"] == 2
    assert report["summary"]["without"]["runs"] == 2


def test_run_tokens_from_agent_stdout(tmp_path):
    task = make_task(tmp_path / "task")
    agent = make_agent(
        tmp_path,
        body="cat > /dev/null\n"
        'printf \'%s\\n\' \'{"usage":{"input_tokens":10,"output_tokens":20}}\'\n',
    )
    report = run_benchmark(
        load_task(task),
        agent_cmd=agent,
        with_path=None,
        without=True,
        repeat=1,
        out_dir=tmp_path / "out",
    )
    assert report["runs"][0]["tokens"] == 30


def test_run_timeout_kills_agent_and_records_timed_out(tmp_path):
    task = make_task(tmp_path / "task", meta={"timeout_s": 1})
    agent = make_agent(tmp_path, body="sleep 10\nexit 0\n")
    report = run_benchmark(
        load_task(task),
        agent_cmd=agent,
        with_path=None,
        without=True,
        repeat=1,
        out_dir=tmp_path / "out",
    )
    run = report["runs"][0]
    assert run["timed_out"] is True
    assert run["pass"] is False
    assert run["wall_s"] < 5


def test_run_with_prepends_path_without_does_not(tmp_path):
    task = make_task(tmp_path / "task", check="test -f tool_marker.txt\n")
    agent = make_agent(
        tmp_path,
        body='cat > /dev/null\ncase ":$PATH:" in *toolbin*) touch tool_marker.txt ;; esac\n',
    )
    toolbin = tmp_path / "toolbin"
    toolbin.mkdir()
    report = run_benchmark(
        load_task(task),
        agent_cmd=agent,
        with_path=str(toolbin),
        without=True,
        repeat=1,
        out_dir=tmp_path / "out",
    )
    by_arm = {r["arm"]: r for r in report["runs"]}
    assert by_arm["with"]["pass"] is True
    assert by_arm["without"]["pass"] is False


def test_run_prompt_template_substitution(tmp_path):
    task = make_task(
        tmp_path / "task",
        prompt="hello prompt\n",
        check="test -f got.txt\n",
        files={"placeholder.txt": "x"},
    )
    agent = make_agent(tmp_path, body="printf '%s' \"$1\" > got.txt\n")
    # {prompt} is passed as the last argument via the template.
    report = run_benchmark(
        load_task(task),
        agent_cmd=f"{agent} '{{prompt}}'",
        with_path=None,
        without=True,
        repeat=1,
        out_dir=tmp_path / "out",
    )
    assert report["runs"][0]["pass"] is True


def test_run_stdin_prompt_and_setup_and_files(tmp_path):
    task = make_task(
        tmp_path / "task",
        check="test -f out.txt\n",
        setup="cat > out.txt\n",
        files={"seed.txt": "seed"},
    )
    agent = make_agent(tmp_path)
    report = run_benchmark(
        load_task(task),
        agent_cmd=agent,
        with_path=None,
        without=True,
        repeat=1,
        out_dir=tmp_path / "out",
    )
    run = report["runs"][0]
    assert run["pass"] is True
    assert (tmp_path / "out" / "without" / "1" / "work" / "seed.txt").is_file()


def test_run_agent_relative_path(tmp_path, monkeypatch):
    """A relative ./agent.sh is resolved against the caller's cwd,
    not the workdir, and must actually run."""
    task = make_task(tmp_path / "task", check="test -f agent_ran.txt\n")
    make_agent(tmp_path, body="touch agent_ran.txt\n")
    monkeypatch.chdir(tmp_path)
    report = run_benchmark(
        load_task(task),
        agent_cmd="./agent.sh",
        with_path=None,
        without=True,
        repeat=1,
        out_dir=tmp_path / "out",
    )
    assert report["runs"][0]["pass"] is True
    assert report["runs"][0]["exit"] == 0


def test_run_agent_non_executable_script(tmp_path):
    """A 644 shell script as --agent runs through sh (GitHub checkouts
    lose the +x bit on fixture scripts)."""
    task = make_task(tmp_path / "task", check="test -f agent_ran.txt\n")
    agent = tmp_path / "agent.sh"
    agent.write_text("#!/bin/sh\ntouch agent_ran.txt\n")
    agent.chmod(0o644)
    report = run_benchmark(
        load_task(task),
        agent_cmd=str(agent),
        with_path=None,
        without=True,
        repeat=1,
        out_dir=tmp_path / "out",
    )
    assert report["runs"][0]["pass"] is True
    assert report["runs"][0]["exit"] == 0


def test_run_invalid_task_raises_task_invalid(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    with pytest.raises(BenchError) as excinfo:
        load_task(bad)
    assert excinfo.value.code == "TASK_INVALID"
    assert excinfo.value.exit_code == 2


def test_task_over_input_limit_is_refused(tmp_path):
    task = make_task(tmp_path / "task")
    big = task / "files" / "big.bin"
    big.parent.mkdir(exist_ok=True)
    with open(big, "wb") as handle:
        handle.seek(50 * 1024 * 1024)  # sparse: 50 MB + 1 byte, no 50 MB write
        handle.write(b"x")
    with pytest.raises(BenchError) as excinfo:
        load_task(task)
    assert excinfo.value.code == "TASK_INVALID"
    assert "50" in excinfo.value.blocked


def test_timeout_over_limit_is_refused(tmp_path):
    task = make_task(tmp_path / "task")
    agent = make_agent(tmp_path)
    with pytest.raises(BenchError) as excinfo:
        run_benchmark(
            load_task(task),
            agent_cmd=agent,
            with_path=None,
            without=True,
            repeat=1,
            out_dir=tmp_path / "out",
            timeout_s=1801,
        )
    assert excinfo.value.code == "TASK_INVALID"


def test_run_report_has_run_id_and_preserved_copy(tmp_path):
    task = make_task(tmp_path / "task")
    agent = make_agent(tmp_path)
    report = run_benchmark(
        load_task(task),
        agent_cmd=agent,
        with_path=None,
        without=True,
        repeat=1,
        out_dir=tmp_path / "out",
    )
    assert report["run_id"]
    copy = tmp_path / "out" / "runs" / f"{report['run_id']}.json"
    assert copy.is_file()
    assert json.loads(copy.read_text()) == report
    # report --out finds the preserved copy as well as run.json
    built = build_report(tmp_path / "out")
    assert len(built["reports"]) == 2


def test_run_deterministic_json(tmp_path):
    task = make_task(tmp_path / "task")
    agent = make_agent(tmp_path)
    report = run_benchmark(
        load_task(task),
        agent_cmd=agent,
        with_path=None,
        without=True,
        repeat=1,
        out_dir=tmp_path / "out",
    )
    text = (tmp_path / "out" / "run.json").read_text()
    assert json.loads(text) == report
    assert text == json.dumps(report, sort_keys=True, indent=2) + "\n"


# ---------------------------------------------------------------- compare / report


def test_compare_delta(tmp_path):
    def _report(pass_rate, tokens, wall):
        return {
            "ok": True,
            "task": "/t",
            "summary": {
                "with": {
                    "pass_rate": pass_rate,
                    "tokens_median": tokens,
                    "wall_median_s": wall,
                    "runs": 1,
                }
            },
            "runs": [
                {
                    "arm": "with",
                    "repeat": 1,
                    "pass": pass_rate == 1,
                    "exit": 0,
                    "wall_s": wall,
                    "tokens": tokens,
                    "check": "ok",
                    "stdout": "/x",
                    "stderr": "/y",
                }
            ],
        }

    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps(_report(1.0, 100, 10.0)))
    b.write_text(json.dumps(_report(0.0, 300, 20.0)))
    delta = compare_reports(a, b)
    assert delta == {
        "ok": True,
        "delta": {"pass_rate": 1.0, "tokens_median": -200, "wall_median_s": -10.0},
    }


def test_report_finds_run_json(tmp_path):
    task = make_task(tmp_path / "task")
    agent = make_agent(tmp_path)
    run_benchmark(
        load_task(task),
        agent_cmd=agent,
        with_path=None,
        without=True,
        repeat=1,
        out_dir=tmp_path / "out",
    )
    report = build_report(tmp_path / "out")
    assert report["ok"] is True
    assert len(report["reports"]) == 2  # run.json + runs/<run_id>.json copy


# ---------------------------------------------------------------- cli


def test_cli_version_and_doctor(tmp_path, capsys):
    assert run_cli("--version") == 0
    out = capsys.readouterr().out
    assert out.strip() == f"agentbench {__version__}"

    doctor = run_doctor(agent_cmd="sh", out_dir=str(tmp_path / "doc-out"))
    assert doctor["ok"] is True
    assert doctor["checks"]["python"]["ok"] is True
    assert doctor["checks"]["platform"]["ok"] is True


def test_cli_run_exit_codes(tmp_path, capsys):
    task = make_task(tmp_path / "task")
    agent = make_agent(tmp_path)
    code = run_cli(
        "run", "--task", str(task), "--agent", agent, "--out", str(tmp_path / "out"), "--json"
    )
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True

    task2 = make_task(tmp_path / "task2", check="exit 1\n")
    code2 = run_cli(
        "run", "--task", str(task2), "--agent", agent, "--out", str(tmp_path / "out2"), "--json"
    )
    assert code2 == 1  # runs failed, not an error
    assert json.loads(capsys.readouterr().out)["runs"][0]["pass"] is False

    code3 = run_cli(
        "run",
        "--task",
        str(tmp_path / "nope"),
        "--agent",
        agent,
        "--out",
        str(tmp_path / "out3"),
        "--json",
    )
    assert code3 == 2
    err = json.loads(capsys.readouterr().out)
    assert err["ok"] is False
    assert err["error"]["code"] == "TASK_INVALID"


def test_cli_request_envelope(tmp_path, capsys):
    task = make_task(tmp_path / "task")
    agent = make_agent(tmp_path)
    envelope = tmp_path / "request.json"
    envelope.write_text(
        json.dumps(
            {
                "protocol": "arabica-plugin-v1",
                "command": "run",
                "args": {
                    "task": str(task),
                    "agent": agent,
                    "out": str(tmp_path / "out"),
                    "repeat": 1,
                },
            }
        )
    )
    assert run_cli("--request", str(envelope)) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_self_test_passes():
    result = run_self_test()
    assert result["ok"] is True, result
    assert {f["fixture"] for f in result["fixtures"]} >= {
        "pass",
        "fail",
        "tokens",
        "timeout",
        "path",
        "claude-json",
        "claude-stream",
        "pi-json",
    }


def test_cli_subprocess_end_to_end(tmp_path):
    """The installed console script, run as a real process."""
    result = subprocess.run(
        [sys.executable, "-m", "agentbench.cli", "--version"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent),
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"agentbench {__version__}"
    telemetry = json.loads(result.stderr.strip().splitlines()[-1])
    assert telemetry["tool"] == "agentbench"
    assert telemetry["command"] == "version"
    assert telemetry["ok"] is True


# ------------------------------------------------------- coverage: cli/report/task/run


def test_cli_do_version_json(capsys):
    code, result, _ = cli_mod._do_version(True)
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == __version__
    assert result["version"] == __version__


def test_cli_do_doctor_json(tmp_path, capsys):
    code, result, _ = cli_mod._do_doctor({"agent": "sh", "out": str(tmp_path / "o")}, True)
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["checks"]["platform"]["ok"] is True


def test_cli_do_self_test_json(capsys):
    code, result, _ = cli_mod._do_self_test({}, True)
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert result["ok"] is True


def test_cli_do_compare_and_report(tmp_path, capsys):
    task = make_task(tmp_path / "task")
    agent = make_agent(tmp_path)
    run_benchmark(
        load_task(task),
        agent_cmd=agent,
        with_path=None,
        without=True,
        repeat=1,
        out_dir=tmp_path / "out",
    )
    code, result, _ = cli_mod._do_compare(
        {"a": str(tmp_path / "out" / "run.json"), "b": str(tmp_path / "out" / "run.json")},
        True,
    )
    assert code == 0
    assert result["delta"]["pass_rate"] == 0.0
    capsys.readouterr()  # clear the compare JSON
    code, built, _ = cli_mod._do_report({"out": str(tmp_path / "out")}, False)
    assert code == 0
    assert "agentbench report:" in capsys.readouterr().out
    assert len(built["reports"]) == 2


def test_cli_main_branches(tmp_path):
    assert run_cli() == 2  # no command
    with pytest.raises(SystemExit):
        run_cli("nonsense")  # argparse exits 2 on an unknown command
    assert run_cli("--version") == 0
    assert run_cli("--self-test") == 0
    assert run_cli("doctor", "--out", str(tmp_path / "o")) == 0


def test_load_envelope_errors(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    with pytest.raises(BenchError) as excinfo:
        cli_mod.load_envelope(bad)
    assert excinfo.value.code == "TASK_INVALID"
    bad.write_text(json.dumps({"protocol": "other", "command": "run", "args": {}}))
    with pytest.raises(BenchError):
        cli_mod.load_envelope(bad)
    bad.write_text(json.dumps({"protocol": "arabica-plugin-v1", "command": "bogus", "args": {}}))
    with pytest.raises(BenchError):
        cli_mod.load_envelope(bad)
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"protocol": "arabica-plugin-v1", "command": "version", "args": {}}))
    assert cli_mod.load_envelope(good) == {
        "command": "version",
        "args": {},
        "json": False,
    }


def test_report_aggregate_and_load_errors(tmp_path):
    task = make_task(tmp_path / "task")
    agent = make_agent(tmp_path)
    run_benchmark(
        load_task(task),
        agent_cmd=agent,
        with_path=None,
        without=True,
        repeat=1,
        out_dir=tmp_path / "out",
    )
    with pytest.raises(BenchError):
        aggregate({})
    single = {
        "ok": True,
        "task": "t",
        "summary": {
            "with": {"pass_rate": 1.0, "tokens_median": 10, "wall_median_s": 0.1, "runs": 1}
        },
        "runs": [],
    }
    assert aggregate(single) == single["summary"]["with"]
    bare = {"pass_rate": 0.5, "tokens_median": 7, "wall_median_s": 0.2, "runs": 3}
    assert aggregate(bare) == bare
    built = build_report(tmp_path / "out")
    table = render_table(built)
    assert table.startswith("agentbench report:")
    missing = tmp_path / "nope.json"
    with pytest.raises(BenchError):
        load_report(missing)
    bad = tmp_path / "bad.json"
    bad.write_text("[]")
    with pytest.raises(BenchError):
        load_report(bad)


def test_task_meta_errors(tmp_path):
    task = make_task(tmp_path / "t1", meta={"timeout_s": 0})
    with pytest.raises(BenchError):
        load_task(task)
    task = make_task(tmp_path / "t2", meta={"timeout_s": 99999})
    with pytest.raises(BenchError):
        load_task(task)
    task = make_task(tmp_path / "t3", meta={"repeat": 0})
    with pytest.raises(BenchError):
        load_task(task)
    task = make_task(tmp_path / "t4", meta={"timeout_s": "soon"})
    with pytest.raises(BenchError):
        load_task(task)
    task = make_task(tmp_path / "t5")
    (task / "meta.json").write_text("[1, 2]")
    with pytest.raises(BenchError):
        load_task(task)
    task = make_task(tmp_path / "t6", prompt="")
    with pytest.raises(BenchError):
        load_task(task)


def test_run_setup_failure_and_bad_with_path(tmp_path):
    task = make_task(tmp_path / "task", setup="exit 3\n")
    agent = make_agent(tmp_path)
    report = run_benchmark(
        load_task(task),
        agent_cmd=agent,
        with_path=None,
        without=True,
        repeat=1,
        out_dir=tmp_path / "out",
    )
    assert report["runs"][0]["pass"] is False
    assert report["runs"][0]["setup_exit"] == 3
    task2 = make_task(tmp_path / "task2")
    with pytest.raises(BenchError):
        run_benchmark(
            load_task(task2),
            agent_cmd=agent,
            with_path=tmp_path / "missing-toolbin",
            without=True,
            repeat=1,
            out_dir=tmp_path / "out2",
        )
    with pytest.raises(BenchError):
        run_benchmark(
            load_task(task2),
            agent_cmd=agent,
            with_path=None,
            without=True,
            repeat=0,
            out_dir=tmp_path / "out3",
        )


def test_agent_helpers(tmp_path):
    present, detail = check_agent_present("definitely-not-a-real-command-xyz")
    assert present is False
    assert "not found" in detail
    assert check_agent_present("sh")[0] is True
    assert resolve_agent_program("sh -c 'true'") == "sh -c 'true'"
    assert resolve_agent_program('"my agent" --flag') == '"my agent" --flag'
    script = tmp_path / "slow.sh"
    script.write_text("#!/bin/sh\nsleep 5\n")
    script.chmod(0o755)
    result = run_script(script, cwd=tmp_path, path_value="/usr/bin:/bin", timeout_s=1)
    assert result.timed_out is True
    assert result.exit_code is None
    tmp_file = tmp_path / "atomic.txt"
    _write_text_atomic(tmp_file, "hello")
    assert tmp_file.read_text() == "hello"


def test_doctor_missing_agent_is_not_ok(tmp_path):
    doctor = run_doctor(agent_cmd="definitely-not-a-real-command-xyz", out_dir=str(tmp_path / "o"))
    assert doctor["ok"] is False
    assert doctor["checks"]["agent"]["ok"] is False


def test_self_test_reports_a_failing_fixture(tmp_path):
    root = tmp_path / "fixtures"
    task = root / "bad" / "task"
    task.mkdir(parents=True)
    (task / "prompt.md").write_text("x\n")
    (task / "check.sh").write_text("#!/bin/sh\nexit 0\n")
    (task / "agent.sh").write_text("#!/bin/sh\ncat > /dev/null\nexit 0\n")
    (root / "bad" / "expected.json").write_text(
        json.dumps(
            {"with": {"pass": False, "tokens": None}, "without": {"pass": False, "tokens": None}}
        )
    )
    result = run_self_test(fixtures_dir=root)
    assert result["ok"] is False
    assert result["fixtures"][0]["problems"]


def test_main_request_envelope_compare_and_version(tmp_path):
    task = make_task(tmp_path / "task")
    agent = make_agent(tmp_path)
    run_benchmark(
        load_task(task),
        agent_cmd=agent,
        with_path=None,
        without=True,
        repeat=1,
        out_dir=tmp_path / "out",
    )
    envelope = tmp_path / "req.json"
    envelope.write_text(
        json.dumps(
            {
                "protocol": "arabica-plugin-v1",
                "command": "compare",
                "args": {
                    "a": str(tmp_path / "out" / "run.json"),
                    "b": str(tmp_path / "out" / "run.json"),
                },
            }
        )
    )
    assert run_cli("--request", str(envelope)) == 0
    envelope.write_text(
        json.dumps(
            {
                "protocol": "arabica-plugin-v1",
                "command": "version",
                "args": {},
            }
        )
    )
    assert run_cli("--request", str(envelope)) == 0


def test_main_unexpected_error_is_exit_3(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_mod, "run_benchmark", _boom)
    task = make_task(tmp_path / "task")
    agent = make_agent(tmp_path)
    code = run_cli("run", "--task", str(task), "--agent", agent, "--out", str(tmp_path / "out"))
    assert code == 3
