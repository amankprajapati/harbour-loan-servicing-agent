"""The enforcement layer. Every gate described in policy.md is a function
or decorator here, and every tool that needs a gate imports it from this
module rather than re-implementing the check. That is deliberate: a
policy check that exists in two places drifts, and a drifted check is how
symptom 5 (money moved with no verification step on record) happens in
the first place.
"""

from __future__ import annotations

import functools
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from harbour import db as db_module
from harbour import models

F = TypeVar("F", bound=Callable[..., Any])


class PolicyDenied(Exception):
    """Raised by a gate when a tool call is not allowed to proceed. The
    agent loop catches this, audits it, and escalates -- it never
    swallows it and tries something else silently."""

    def __init__(self, reason: str, *, escalate: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.escalate = escalate


@dataclass
class ToolContext:
    """Carried into every tool call. `case_id` is what the audit log and
    the identity-verification gate key off of; a tool cannot be called
    without one, on purpose -- there is no "just run this against the
    database" path that bypasses auditing."""

    conn: sqlite3.Connection
    case_id: str


def is_identity_verified(conn: sqlite3.Connection, case_id: str, customer_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM identity_verifications "
        "WHERE case_id = ? AND customer_id = ? AND verified = 1 "
        "ORDER BY created_at DESC LIMIT 1",
        (case_id, customer_id),
    ).fetchone()
    return row is not None


def _resolve_customer_id_for_loan(conn: sqlite3.Connection, loan_id: str) -> str | None:
    loan = models.get_loan(conn, loan_id)
    return loan.customer_id if loan else None


def require_identity_verified(func: F) -> F:
    """Decorate a tool whose first positional argument (after `ctx`) is a
    `loan_id`. Resolves the loan's owning customer, checks the identity
    gate, and raises PolicyDenied -- logging the denial -- if it is not
    satisfied. The wrapped tool's body never runs unless this passes, so
    there is no code path where the money-moving logic executes and the
    gate is merely "supposed to have run before it."
    """

    @functools.wraps(func)
    def wrapper(ctx: ToolContext, loan_id: str, *args: Any, **kwargs: Any) -> Any:
        customer_id = _resolve_customer_id_for_loan(ctx.conn, loan_id)
        if customer_id is None:
            raise PolicyDenied(f"no such loan: {loan_id}")
        verified = is_identity_verified(ctx.conn, ctx.case_id, customer_id)
        if not verified:
            db_module.append_audit(
                ctx.conn,
                case_id=ctx.case_id,
                event_type="policy_denied",
                payload={
                    "tool": func.__name__,
                    "reason": "identity_not_verified",
                    "loan_id": loan_id,
                    "customer_id": customer_id,
                },
            )
            raise PolicyDenied(
                f"{func.__name__} requires a verified identity for case {ctx.case_id}"
            )
        return func(ctx, loan_id, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


class Untrusted(str):
    """A string subtype that marks its content as untrusted: it entered
    the system from a customer message or a tool's returned data, not
    from the system prompt or policy.md. Using a distinct type (rather
    than a naming convention) means `isinstance(x, Untrusted)` is a real
    check a reviewer or a test can assert on, not a convention someone
    can forget.

    Untrusted text may be read and reasoned about. It may never be used
    to expand the set of actions already approved for a case -- see
    `agent.py::Planner.approved_actions`, which is computed once from the
    customer's actual message before any untrusted tool output is read,
    and is never mutated afterward.
    """

    __slots__ = ()


def tag_untrusted(text: str) -> Untrusted:
    return Untrusted(text)


def contains_injection_markers(text: str) -> bool:
    """A narrow, explicit heuristic used only for the audit trail (to
    flag a case for human review) -- never used as the actual defense.
    The actual defense is structural: untrusted text cannot expand the
    approved action set, regardless of what it says. This is a secondary
    signal, not the mechanism.
    """
    markers = (
        "ignore prior instructions",
        "ignore previous instructions",
        "disregard the above",
        "system:",
        "new instructions:",
    )
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


# --- Model integrity (policy.md section 4) -------------------------------

APPROVED_MODEL_IDS = frozenset({"harbour-planner-v1", "harbour-planner-v1-scripted"})


def check_model_identity(reported_model_id: str) -> bool:
    return reported_model_id in APPROVED_MODEL_IDS


# --- Refund limits (policy.md section 2) ----------------------------------

REFUND_AUTO_LIMIT_CENTS = 2_000_00


def refund_within_auto_limit(amount_cents: int) -> bool:
    return 0 < amount_cents <= REFUND_AUTO_LIMIT_CENTS


# --- Blanket tool-call auditing -------------------------------------------


def audited(func: F) -> F:
    """Every tool, gated or not, gets one `tool_call` audit row recording
    that it ran and with what arguments (loan/customer/dispute IDs only --
    never raw PII like full SSNs). This is the row the cost ledger and
    tracing layer (P4) join against to attribute a dollar figure to a
    case and a specific call, which is the direct fix for "the bill
    spikes and nobody can say which case caused it."

    Order matters relative to `require_identity_verified`: `audited`
    should wrap the *outermost* function so a denied call is still
    recorded (as a `tool_call` row with `outcome: denied`, followed by
    the gate's own `policy_denied` row) rather than vanishing silently.
    """

    @functools.wraps(func)
    def wrapper(ctx: ToolContext, *args: Any, **kwargs: Any) -> Any:
        safe_kwargs = {k: v for k, v in kwargs.items() if k not in {"ssn", "full_ssn"}}
        try:
            result = func(ctx, *args, **kwargs)
        except PolicyDenied as exc:
            db_module.append_audit(
                ctx.conn,
                case_id=ctx.case_id,
                event_type="tool_call",
                payload={
                    "tool": func.__name__,
                    "args": safe_kwargs,
                    "outcome": "denied",
                    "reason": exc.reason,
                },
            )
            raise
        db_module.append_audit(
            ctx.conn,
            case_id=ctx.case_id,
            event_type="tool_call",
            payload={"tool": func.__name__, "args": safe_kwargs, "outcome": "ok"},
        )
        return result

    return wrapper  # type: ignore[return-value]
