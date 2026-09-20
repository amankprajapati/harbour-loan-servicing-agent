"""Tools 2-6 of 14: read-only lookups. None of these are gated behind
identity verification -- see policy.md section 1 for why -- but they are
still audited, both for the cost ledger and because "what did the agent
look at before it acted" is exactly the kind of question an audit trail
should be able to answer without a database grep.
"""

from __future__ import annotations

from harbour import models
from harbour.policy_engine import ToolContext, Untrusted, audited, tag_untrusted


@audited
def get_customer(ctx: ToolContext, customer_id: str) -> dict:
    customer = models.get_customer(ctx.conn, customer_id)
    if customer is None:
        raise ValueError(f"no such customer: {customer_id}")
    return {
        "customer_id": customer.customer_id,
        "name": customer.name,
        "email": customer.email,
        "phone": customer.phone,
    }


@audited
def get_loan(ctx: ToolContext, loan_id: str) -> dict:
    loan = models.get_loan(ctx.conn, loan_id)
    if loan is None:
        raise ValueError(f"no such loan: {loan_id}")
    note: Untrusted = tag_untrusted(loan.servicing_note)
    return {
        "loan_id": loan.loan_id,
        "customer_id": loan.customer_id,
        "principal_cents": loan.principal_cents,
        "balance_cents": loan.balance_cents,
        "interest_rate_bps": loan.interest_rate_bps,
        "status": loan.status,
        # Deliberately kept as an Untrusted instance in the returned dict
        # rather than coerced back to a plain str: the agent loop checks
        # `isinstance(v, Untrusted)` before folding any tool output into
        # planning context. See policy_engine.py and agent.py.
        "servicing_note": note,
    }


@audited
def get_loans_for_customer(ctx: ToolContext, customer_id: str) -> dict:
    loans = models.get_loans_for_customer(ctx.conn, customer_id)
    return {"loans": [{"loan_id": loan.loan_id, "status": loan.status} for loan in loans]}


@audited
def get_transaction_history(ctx: ToolContext, loan_id: str) -> dict:
    rows = ctx.conn.execute(
        "SELECT transaction_id, type, amount_cents, created_at FROM transactions "
        "WHERE loan_id = ? ORDER BY created_at ASC",
        (loan_id,),
    ).fetchall()
    return {
        "transactions": [
            {
                "transaction_id": row["transaction_id"],
                "type": row["type"],
                "amount_cents": row["amount_cents"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }


@audited
def calculate_payoff(ctx: ToolContext, loan_id: str) -> dict:
    """Payoff = outstanding balance plus a flat processing fee. A
    read-only computation: it does not write a transaction, it only
    tells the caller what one would need to be to close the loan."""
    loan = models.get_loan(ctx.conn, loan_id)
    if loan is None:
        raise ValueError(f"no such loan: {loan_id}")
    processing_fee_cents = 2_500 if loan.balance_cents > 0 else 0
    payoff_cents = max(0, loan.balance_cents) + processing_fee_cents
    return {"loan_id": loan_id, "balance_cents": loan.balance_cents, "payoff_cents": payoff_cents}
