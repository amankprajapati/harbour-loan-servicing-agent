"""Tool 1 of 14: verify_identity.

The only tool that writes to `identity_verifications`. Every other gated
tool reads that table through `policy_engine.is_identity_verified`; none
of them write to it directly, so there is exactly one place a case can
become "verified."
"""

from __future__ import annotations

import time
import uuid

from harbour import db as db_module
from harbour import models
from harbour.policy_engine import ToolContext, audited


@audited
def verify_identity(ctx: ToolContext, customer_id: str, provided_last4: str, provided_dob: str) -> dict:
    """Check a customer-provided last-4-SSN and date of birth against the
    record on file. Always writes a row to identity_verifications --
    including on failure -- so a failed attempt is on the record too."""
    customer = models.get_customer(ctx.conn, customer_id)
    if customer is None:
        raise ValueError(f"no such customer: {customer_id}")

    verified = customer.ssn_last4 == provided_last4 and customer.dob == provided_dob
    verification_id = f"IDV-{uuid.uuid4().hex[:12]}"

    with db_module.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO identity_verifications "
            "(verification_id, case_id, customer_id, method, verified, created_at) "
            "VALUES (?, ?, ?, 'last4_ssn_dob', ?, ?)",
            (verification_id, ctx.case_id, customer_id, int(verified), time.time()),
        )
        db_module.append_audit(
            ctx.conn,
            case_id=ctx.case_id,
            event_type="identity_verified",
            payload={"customer_id": customer_id, "verified": verified, "method": "last4_ssn_dob"},
        )

    return {"verified": verified, "verification_id": verification_id}
