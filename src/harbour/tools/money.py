"""Tools 7-9 of 14: post_payment, issue_refund, apply_fee.

Sign convention (see seed.py for the fuller note): `transactions.amount_cents`
is signed from "amount owed by the customer". A payment is negative
(reduces what's owed); a refund or a fee is positive (increases it).
`balance_cents` is always `SUM(amount_cents)` -- there is no other place
that number lives.

All three are gated by `require_identity_verified` and all three write
their `money_moved` audit row in the same `db.transaction()` block as the
`transactions` insert, so a crash between the two is impossible -- either
both are committed or neither is. This, plus the identity gate itself, is
the concrete fix for symptom 5 (money moved with no verification step on
record): the two facts are now written atomically, by construction, not
by convention.
"""

from __future__ import annotations

import uuid

from harbour import db as db_module
from harbour.policy_engine import (
    REFUND_AUTO_LIMIT_CENTS,
    PolicyDenied,
    ToolContext,
    audited,
    refund_within_auto_limit,
    require_identity_verified,
)


@audited
@require_identity_verified
def post_payment(ctx: ToolContext, loan_id: str, amount_cents: int) -> dict:
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")
    transaction_id = f"TXN-{uuid.uuid4().hex[:12]}"
    with db_module.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO transactions (transaction_id, loan_id, case_id, type, amount_cents, created_at) "
            "VALUES (?, ?, ?, 'payment', ?, strftime('%s','now'))",
            (transaction_id, loan_id, ctx.case_id, -amount_cents),
        )
        new_balance = db_module.loan_balance_cents(ctx.conn, loan_id)
        db_module.append_audit(
            ctx.conn,
            case_id=ctx.case_id,
            event_type="money_moved",
            payload={
                "tool": "post_payment",
                "loan_id": loan_id,
                "transaction_id": transaction_id,
                "amount_cents": -amount_cents,
                "new_balance_cents": new_balance,
            },
        )
    return {"transaction_id": transaction_id, "new_balance_cents": new_balance}


@audited
@require_identity_verified
def issue_refund(ctx: ToolContext, loan_id: str, amount_cents: int, reason: str) -> dict:
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")

    if not refund_within_auto_limit(amount_cents):
        db_module.append_audit(
            ctx.conn,
            case_id=ctx.case_id,
            event_type="policy_denied",
            payload={
                "tool": "issue_refund",
                "reason": "over_auto_limit",
                "amount_cents": amount_cents,
                "limit_cents": REFUND_AUTO_LIMIT_CENTS,
            },
        )
        raise PolicyDenied(
            f"refund of {amount_cents} exceeds the {REFUND_AUTO_LIMIT_CENTS}-cent auto limit; escalate"
        )

    prior_refunds = ctx.conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE loan_id = ? AND case_id = ? AND type = 'refund'",
        (loan_id, ctx.case_id),
    ).fetchone()["n"]
    if prior_refunds > 0:
        db_module.append_audit(
            ctx.conn,
            case_id=ctx.case_id,
            event_type="policy_denied",
            payload={"tool": "issue_refund", "reason": "second_refund_in_case"},
        )
        raise PolicyDenied("a refund has already been issued for this case; escalate for a second one")

    transaction_id = f"TXN-{uuid.uuid4().hex[:12]}"
    with db_module.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO transactions (transaction_id, loan_id, case_id, type, amount_cents, created_at) "
            "VALUES (?, ?, ?, 'refund', ?, strftime('%s','now'))",
            (transaction_id, loan_id, ctx.case_id, amount_cents),
        )
        new_balance = db_module.loan_balance_cents(ctx.conn, loan_id)
        db_module.append_audit(
            ctx.conn,
            case_id=ctx.case_id,
            event_type="money_moved",
            payload={
                "tool": "issue_refund",
                "loan_id": loan_id,
                "transaction_id": transaction_id,
                "amount_cents": amount_cents,
                "reason": reason,
                "new_balance_cents": new_balance,
            },
        )
    return {"transaction_id": transaction_id, "new_balance_cents": new_balance}


@audited
@require_identity_verified
def apply_fee(ctx: ToolContext, loan_id: str, amount_cents: int, reason: str) -> dict:
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")
    transaction_id = f"TXN-{uuid.uuid4().hex[:12]}"
    with db_module.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO transactions (transaction_id, loan_id, case_id, type, amount_cents, created_at) "
            "VALUES (?, ?, ?, 'fee', ?, strftime('%s','now'))",
            (transaction_id, loan_id, ctx.case_id, amount_cents),
        )
        new_balance = db_module.loan_balance_cents(ctx.conn, loan_id)
        db_module.append_audit(
            ctx.conn,
            case_id=ctx.case_id,
            event_type="money_moved",
            payload={
                "tool": "apply_fee",
                "loan_id": loan_id,
                "transaction_id": transaction_id,
                "amount_cents": amount_cents,
                "reason": reason,
                "new_balance_cents": new_balance,
            },
        )
    return {"transaction_id": transaction_id, "new_balance_cents": new_balance}


__all__ = ["post_payment", "issue_refund", "apply_fee"]
