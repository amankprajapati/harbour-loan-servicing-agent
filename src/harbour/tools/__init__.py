"""The 14-tool registry. This is the single list the planner (agent.py)
is allowed to choose from -- it cannot call a Python function that isn't
in here, which is what makes "the approved action set" in policy.md
section 3 an actual constraint rather than a prompt instruction.
"""

from __future__ import annotations

from typing import Callable

from harbour.policy_engine import ToolContext

from .disputes import raise_dispute, resolve_dispute
from .identity import verify_identity
from .money import apply_fee, issue_refund, post_payment
from .ops import escalate_to_human, freeze_account, send_customer_message
from .reads import (
    calculate_payoff,
    get_customer,
    get_loan,
    get_loans_for_customer,
    get_transaction_history,
)

TOOLS: dict[str, Callable[..., dict]] = {
    "verify_identity": verify_identity,
    "get_customer": get_customer,
    "get_loan": get_loan,
    "get_loans_for_customer": get_loans_for_customer,
    "get_transaction_history": get_transaction_history,
    "calculate_payoff": calculate_payoff,
    "post_payment": post_payment,
    "issue_refund": issue_refund,
    "apply_fee": apply_fee,
    "raise_dispute": raise_dispute,
    "resolve_dispute": resolve_dispute,
    "freeze_account": freeze_account,
    "escalate_to_human": escalate_to_human,
    "send_customer_message": send_customer_message,
}

assert len(TOOLS) == 14, f"expected 14 tools, found {len(TOOLS)}"

# Tools whose first argument after ctx is *not* a loan_id, and so cannot
# go through the identity gate's loan->customer resolution even if
# someone mistakenly decorated them. Used by tests to confirm the gated
# set in policy.md matches the gated set in code.
GATED_TOOLS = frozenset({"post_payment", "issue_refund", "apply_fee", "raise_dispute", "resolve_dispute"})
UNGATED_TOOLS = frozenset(TOOLS) - GATED_TOOLS


def call_tool(name: str, ctx: ToolContext, **kwargs: object) -> dict:
    if name not in TOOLS:
        raise KeyError(f"no such tool: {name}")
    return TOOLS[name](ctx, **kwargs)


__all__ = ["TOOLS", "GATED_TOOLS", "UNGATED_TOOLS", "call_tool"]
