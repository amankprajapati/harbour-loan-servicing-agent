#!/usr/bin/env python
"""The eval suite runner. `make eval` calls this. Loads
cases/published/cases.json, runs every case against a fresh seeded
database, checks the resulting backend state against each case's
goal_state, and writes results/eval_report.json as a structured report
suitable for a CI gate.

Exit code is 0 only if every case passed -- this is what makes `make
eval` usable as a gate, not just a report generator.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from harbour.eval_support import run_one_case  # noqa: E402

CASES_PATH = REPO_ROOT / "cases" / "published" / "cases.json"
RESULTS_DIR = REPO_ROOT / "results"
TRACE_PATH = RESULTS_DIR / "traces" / "otlp.jsonl"
LEDGER_PATH = RESULTS_DIR / "ledger.jsonl"
REPORT_PATH = RESULTS_DIR / "eval_report.json"
EVAL_TMP_DIR = REPO_ROOT / ".eval_tmp"


def main() -> int:
    if not CASES_PATH.exists():
        print(f"no case file at {CASES_PATH}; run `python cases/generate_cases.py` first", file=sys.stderr)
        return 2

    with CASES_PATH.open(encoding="utf-8") as f:
        case_file = json.load(f)

    # Clean slate for this run's artefacts, so a stale trace/ledger from a
    # previous run never leaks into this one's report.
    for path in (TRACE_PATH, LEDGER_PATH, REPORT_PATH):
        if path.exists():
            path.unlink()
    if EVAL_TMP_DIR.exists():
        shutil.rmtree(EVAL_TMP_DIR)

    outcomes = []
    for case in case_file["cases"]:
        outcome = run_one_case(
            case,
            seed=case_file["seed"],
            n_customers=case_file["n_customers"],
            trace_path=TRACE_PATH,
            ledger_path=LEDGER_PATH,
            db_path=EVAL_TMP_DIR / f"{case['case_id']}.sqlite",
        )
        outcomes.append(outcome)
        mark = "PASS" if outcome.passed else "FAIL"
        print(f"[{mark}] {outcome.case_id} ({outcome.scenario})")
        for mismatch in outcome.mismatches:
            print(f"       {mismatch}")

    passed = sum(1 for o in outcomes if o.passed)
    failed = len(outcomes) - passed

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "total": len(outcomes),
        "passed": passed,
        "failed": failed,
        "results": [
            {
                "case_id": o.case_id,
                "scenario": o.scenario,
                "passed": o.passed,
                "mismatches": o.mismatches,
                "actual": o.actual,
                "expected": o.expected,
                "agent_status": o.agent_status,
                "agent_final_reason": o.agent_final_reason,
            }
            for o in outcomes
        ],
    }
    with REPORT_PATH.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"\n{passed}/{len(outcomes)} passed. Report: {REPORT_PATH}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
