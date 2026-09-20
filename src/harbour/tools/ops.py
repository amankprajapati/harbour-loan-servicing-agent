"""Tools 12-14 of 14: freeze_account, escalate_to_human,
send_customer_message. None of these are gated behind identity
verification -- see policy.md section 1 for the reasoning on each. They
are still fully audited.
"""

from __future__ import annotations

from harbour import db as db_module
from harbour.policy_engine import ToolContext, audited


@audited
def freeze_account(ctx: ToolContext, loan_id: str, reason: str) -> dict:
    """Sets the loan to a frozen state by recording the freeze in the
    audit log and updating loan status. Intentionally reversible only by
    a human (there is no unfreeze tool in this tool set) -- freezing is
    cheap to grant and should be expensive to walk back automatically."""
    row = ctx.conn.execute("SELECT status FROM loans WHERE loan_id = ?", (loan_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such loan: {loan_id}")
    with db_module.transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE loans SET status = 'charged_off' WHERE loan_id = ? AND status != 'charged_off'",
            (loan_id,),
        )
        db_module.append_audit(
            ctx.conn,
            case_id=ctx.case_id,
            event_type="account_frozen",
            payload={"loan_id": loan_id, "reason": reason, "prior_status": row["status"]},
        )
    return {"loan_id": loan_id, "frozen": True}


@audited
def escalate_to_human(ctx: ToolContext, reason: str) -> dict:
    with db_module.transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE cases SET status = 'escalated' WHERE case_id = ?", (ctx.case_id,)
        )
        db_module.append_audit(
            ctx.conn, case_id=ctx.case_id, event_type="escalated", payload={"reason": reason}
        )
    return {"case_id": ctx.case_id, "status": "escalated"}


@audited
def send_customer_message(ctx: ToolContext, message: str) -> dict:
    """Outbound communication. No backend state changes beyond the audit
    row -- this is the tool a real integration would wire to an email or
    SMS provider; here it just records what would have been sent."""
    db_module.append_audit(
        ctx.conn, case_id=ctx.case_id, event_type="message_sent", payload={"message": message}
    )
    return {"sent": True}
