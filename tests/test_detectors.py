"""Phase 10: defect detectors. Each detector is checked two ways: that
it stays silent on a real, legitimately-produced audit trail, and that
it actually fires when the specific defect it targets is present --
proving these are real checks, not detectors that would pass on
anything.
"""

from __future__ import annotations

import json
import sqlite3

from detectors import invariants
from harbour import agent as agent_module
from harbour import db as db_module


def _insert_case(conn, case_id, message, *, customer_id=None, loan_id=None, claimed_last4=None, claimed_dob=None):
    conn.execute(
        "INSERT INTO cases (case_id, customer_id, loan_id, customer_message, claimed_last4, claimed_dob, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 0)",
        (case_id, customer_id, loan_id, message, claimed_last4, claimed_dob),
    )
    conn.commit()


def test_clean_run_produces_zero_findings_from_any_per_case_detector(seeded_db: sqlite3.Connection, tmp_path):
    loan_row = seeded_db.execute("SELECT loan_id, customer_id FROM loans LIMIT 1").fetchone()
    customer = seeded_db.execute(
        "SELECT ssn_last4, dob FROM customers WHERE customer_id = ?", (loan_row["customer_id"],)
    ).fetchone()
    case_id = "CASE-DETECT-CLEAN"
    _insert_case(
        seeded_db,
        case_id,
        "please refund $25 for the duplicate fee",
        customer_id=loan_row["customer_id"],
        loan_id=loan_row["loan_id"],
        claimed_last4=customer["ssn_last4"],
        claimed_dob=customer["dob"],
    )
    agent_module.handle_case(seeded_db, case_id, trace_path=tmp_path / "otlp.jsonl", ledger_path=tmp_path / "ledger.jsonl")

    for detector in invariants.PER_CASE_DB_DETECTORS:
        assert detector(seeded_db) == [], f"{detector.__name__} found something in a clean run"


def test_detect_money_without_verified_identity_fires_on_fabricated_event(fresh_db: sqlite3.Connection):
    case_id = "CASE-DETECT-1"
    db_module.append_audit(fresh_db, case_id=case_id, event_type="case_received", payload={"intent": "refund"})
    # No identity_verified event at all -- money_moved appears out of nowhere.
    db_module.append_audit(fresh_db, case_id=case_id, event_type="money_moved", payload={"amount_cents": 5000})
    fresh_db.commit()

    findings = invariants.detect_money_without_verified_identity(fresh_db)
    assert len(findings) == 1
    assert findings[0].case_id == case_id


def test_detect_money_without_verified_identity_ignores_failed_verification(fresh_db: sqlite3.Connection):
    case_id = "CASE-DETECT-2"
    db_module.append_audit(fresh_db, case_id=case_id, event_type="identity_verified", payload={"verified": False})
    db_module.append_audit(fresh_db, case_id=case_id, event_type="money_moved", payload={"amount_cents": 5000})
    fresh_db.commit()

    findings = invariants.detect_money_without_verified_identity(fresh_db)
    assert len(findings) == 1


def test_detect_action_outside_approved_set_fires_when_tool_exceeds_scope(fresh_db: sqlite3.Connection):
    case_id = "CASE-DETECT-3"
    db_module.append_audit(
        fresh_db, case_id=case_id, event_type="case_received", payload={"approved_actions": ["get_loan"]}
    )
    db_module.append_audit(
        fresh_db,
        case_id=case_id,
        event_type="tool_call",
        payload={"tool": "issue_refund", "outcome": "ok", "args": {}},
    )
    fresh_db.commit()

    findings = invariants.detect_action_outside_approved_set(fresh_db)
    assert len(findings) == 1
    assert findings[0].evidence["tool"] == "issue_refund"


def test_detect_action_outside_approved_set_ignores_denied_calls(fresh_db: sqlite3.Connection):
    case_id = "CASE-DETECT-4"
    db_module.append_audit(
        fresh_db, case_id=case_id, event_type="case_received", payload={"approved_actions": ["get_loan"]}
    )
    db_module.append_audit(
        fresh_db,
        case_id=case_id,
        event_type="tool_call",
        payload={"tool": "issue_refund", "outcome": "denied", "args": {}},
    )
    fresh_db.commit()

    assert invariants.detect_action_outside_approved_set(fresh_db) == []


def test_detect_audit_chain_tampering_fires_on_edited_row(fresh_db: sqlite3.Connection):
    case_id = "CASE-DETECT-5"
    db_module.append_audit(fresh_db, case_id=case_id, event_type="case_received", payload={"x": 1})
    fresh_db.commit()
    fresh_db.execute(
        "UPDATE audit_log SET payload_json = '{\"x\": 999}' WHERE case_id = ?", (case_id,)
    )
    fresh_db.commit()

    findings = invariants.detect_audit_chain_tampering(fresh_db)
    assert len(findings) == 1
    assert findings[0].case_id == case_id


def test_detect_transaction_audit_drift_fires_on_transaction_with_no_audit_row(seeded_db: sqlite3.Connection):
    loan_row = seeded_db.execute("SELECT loan_id FROM loans LIMIT 1").fetchone()
    case_id = "CASE-DETECT-6"
    # Insert a refund transaction the way a buggy tool that skipped the
    # audit write would -- bypassing issue_refund entirely.
    seeded_db.execute(
        "INSERT INTO transactions (transaction_id, loan_id, case_id, type, amount_cents, created_at) "
        "VALUES ('TXN-DRIFT-1', ?, ?, 'refund', 2500, 0)",
        (loan_row["loan_id"], case_id),
    )
    seeded_db.commit()

    findings = invariants.detect_transaction_audit_drift(seeded_db)
    assert len(findings) == 1
    assert findings[0].evidence["transaction_id"] == "TXN-DRIFT-1"


def test_detect_transaction_audit_drift_fires_on_orphaned_audit_row(fresh_db: sqlite3.Connection):
    case_id = "CASE-DETECT-7"
    db_module.append_audit(
        fresh_db,
        case_id=case_id,
        event_type="money_moved",
        payload={"transaction_id": "TXN-DOES-NOT-EXIST"},
    )
    fresh_db.commit()

    findings = invariants.detect_transaction_audit_drift(fresh_db)
    assert len(findings) == 1
    assert findings[0].evidence["transaction_id"] == "TXN-DOES-NOT-EXIST"


def test_detect_unapproved_model_identity_in_ledger(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    with ledger_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"case_id": "C1", "call_id": "S1", "model_id": "harbour-planner-v1-scripted", "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0, "created_at": 0.0}) + "\n")
        f.write(json.dumps({"case_id": "C1", "call_id": "S2", "model_id": "some-rolled-snapshot-2027-01-01", "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0, "created_at": 0.0}) + "\n")

    findings = invariants.detect_unapproved_model_identity_in_ledger(ledger_path)
    assert len(findings) == 1
    assert findings[0].evidence["model_id"] == "some-rolled-snapshot-2027-01-01"


def test_detect_unattributed_cost(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    with ledger_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"case_id": "", "call_id": "S1", "model_id": "harbour-planner-v1-scripted", "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.001, "created_at": 0.0}) + "\n")
        f.write(json.dumps({"case_id": "C2", "call_id": "S2", "model_id": "harbour-planner-v1-scripted", "tokens_in": 1, "tokens_out": 1, "cost_usd": -0.001, "created_at": 0.0}) + "\n")

    findings = invariants.detect_unattributed_cost(ledger_path)
    assert len(findings) == 2


def test_detect_unattributed_cost_is_silent_on_a_clean_ledger(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    with ledger_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"case_id": "C1", "call_id": "S1", "model_id": "harbour-planner-v1-scripted", "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.001, "created_at": 0.0}) + "\n")

    assert invariants.detect_unattributed_cost(ledger_path) == []
    assert invariants.detect_unapproved_model_identity_in_ledger(ledger_path) == []
