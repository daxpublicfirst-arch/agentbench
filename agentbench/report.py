"""``compare`` and ``report``: deltas and tables over run reports."""

from __future__ import annotations

import json
from pathlib import Path

from . import BenchError
from .run import summarize_runs


def load_report(path: str | Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise BenchError(
            "TASK_INVALID", f"Run report not found: {path}",
            "Pass --a / --b with the path of a run.json produced by agentbench run.",
            "Run agentbench run first and check the --out directory.",
            exit_code=2,
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchError(
            "TASK_INVALID", f"Run report is not valid JSON: {path}",
            "Pass a run.json file produced by agentbench run.",
            "Re-run agentbench run to regenerate the report.",
            exit_code=2,
        ) from exc
    if not isinstance(data, dict):
        raise BenchError(
            "TASK_INVALID", f"Run report must be a JSON object: {path}",
            "Pass a run.json file produced by agentbench run.",
            "Re-run agentbench run to regenerate the report.",
            exit_code=2,
        )
    return data


def aggregate(report: dict) -> dict:
    """One aggregate (pass rate, token / wall medians) for a report.

    Handles a full ``run`` report (``runs`` list), a single-arm summary,
    and a bare aggregate object.
    """
    if isinstance(report.get("runs"), list) and report["runs"]:
        return summarize_runs(report["runs"])
    summary = report.get("summary")
    if isinstance(summary, dict):
        arms = [v for v in summary.values() if isinstance(v, dict)
                and "pass_rate" in v]
        if len(arms) == 1:
            return arms[0]
        if arms:
            # Combined arms: average the pass rates, median the medians
            # is not meaningful, so report the combined pass rate and
            # leave medians to the caller-visible arms. For compare we
            # use the first arm's medians only when there is one arm.
            combined = {
                "pass_rate": round(
                    sum(a["pass_rate"] for a in arms) / len(arms), 4),
                "tokens_median": None,
                "wall_median_s": None,
                "runs": sum(a.get("runs", 0) for a in arms),
            }
            return combined
    if "pass_rate" in report:
        return {
            "pass_rate": report["pass_rate"],
            "tokens_median": report.get("tokens_median"),
            "wall_median_s": report.get("wall_median_s"),
            "runs": report.get("runs", 0),
        }
    raise BenchError(
        "TASK_INVALID", "Run report has no runs or summary to compare.",
        "Pass run.json files produced by agentbench run.",
        "Re-run agentbench run to regenerate the reports.",
        exit_code=2,
    )


def _delta(a, b):
    if a is None or b is None:
        return None
    value = a - b
    if isinstance(value, float):
        return round(value, 4)
    return value


def compare_reports(a_path: str | Path, b_path: str | Path) -> dict:
    """Delta of report A minus report B (A is usually ``with``)."""
    a = aggregate(load_report(a_path))
    b = aggregate(load_report(b_path))
    return {
        "ok": True,
        "delta": {
            "pass_rate": _delta(a.get("pass_rate"), b.get("pass_rate")),
            "tokens_median": _delta(a.get("tokens_median"), b.get("tokens_median")),
            "wall_median_s": _delta(a.get("wall_median_s"), b.get("wall_median_s")),
        },
    }


def find_run_reports(out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    if not out.is_dir():
        raise BenchError(
            "TASK_INVALID", f"Output directory does not exist: {out}",
            "Pass --out with the directory agentbench run wrote to.",
            "Run agentbench run first and check the --out directory.",
            exit_code=2,
        )
    reports = sorted(out.rglob("run.json"))
    if not reports:
        raise BenchError(
            "TASK_INVALID", f"No run.json found under: {out}",
            "Run agentbench run with --out pointing at this directory first.",
            "Check the --out path and run again.",
            exit_code=2,
        )
    return reports


def build_report(out_dir: str | Path) -> dict:
    """Aggregate every run.json under ``out_dir`` into one report object."""
    reports = find_run_reports(out_dir)
    entries = []
    for path in reports:
        data = load_report(path)
        entries.append({
            "file": str(path.resolve()),
            "task": data.get("task"),
            "summary": data.get("summary", {}),
            "runs": len(data.get("runs", [])),
        })
    return {"ok": True, "out": str(Path(out_dir).resolve()), "reports": entries}


def render_table(report: dict) -> str:
    lines = [f"agentbench report: {report['out']}", ""]
    header = f"{'file':<60} {'runs':>4}  {'with pass':>9}  {'without pass':>12}"
    lines.append(header)
    lines.append("-" * len(header))
    for entry in report["reports"]:
        summary = entry.get("summary", {})
        with_arm = summary.get("with", {})
        without_arm = summary.get("without", {})
        lines.append(
            f"{entry['file'][-60:]:<60} {entry['runs']:>4}  "
            f"{str(with_arm.get('pass_rate', '-')):>9}  "
            f"{str(without_arm.get('pass_rate', '-')):>12}"
        )
        for arm_name in ("with", "without"):
            arm = summary.get(arm_name)
            if arm:
                lines.append(
                    f"    {arm_name}: pass_rate={arm.get('pass_rate')} "
                    f"tokens_median={arm.get('tokens_median')} "
                    f"wall_median_s={arm.get('wall_median_s')} "
                    f"runs={arm.get('runs')}"
                )
    return "\n".join(lines) + "\n"
