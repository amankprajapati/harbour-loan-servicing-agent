#!/usr/bin/env python
"""Runs the full published case set (via eval/runner.py, as a
subprocess so this script has no import-order dependency on it), then
runs every detector in detectors/invariants.py against the resulting
per-case databases and cost ledger. `make detect` calls this.

Exit code is 0 only if every detector reports zero findings across
every case -- this is the qualification bar PLAN.md's "definition of
done" describes as running the detectors and expecting a clean report,
not just a summary.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from harbour import db as db_module  # noqa: E402
from detectors.invariants import LEDGER_DETECTORS, PER_CASE_DB_DETECTORS  # noqa: E402

EVAL_TMP_DIR = REPO_ROOT / ".eval_tmp"
LEDGER_PATH = REPO_ROOT / "results" / "ledger.jsonl"
REPORT_PATH = REPO_ROOT / "results" / "detector_report.json"


def main() -> int:
    print("running the published case set fresh (eval/runner.py) before detecting...")
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "eval" / "runner.py")])
    if proc.returncode not in (0, 1):
        # 0 = all cases passed goal_state, 1 = some didn't -- either way
        # the run happened and produced real artefacts to detect against.
        # Anything else means the run itself crashed.
        print("eval/runner.py failed to complete; cannot run detectors against a run that didn't happen", file=sys.stderr)
        return 2

    db_paths = sorted(EVAL_TMP_DIR.glob("*.sqlite"))
    if not db_paths:
        print(f"no per-case databases found in {EVAL_TMP_DIR}", file=sys.stderr)
        return 2

    all_findings: list[dict] = []
    for db_path in db_paths:
        conn = db_module.connect(db_path)
        try:
            for detector in PER_CASE_DB_DETECTORS:
                findings = detector(conn)
                all_findings.extend(f.to_dict() for f in findings)
        finally:
            conn.close()

    for detector in LEDGER_DETECTORS:
        findings = detector(LEDGER_PATH)
        all_findings.extend(f.to_dict() for f in findings)

    by_detector: dict[str, int] = {}
    for finding in all_findings:
        by_detector[finding["detector"]] = by_detector.get(finding["detector"], 0) + 1

    report = {
        "databases_checked": len(db_paths),
        "total_findings": len(all_findings),
        "findings_by_detector": by_detector,
        "findings": all_findings,
    }
    REPO_ROOT.joinpath("results").mkdir(parents=True, exist_ok=True)
    with REPORT_PATH.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    if all_findings:
        print(f"[FAIL] {len(all_findings)} finding(s) across {len(db_paths)} databases:")
        for finding in all_findings:
            print(f"  - [{finding['detector']}] case={finding['case_id']}: {finding['description']}")
    else:
        print(f"[PASS] 0 findings across {len(db_paths)} databases and the cost ledger.")
    print(f"Report: {REPORT_PATH}")
    return 0 if not all_findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
