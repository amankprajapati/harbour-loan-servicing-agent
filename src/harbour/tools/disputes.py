"""Tools 10-11 of 14: raise_dispute, resolve_dispute. Both gated: a
dispute is a formal claim tied to a specific customer, and policy treats
filing or resolving one the same as a state-changing action on their
account, not a read."""

from __future__ import annotations

import uuid

from harbour import db as db_module
from harbour.policy_engine import PolicyDenied, ToolContext, audited, require_identity_verified


@audited
@require_identity_verified
def raise_dispute(ctx: ToolContext, loan_id: str, customer_id: str, reason: str) -> dict:
    dispute_id = f"DSP-{uuid.uuid4().hex[:12]}"
    with db_module.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO disputes (dispute_id, loan_id, customer_id, case_id, reason, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'open', strftime('%s','now'))",
            (dispute_id, loan_id, customer_id, ctx.case_id, reason),
        )
        db_module.append_audit(
            ctx.conn,
            case_id=ctx.case_id,
            event_type="dispute_raised",
            payload={"dispute_id": dispute_id, "loan_id": loan_id, "reason": reason},
        )
    return {"dispute_id": dispute_id, "status": "open"}


@audited
@require_identity_verified
def resolve_dispute(ctx: ToolContext, loan_id: str, dispute_id: str, resolution: str) -> dict:
    row = ctx.conn.execute(
        "SELECT status, loan_id FROM disputes WHERE dispute_id = ?", (dispute_id,)
    ).fetchone()
    if row is None:
        raise PolicyDenied(f"no such dispute: {dispute_id}")
    if row["loan_id"] != loan_id:
        raise PolicyDenied("dispute does not belong to the given loan")
    if row["status"] != "open":
        raise PolicyDenied(f"dispute {dispute_id} is already {row['status']}")
    if resolution not in {"resolved", "rejected"}:
        raise ValueError("resolution must be 'resolved' or 'rejected'")

    with db_module.transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE disputes SET status = ?, resolved_at = strftime('%s','now') WHERE dispute_id = ?",
            (resolution, dispute_id),
        )
        db_module.append_audit(
            ctx.conn,
            case_id=ctx.case_id,
            event_type="dispute_resolved",
            payload={"dispute_id": dispute_id, "resolution": resolution},
        )
    return {"dispute_id": dispute_id, "status": resolution}
