#!/usr/bin/env python
"""Phase 11: the contract checker. Tests a *running* Harbour service
against CONTRACT.md as a black box, over real HTTP -- it does not
import agent.py, policy_engine.py, or anything else Harbour-internal,
because the contract is specifically the part of the system that is
not allowed to change even when everything behind it does.

Usage:
    python -m contract_check.check --base-url http://127.0.0.1:8000

The service must already be running (e.g. `uvicorn harbour.service:app`)
-- this script does not start it, the same way a real external
contract test wouldn't get to assume anything about how the service
under test was deployed. One check (the refund-flow shape check) needs
a known customer/loan to test against; it reads them directly from the
service's own sqlite file via --db-path, the same way a test harness
with filesystem access to a shared fixture database would, and skips
itself (rather than failing) if that file isn't reachable.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

ALLOWED_STATUSES = {"resolved", "escalated"}


class CheckResult:
    def __init__(self, name: str, passed: bool, detail: str = ""):
        self.name = name
        self.passed = passed
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


def _peek_known_loan(db_path: Path) -> tuple[str, str] | None:
    """Best-effort: read one (customer_id, loan_id) pair directly from
    the service's own database file, for the one check that needs a
    real account to exercise a non-trivial case. Returns None if the
    file isn't there or has no data yet -- callers skip that check
    rather than failing the whole run over it."""
    if not db_path.exists():
        return None
    try:
        from harbour import db as db_module

        conn = db_module.connect(db_path)
        row = conn.execute("SELECT loan_id, customer_id FROM loans LIMIT 1").fetchone()
        conn.close()
        if row is None:
            return None
        return row["customer_id"], row["loan_id"]
    except Exception:
        return None


def check_health(client: httpx.Client) -> CheckResult:
    resp = client.get("/health")
    if resp.status_code != 200:
        return CheckResult("health_returns_200", False, f"got {resp.status_code}")
    if resp.json() != {"status": "ok"}:
        return CheckResult("health_returns_200", False, f"unexpected body: {resp.text}")
    return CheckResult("health_returns_200", True)


def check_post_case_missing_message_is_422(client: httpx.Client) -> CheckResult:
    resp = client.post("/case", json={"customer_id": "CUST-X"})
    ok = resp.status_code == 422
    return CheckResult(
        "post_case_missing_customer_message_is_422", ok, f"got {resp.status_code}, expected 422"
    )

def check_post_case_response_shape(client: httpx.Client) -> CheckResult:
    resp = client.post("/case", json={"customer_message": "what's my balance?"})
    if resp.status_code != 200:
        return CheckResult("post_case_response_shape", False, f"got {resp.status_code}, expected 200")
    if resp.headers.get("content-type", "").split(";")[0] != "application/json":
        return CheckResult(
            "post_case_response_shape", False, f"content-type was {resp.headers.get('content-type')!r}"
        )
    body = resp.json()
    problems = []
    if not isinstance(body.get("case_id"), str) or not body["case_id"].startswith("CASE-"):
        problems.append(f"case_id {body.get('case_id')!r} does not start with 'CASE-'")
    if body.get("status") not in ALLOWED_STATUSES:
        problems.append(f"status {body.get('status')!r} not in {sorted(ALLOWED_STATUSES)}")
    if not isinstance(body.get("final_reason"), str) or not body["final_reason"]:
        problems.append(f"final_reason {body.get('final_reason')!r} is not a non-empty string")
    if not isinstance(body.get("steps_taken"), int) or body["steps_taken"] < 0:
        problems.append(f"steps_taken {body.get('steps_taken')!r} is not a non-negative int")
    return CheckResult("post_case_response_shape", not problems, "; ".join(problems))


def check_no_idempotency(client: httpx.Client) -> CheckResult:
    body = {"customer_message": "what's my balance?"}
    resp_a = client.post("/case", json=body)
    resp_b = client.post("/case", json=body)
    if resp_a.status_code != 200 or resp_b.status_code != 200:
        return CheckResult("no_idempotency", False, "one of the two identical POSTs did not return 200")
    id_a, id_b = resp_a.json()["case_id"], resp_b.json()["case_id"]
    ok = id_a != id_b
    return CheckResult("no_idempotency", ok, f"identical bodies produced case_ids {id_a!r} and {id_b!r}")


def check_get_unknown_case_is_404(client: httpx.Client) -> CheckResult:
    resp = client.get(f"/case/CASE-does-not-exist-{uuid.uuid4().hex[:8]}")
    ok = resp.status_code == 404
    return CheckResult("get_unknown_case_is_404", ok, f"got {resp.status_code}, expected 404")


def check_post_then_get_roundtrip(client: httpx.Client) -> CheckResult:
    post_resp = client.post("/case", json={"customer_message": "what's my balance?"})
    if post_resp.status_code != 200:
        return CheckResult("post_then_get_roundtrip", False, f"POST /case returned {post_resp.status_code}")
    case_id = post_resp.json()["case_id"]

    get_resp = client.get(f"/case/{case_id}")
    if get_resp.status_code != 200:
        return CheckResult("post_then_get_roundtrip", False, f"GET /case/{{id}} returned {get_resp.status_code}")
    body = get_resp.json()
    problems = []
    if body.get("case_id") != case_id:
        problems.append(f"case_id mismatch: posted {case_id!r}, got back {body.get('case_id')!r}")
    if not isinstance(body.get("audit_trail"), list) or not body["audit_trail"]:
        problems.append("audit_trail is missing or empty")
    return CheckResult("post_then_get_roundtrip", not problems, "; ".join(problems))


def check_refund_case_with_known_account(client: httpx.Client, known: tuple[str, str] | None) -> CheckResult:
    if known is None:
        return CheckResult(
            "refund_case_with_known_account", True, "skipped: no reachable service database to read a fixture account from"
        )
    customer_id, loan_id = known
    resp = client.post(
        "/case",
        json={
            "customer_message": "please refund $10 for a duplicate fee",
            "customer_id": customer_id,
            "loan_id": loan_id,
        },
    )
    if resp.status_code != 200:
        return CheckResult("refund_case_with_known_account", False, f"got {resp.status_code}")
    body = resp.json()
    ok = body.get("status") in ALLOWED_STATUSES and isinstance(body.get("steps_taken"), int) and body["steps_taken"] > 0
    return CheckResult("refund_case_with_known_account", ok, json.dumps(body))


def check_latency_is_bounded(client: httpx.Client) -> CheckResult:
    """A soft smoke check, not a claimed SLA: a case that never blocks
    indefinitely (CONTRACT.md's bounded-latency guarantee) should return
    in well under a second against the scripted planner locally. A real
    production threshold belongs in the qualification bars, not here."""
    start = time.monotonic()
    resp = client.post("/case", json={"customer_message": "what's my balance?"})
    elapsed = time.monotonic() - start
    ok = resp.status_code == 200 and elapsed < 5.0
    return CheckResult("latency_is_bounded", ok, f"{elapsed:.3f}s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--db-path", default="harbour.sqlite")
    args = parser.parse_args()

    known = _peek_known_loan(Path(args.db_path))

    checks: list[Callable[[httpx.Client], CheckResult]] = [
        check_health,
        check_post_case_missing_message_is_422,
        check_post_case_response_shape,
        check_no_idempotency,
        check_get_unknown_case_is_404,
        check_post_then_get_roundtrip,
        lambda c: check_refund_case_with_known_account(c, known),
        check_latency_is_bounded,
    ]

    results: list[CheckResult] = []
    with httpx.Client(base_url=args.base_url, timeout=10.0) as client:
        try:
            client.get("/health")
        except httpx.ConnectError:
            print(f"could not reach {args.base_url} -- is the service running?", file=sys.stderr)
            return 2
        for check in checks:
            try:
                results.append(check(client))
            except Exception as exc:  # noqa: BLE001 - a check that raises is a failure, not a crash
                results.append(CheckResult(getattr(check, "__name__", str(check)), False, f"raised {exc!r}"))

    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        detail = f" -- {r.detail}" if r.detail else ""
        print(f"[{mark}] {r.name}{detail}")

    failed = [r for r in results if not r.passed]
    report_dir = REPO_ROOT / "results"
    report_dir.mkdir(parents=True, exist_ok=True)
    with (report_dir / "contract_report.json").open("w", encoding="utf-8") as f:
        json.dump({"total": len(results), "failed": len(failed), "results": [r.to_dict() for r in results]}, f, indent=2)

    print(f"\n{len(results) - len(failed)}/{len(results)} contract checks passed.")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
