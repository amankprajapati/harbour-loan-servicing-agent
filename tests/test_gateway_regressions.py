"""Phase 9: gateway regression pack, exercised through the real agent
loop (not just unit-tested against the wrapper in isolation). Each test
answers the same question for a different fault: given a gateway that
corrupts the model call this way, does the system still fail safe --
escalate, never move unauthorized or mis-amounted money -- or does the
fault slip through?
"""

from __future__ import annotations

import sqlite3

from harbour import agent as agent_module
from harbour import db as db_module
from harbour.gateway_regressions import (
    REGRESSIONS,
    ScrambledArgsClient,
    StrippedSystemPromptClient,
    TruncatedCompletionClient,
)


def _insert_case(
    conn: sqlite3.Connection,
    case_id: str,
    message: str,
    *,
    customer_id: str | None = None,
    loan_id: str | None = None,
    claimed_last4: str | None = None,
    claimed_dob: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO cases (case_id, customer_id, loan_id, customer_message, claimed_last4, claimed_dob, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 0)",
        (case_id, customer_id, loan_id, message, claimed_last4, claimed_dob),
    )
    conn.commit()


def test_registry_has_all_four_regressions():
    assert set(REGRESSIONS) == {
        "stripped_system_prompt",
        "truncated_completions",
        "scrambled_tool_args",
        "unapproved_model_identity",
    }


def test_stripped_system_prompt_cannot_skip_identity_verification(seeded_db: sqlite3.Connection, tmp_path):
    """With the system prompt stripped, the model reaches straight for
    issue_refund with no verify_identity step first. The identity gate
    must still deny it -- the case escalates and no money moves."""
    loan_row = seeded_db.execute("SELECT loan_id, customer_id FROM loans LIMIT 1").fetchone()
    customer = seeded_db.execute(
        "SELECT ssn_last4, dob FROM customers WHERE customer_id = ?", (loan_row["customer_id"],)
    ).fetchone()
    case_id = "CASE-GW-STRIPPED"
    _insert_case(
        seeded_db,
        case_id,
        "please refund $25 for the duplicate fee",
        customer_id=loan_row["customer_id"],
        loan_id=loan_row["loan_id"],
        claimed_last4=customer["ssn_last4"],
        claimed_dob=customer["dob"],
    )
    balance_before = db_module.loan_balance_cents(seeded_db, loan_row["loan_id"])

    result = agent_module.handle_case(
        seeded_db,
        case_id,
        client=StrippedSystemPromptClient(),
        trace_path=tmp_path / "otlp.jsonl",
        ledger_path=tmp_path / "ledger.jsonl",
    )

    assert result.status == "escalated"
    assert not any(h["action"] == "issue_refund" and h["outcome"] == "ok" for h in result.steps)
    balance_after = db_module.loan_balance_cents(seeded_db, loan_row["loan_id"])
    assert balance_after == balance_before


def test_truncated_completion_escalates_instead_of_crashing(seeded_db: sqlite3.Connection, tmp_path):
    """A truncated tool name matches no real tool and no 'finish'. The
    service must escalate cleanly rather than raising out of handle_case
    or silently doing nothing."""
    loan_row = seeded_db.execute("SELECT loan_id, customer_id FROM loans LIMIT 1").fetchone()
    customer = seeded_db.execute(
        "SELECT ssn_last4, dob FROM customers WHERE customer_id = ?", (loan_row["customer_id"],)
    ).fetchone()
    case_id = "CASE-GW-TRUNCATED"
    _insert_case(
        seeded_db,
        case_id,
        "please refund $25 for the duplicate fee",
        customer_id=loan_row["customer_id"],
        loan_id=loan_row["loan_id"],
        claimed_last4=customer["ssn_last4"],
        claimed_dob=customer["dob"],
    )
    balance_before = db_module.loan_balance_cents(seeded_db, loan_row["loan_id"])

    result = agent_module.handle_case(
        seeded_db,
        case_id,
        client=TruncatedCompletionClient(),
        trace_path=tmp_path / "otlp.jsonl",
        ledger_path=tmp_path / "ledger.jsonl",
    )

    assert result.status == "escalated"
    assert not any(h["action"] == "issue_refund" and h["outcome"] == "ok" for h in result.steps)
    balance_after = db_module.loan_balance_cents(seeded_db, loan_row["loan_id"])
    assert balance_after == balance_before


def test_scrambled_tool_args_cannot_redirect_money_to_another_loan(seeded_db: sqlite3.Connection, tmp_path):
    """The gateway corrupts the refund's loan_id and inflates the amount.
    require_identity_verified re-resolves the customer from whatever
    loan_id actually arrives -- a nonexistent one is denied outright, so
    the corrupted call never reaches issue_refund's own amount check."""
    loan_row = seeded_db.execute("SELECT loan_id, customer_id FROM loans LIMIT 1").fetchone()
    customer = seeded_db.execute(
        "SELECT ssn_last4, dob FROM customers WHERE customer_id = ?", (loan_row["customer_id"],)
    ).fetchone()
    case_id = "CASE-GW-SCRAMBLED"
    _insert_case(
        seeded_db,
        case_id,
        "please refund $25 for the duplicate fee",
        customer_id=loan_row["customer_id"],
        loan_id=loan_row["loan_id"],
        claimed_last4=customer["ssn_last4"],
        claimed_dob=customer["dob"],
    )
    balance_before = db_module.loan_balance_cents(seeded_db, loan_row["loan_id"])

    result = agent_module.handle_case(
        seeded_db,
        case_id,
        client=ScrambledArgsClient(),
        trace_path=tmp_path / "otlp.jsonl",
        ledger_path=tmp_path / "ledger.jsonl",
    )

    assert result.status == "escalated"
    assert not any(h["action"] == "issue_refund" and h["outcome"] == "ok" for h in result.steps)
    balance_after = db_module.loan_balance_cents(seeded_db, loan_row["loan_id"])
    assert balance_after == balance_before
    refund_rows = seeded_db.execute(
        "SELECT * FROM transactions WHERE type = 'refund'"
    ).fetchall()
    assert len(refund_rows) == 0


def test_unapproved_model_identity_is_rejected_before_any_tool_call(seeded_db: sqlite3.Connection, tmp_path):
    """policy.md section 4's fix, driven end to end: a gateway that has
    quietly rolled the model snapshot behind the pin must be caught on
    the very first model_call, before any tool ever runs."""
    loan_row = seeded_db.execute("SELECT loan_id, customer_id FROM loans LIMIT 1").fetchone()
    case_id = "CASE-GW-IDENTITY"
    _insert_case(seeded_db, case_id, "what's my balance?", customer_id=loan_row["customer_id"], loan_id=loan_row["loan_id"])

    result = agent_module.handle_case(
        seeded_db,
        case_id,
        client=REGRESSIONS["unapproved_model_identity"](),
        trace_path=tmp_path / "otlp.jsonl",
        ledger_path=tmp_path / "ledger.jsonl",
    )

    assert result.status == "escalated"
    assert result.final_reason == "model_identity_rejected"
    assert len(result.steps) == 0  # rejected before any tool call was ever made
    rejection = seeded_db.execute(
        "SELECT * FROM audit_log WHERE case_id = ? AND event_type = 'model_identity_rejected'", (case_id,)
    ).fetchone()
    assert rejection is not None
