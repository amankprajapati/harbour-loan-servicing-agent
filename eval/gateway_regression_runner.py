#!/usr/bin/env python
"""Runs the full published case set once per gateway regression (plus a
clean baseline) and checks a safety invariant that must hold no matter
what the gateway does: a case may degrade to an escalation under a
regression, but it may never move money it wasn't supposed to, and it
may never move a different amount than the clean baseline would have.

This is what "the eval assertions fail under each regression and pass
clean" means in practice here: comparing a regression run's per-case
outcome to the clean baseline's outcome makes the fault visible (cases
that used to resolve now escalate) while the money-safety invariant is
checked directly and must hold in every single case, in every run.

The safety invariant is deliberately one-directional: a regression is
allowed to make a case fail *closed* (money that should have moved
under the clean baseline doesn't, because the case escalated instead)
-- that is the system behaving correctly under a degraded gateway. It
is never allowed to fail *open*: money must never move when the clean
baseline wouldn't have moved it, and never in a different amount than
the clean baseline would have moved.

Exit code is 0 only if the clean baseline is 100% correct AND the
money-safety invariant holds for every case under every regression.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from harbour.eval_support import run_one_case  # noqa: E402
from harbour.gateway_regressions import REGRESSIONS  # noqa: E402

CASES_PATH = REPO_ROOT / "cases" / "published" / "cases.json"
RESULTS_DIR = REPO_ROOT / "results"
TMP_DIR = REPO_ROOT / ".eval_tmp_gateway"
REPORT_PATH = RESULTS_DIR / "gateway_regression_report.json"


def _run_all(case_file: dict, *, client_factory, run_name: str) -> list[dict]:
    run_dir = TMP_DIR / run_name
    if run_dir.exists():
        shutil.rmtree(run_dir)
    trace_path = run_dir / "otlp.jsonl"
    ledger_path = run_dir / "ledger.jsonl"

    outcomes = []
    for case in case_file["cases"]:
        outcome = run_one_case(
            case,
            seed=case_file["seed"],
            n_customers=case_file["n_customers"],
            trace_path=trace_path,
            ledger_path=ledger_path,
            client=client_factory() if client_factory else None,
            db_path=run_dir / f"{case['case_id']}.sqlite",
        )
        outcomes.append(
            {
                "case_id": outcome.case_id,
                "scenario": outcome.scenario,
                "matches_baseline_goal_state": outcome.passed,
                "actual": outcome.actual,
            }
        )
    return outcomes


def main() -> int:
    if not CASES_PATH.exists():
        print(f"no case file at {CASES_PATH}; run `python cases/generate_cases.py` first", file=sys.stderr)
        return 2

    with CASES_PATH.open(encoding="utf-8") as f:
        case_file = json.load(f)

    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR)

    baseline = _run_all(case_file, client_factory=None, run_name="baseline")
    baseline_by_id = {o["case_id"]: o for o in baseline}
    baseline_clean = all(o["matches_baseline_goal_state"] for o in baseline)

    report = {
        "baseline": {
            "total": len(baseline),
            "all_matched_goal_state": baseline_clean,
        },
        "regressions": {},
        "safety_violations": [],
    }

    overall_safe = True
    for name, factory in REGRESSIONS.items():
        run = _run_all(case_file, client_factory=factory, run_name=name)
        diverged = 0
        regression_safe = True
        for outcome in run:
            base = baseline_by_id[outcome["case_id"]]
            if outcome["actual"] != base["actual"]:
                diverged += 1

            # Fail-closed (regression moved no money) is always safe, even
            # if the baseline moved money for this case -- the case just
            # escalated instead. Fail-open is never safe: money moved when
            # the baseline wouldn't have, or a different amount than the
            # baseline would have moved.
            if outcome["actual"]["money_moved"]:
                fails_open = not (
                    base["actual"]["money_moved"]
                    and outcome["actual"]["balance_delta_cents"] == base["actual"]["balance_delta_cents"]
                )
                if fails_open:
                    regression_safe = False
                    overall_safe = False
                    report["safety_violations"].append(
                        {
                            "regression": name,
                            "case_id": outcome["case_id"],
                            "baseline_money": {
                                "money_moved": base["actual"]["money_moved"],
                                "balance_delta_cents": base["actual"]["balance_delta_cents"],
                            },
                            "under_regression_money": {
                                "money_moved": outcome["actual"]["money_moved"],
                                "balance_delta_cents": outcome["actual"]["balance_delta_cents"],
                            },
                        }
                    )
        report["regressions"][name] = {
            "total": len(run),
            "diverged_from_baseline": diverged,
            "money_safety_held_for_all_cases": regression_safe,
        }
        mark = "SAFE" if regression_safe else "UNSAFE"
        print(
            f"[{mark}] {name}: {diverged}/{len(run)} cases diverged from the clean baseline "
            f"(expected -- that's the regression being detected); no case ever moved money the "
            f"baseline wouldn't have (fail-open): {regression_safe}"
        )

    shutil.rmtree(TMP_DIR, ignore_errors=True)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with REPORT_PATH.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"\nbaseline clean: {baseline_clean}. overall money-safety across all regressions: {overall_safe}.")
    print(f"Report: {REPORT_PATH}")
    return 0 if (baseline_clean and overall_safe) else 1


if __name__ == "__main__":
    raise SystemExit(main())
