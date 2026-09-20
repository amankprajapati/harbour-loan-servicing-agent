from __future__ import annotations

import sqlite3

import pytest

from harbour import db as db_module
from harbour import policy_engine as pe
from harbour import seed as seed_module
from harbour import tools as tools_module
from harbour.tools.identity import verify_identity


def _make_ctx(conn: sqlite3.Connection, case_id: str) -> pe.ToolContext:
    conn.execute(
        "INSERT INTO cases (case_id, customer_message, created_at) VALUES (?, '', 0)",
        (case_id,),
    )
    return pe.ToolContext(conn=conn, case_id=case_id)


def _verify(conn: sqlite3.Connection, case_id: str, customer_id: str, correct: bool = True) -> None:
    row = conn.execute(
        "SELECT ssn_last4, dob FROM customers WHERE customer_id = ?", (customer_id,)
    ).fetchone()
    last4 = row["ssn_last4"] if correct else "0000"
    dob = row["dob"] if correct else "1900-01-01"
    ctx = pe.ToolContext(conn=conn, case_id=case_id)
    verify_identity(ctx, customer_id, last4, dob)


def test_registry_has_exactly_14_tools():
    assert len(tools_module.TOOLS) == 14


def test_gated_and_ungated_partition_matches_policy_md():
    # policy.md section 1 names these five as gated.
    assert tools_module.GATED_TOOLS == {
        "post_payment",
        "issue_refund",
        "apply_fee",
        "raise_dispute",
        "resolve_dispute",
    }
    assert tools_module.GATED_TOOLS | tools_module.UNGATED_TOOLS == set(tools_module.TOOLS)
    assert tools_module.GATED_TOOLS & tools_module.UNGATED_TOOLS == set()


def test_verify_identity_correct_credentials(seeded_db: sqlite3.Connection):
    row = seeded_db.execute("SELECT customer_id, ssn_last4, dob FROM customers LIMIT 1").fetchone()
    ctx = _make_ctx(seeded_db, "CASE-V1")
    result = verify_identity(ctx, row["customer_id"], row["ssn_last4"], row["dob"])
    assert result["verified"] is True
    assert pe.is_identity_verified(seeded_db, "CASE-V1", row["customer_id"]) is True


def test_verify_identity_wrong_credentials(seeded_db: sqlite3.Connection):
    row = seeded_db.execute("SELECT customer_id FROM customers LIMIT 1").fetchone()
    ctx = _make_ctx(seeded_db, "CASE-V2")
    result = verify_identity(ctx, row["customer_id"], "0000", "1900-01-01")
    assert result["verified"] is False
    assert pe.is_identity_verified(seeded_db, "CASE-V2", row["customer_id"]) is False


@pytest.mark.parametrize("tool_name", sorted(tools_module.GATED_TOOLS))
def test_every_gated_tool_denies_without_verification(seeded_db: sqlite3.Connection, tool_name: str):
    loan_row = seeded_db.execute("SELECT loan_id, customer_id FROM loans LIMIT 1").fetchone()
    ctx = _make_ctx(seeded_db, f"CASE-GATE-{tool_name}")
    tool = tools_module.TOOLS[tool_name]

    kwargs_by_tool = {
        "post_payment": {"amount_cents": 1000},
        "issue_refund": {"amount_cents": 1000, "reason": "test"},
        "apply_fee": {"amount_cents": 500, "reason": "test"},
        "raise_dispute": {"customer_id": loan_row["customer_id"], "reason": "test"},
        "resolve_dispute": {"dispute_id": "DSP-NONEXISTENT", "resolution": "resolved"},
    }
    with pytest.raises(pe.PolicyDenied):
        tool(ctx, loan_row["loan_id"], **kwargs_by_tool[tool_name])


def test_post_payment_reduces_balance(seeded_db: sqlite3.Connection):
    loan_row = seeded_db.execute("SELECT loan_id, customer_id FROM loans LIMIT 1").fetchone()
    case_id = "CASE-PAY-1"
    _make_ctx(seeded_db, case_id)
    _verify(seeded_db, case_id, loan_row["customer_id"])

    balance_before = db_module.loan_balance_cents(seeded_db, loan_row["loan_id"])
    ctx = pe.ToolContext(conn=seeded_db, case_id=case_id)
    result = tools_module.TOOLS["post_payment"](ctx, loan_row["loan_id"], amount_cents=10000)
    balance_after = db_module.loan_balance_cents(seeded_db, loan_row["loan_id"])

    assert balance_after == balance_before - 10000
    assert result["new_balance_cents"] == balance_after

    money_moved_rows = seeded_db.execute(
        "SELECT payload_json FROM audit_log WHERE case_id = ? AND event_type = 'money_moved'", (case_id,)
    ).fetchall()
    assert len(money_moved_rows) == 1


def test_issue_refund_increases_balance_and_is_capped(seeded_db: sqlite3.Connection):
    loan_row = seeded_db.execute("SELECT loan_id, customer_id FROM loans LIMIT 1").fetchone()
    case_id = "CASE-REFUND-1"
    _make_ctx(seeded_db, case_id)
    _verify(seeded_db, case_id, loan_row["customer_id"])
    ctx = pe.ToolContext(conn=seeded_db, case_id=case_id)

    balance_before = db_module.loan_balance_cents(seeded_db, loan_row["loan_id"])
    result = tools_module.TOOLS["issue_refund"](ctx, loan_row["loan_id"], amount_cents=1500, reason="overpaid")
    balance_after = db_module.loan_balance_cents(seeded_db, loan_row["loan_id"])
    assert balance_after == balance_before + 1500
    assert result["new_balance_cents"] == balance_after


def test_issue_refund_over_limit_is_denied(seeded_db: sqlite3.Connection):
    loan_row = seeded_db.execute("SELECT loan_id, customer_id FROM loans LIMIT 1").fetchone()
    case_id = "CASE-REFUND-2"
    _make_ctx(seeded_db, case_id)
    _verify(seeded_db, case_id, loan_row["customer_id"])
    ctx = pe.ToolContext(conn=seeded_db, case_id=case_id)
    with pytest.raises(pe.PolicyDenied):
        tools_module.TOOLS["issue_refund"](
            ctx, loan_row["loan_id"], amount_cents=pe.REFUND_AUTO_LIMIT_CENTS + 1, reason="too big"
        )


def test_issue_refund_second_refund_in_same_case_is_denied(seeded_db: sqlite3.Connection):
    loan_row = seeded_db.execute("SELECT loan_id, customer_id FROM loans LIMIT 1").fetchone()
    case_id = "CASE-REFUND-3"
    _make_ctx(seeded_db, case_id)
    _verify(seeded_db, case_id, loan_row["customer_id"])
    ctx = pe.ToolContext(conn=seeded_db, case_id=case_id)
    tools_module.TOOLS["issue_refund"](ctx, loan_row["loan_id"], amount_cents=1000, reason="first")
    with pytest.raises(pe.PolicyDenied):
        tools_module.TOOLS["issue_refund"](ctx, loan_row["loan_id"], amount_cents=1000, reason="second")


def test_apply_fee_increases_balance(seeded_db: sqlite3.Connection):
    loan_row = seeded_db.execute("SELECT loan_id, customer_id FROM loans LIMIT 1").fetchone()
    case_id = "CASE-FEE-1"
    _make_ctx(seeded_db, case_id)
    _verify(seeded_db, case_id, loan_row["customer_id"])
    ctx = pe.ToolContext(conn=seeded_db, case_id=case_id)
    balance_before = db_module.loan_balance_cents(seeded_db, loan_row["loan_id"])
    tools_module.TOOLS["apply_fee"](ctx, loan_row["loan_id"], amount_cents=2500, reason="late fee")
    balance_after = db_module.loan_balance_cents(seeded_db, loan_row["loan_id"])
    assert balance_after == balance_before + 2500


def test_raise_and_resolve_dispute(seeded_db: sqlite3.Connection):
    loan_row = seeded_db.execute("SELECT loan_id, customer_id FROM loans LIMIT 1").fetchone()
    case_id = "CASE-DSP-1"
    _make_ctx(seeded_db, case_id)
    _verify(seeded_db, case_id, loan_row["customer_id"])
    ctx = pe.ToolContext(conn=seeded_db, case_id=case_id)

    raised = tools_module.TOOLS["raise_dispute"](
        ctx, loan_row["loan_id"], customer_id=loan_row["customer_id"], reason="incorrect fee"
    )
    assert raised["status"] == "open"

    resolved = tools_module.TOOLS["resolve_dispute"](
        ctx, loan_row["loan_id"], dispute_id=raised["dispute_id"], resolution="resolved"
    )
    assert resolved["status"] == "resolved"

    with pytest.raises(pe.PolicyDenied):
        tools_module.TOOLS["resolve_dispute"](
            ctx, loan_row["loan_id"], dispute_id=raised["dispute_id"], resolution="resolved"
        )


def test_freeze_account_does_not_require_verification(seeded_db: sqlite3.Connection):
    loan_row = seeded_db.execute("SELECT loan_id FROM loans LIMIT 1").fetchone()
    case_id = "CASE-FREEZE-1"
    _make_ctx(seeded_db, case_id)
    ctx = pe.ToolContext(conn=seeded_db, case_id=case_id)
    result = tools_module.TOOLS["freeze_account"](ctx, loan_row["loan_id"], reason="suspected fraud")
    assert result["frozen"] is True
    status = seeded_db.execute(
        "SELECT status FROM loans WHERE loan_id = ?", (loan_row["loan_id"],)
    ).fetchone()["status"]
    assert status == "charged_off"


def test_escalate_to_human_does_not_require_verification(seeded_db: sqlite3.Connection):
    case_id = "CASE-ESC-1"
    _make_ctx(seeded_db, case_id)
    ctx = pe.ToolContext(conn=seeded_db, case_id=case_id)
    result = tools_module.TOOLS["escalate_to_human"](ctx, reason="cannot verify caller")
    assert result["status"] == "escalated"


def test_send_customer_message_does_not_require_verification(seeded_db: sqlite3.Connection):
    case_id = "CASE-MSG-1"
    _make_ctx(seeded_db, case_id)
    ctx = pe.ToolContext(conn=seeded_db, case_id=case_id)
    result = tools_module.TOOLS["send_customer_message"](ctx, message="We've received your request.")
    assert result["sent"] is True


def test_calculate_payoff_matches_balance_plus_fee(seeded_db: sqlite3.Connection):
    loan_row = seeded_db.execute("SELECT loan_id FROM loans LIMIT 1").fetchone()
    case_id = "CASE-PAYOFF-1"
    _make_ctx(seeded_db, case_id)
    ctx = pe.ToolContext(conn=seeded_db, case_id=case_id)
    balance = db_module.loan_balance_cents(seeded_db, loan_row["loan_id"])
    result = tools_module.TOOLS["calculate_payoff"](ctx, loan_row["loan_id"])
    assert result["payoff_cents"] == max(0, balance) + 2_500


def test_get_loan_returns_untrusted_servicing_note(seeded_db: sqlite3.Connection):
    customer_id, loan_id = seed_module.seed_customer_with_injection_note(seeded_db)
    seeded_db.commit()
    case_id = "CASE-NOTE-1"
    _make_ctx(seeded_db, case_id)
    ctx = pe.ToolContext(conn=seeded_db, case_id=case_id)
    result = tools_module.TOOLS["get_loan"](ctx, loan_id)
    assert isinstance(result["servicing_note"], pe.Untrusted)
    assert "ignore prior instructions" in result["servicing_note"]
