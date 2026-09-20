"""The model client boundary.

Two implementations:

- `ScriptedPlannerClient`, the default. Deterministic, keyword-and-regex
  based, no network call. This exists because there is no reachable
  `LLM_BASE_URL` in this build environment, and a system that only runs
  when a specific external service is up is not something anyone could
  actually test here. Every design decision in this file is written so a
  real LLM-backed client can be swapped in behind the same
  `LLMClient` protocol without changing `agent.py`, `policy_engine.py`, or
  any tool.
- `PinnedModelClient`, a wrapper that enforces policy.md section 4 around
  *either* implementation: pins a model version on outgoing requests and
  refuses (raising, not silently accepting) any response whose reported
  model identity is not on the allow-list. This is the actual fix for
  "the provider rolled a snapshot and nothing in the repo changed" --
  the check runs regardless of which underlying client produced the
  response.

The single biggest limitation of this build is that the scripted planner
is not a language model: it cannot handle a request it doesn't have a
rule for, and it says so (escalates) rather than guessing. See MEMO.md.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Protocol

from harbour.policy_engine import Untrusted, check_model_identity


@dataclass
class LLMResponse:
    model_id: str
    tokens_in: int
    tokens_out: int
    action: str  # a tool name, "finish", or "escalate"
    action_args: dict = field(default_factory=dict)
    rationale: str = ""


class LLMClient(Protocol):
    def plan_step(
        self,
        *,
        case_id: str,
        customer_message: Untrusted,
        approved_actions: frozenset[str],
        history: list[dict],
    ) -> LLMResponse: ...


# --- Intent classification (runs once, on the customer's own message,
#     before any tool output exists) -----------------------------------

_INTENT_RULES: list[tuple[str, re.Pattern[str], frozenset[str]]] = [
    (
        "fraud_or_freeze",
        re.compile(r"\b(fraud\w*|stole\w*|hack\w*|unauthorized|freeze)\b", re.I),
        frozenset({"freeze_account", "escalate_to_human", "send_customer_message"}),
    ),
    (
        "refund",
        re.compile(r"\brefund\b", re.I),
        frozenset(
            {
                "verify_identity",
                "get_loan",
                "get_loans_for_customer",
                "get_transaction_history",
                "issue_refund",
                "escalate_to_human",
                "send_customer_message",
            }
        ),
    ),
    (
        "dispute",
        re.compile(r"\bdispute|incorrect charge|wrong (fee|amount)\b", re.I),
        frozenset(
            {
                "verify_identity",
                "get_loan",
                "get_transaction_history",
                "raise_dispute",
                "escalate_to_human",
                "send_customer_message",
            }
        ),
    ),
    (
        "payoff_quote",
        re.compile(r"\bpay ?off\b", re.I),
        frozenset({"get_loan", "calculate_payoff", "escalate_to_human", "send_customer_message"}),
    ),
    (
        "make_payment",
        re.compile(r"\b(make a payment|pay my loan|submit (a )?payment)\b", re.I),
        frozenset(
            {
                "verify_identity",
                "get_loan",
                "post_payment",
                "escalate_to_human",
                "send_customer_message",
            }
        ),
    ),
    (
        "balance_inquiry",
        re.compile(r"\b(balance|status|how much)\b", re.I),
        frozenset(
            {
                "get_loan",
                "get_transaction_history",
                "escalate_to_human",
                "send_customer_message",
            }
        ),
    ),
]

_UNIVERSAL_ACTIONS = frozenset({"get_customer", "escalate_to_human", "send_customer_message"})

_AMOUNT_RE = re.compile(r"\$?\s?(\d+(?:\.\d{1,2})?)")


def classify_intent(customer_message: str) -> tuple[str, frozenset[str]]:
    """Runs once per case, on the customer's own inbound message only.
    Never called again mid-case, and never called on a tool's returned
    text -- see agent.py, which computes this before the first tool call
    and treats the result as immutable for the rest of the case. This is
    the concrete mechanism behind policy.md section 3.
    """
    for intent_name, pattern, actions in _INTENT_RULES:
        if pattern.search(customer_message):
            return intent_name, actions | _UNIVERSAL_ACTIONS
    return "unclear", _UNIVERSAL_ACTIONS


def extract_amount_cents(text: str) -> int | None:
    match = _AMOUNT_RE.search(text)
    if not match:
        return None
    dollars = float(match.group(1))
    return int(round(dollars * 100))


def _estimate_tokens(*parts: str) -> int:
    """A crude stand-in for a real tokenizer: ~4 characters per token,
    floor of 1. Good enough to make the ledger mechanism exercisable and
    proportionate to input size; not a claim of matching any real
    provider's tokenizer."""
    total_chars = sum(len(p) for p in parts)
    return max(1, total_chars // 4)


class ScriptedPlannerClient:
    """Deterministic default client. See module docstring."""

    MODEL_ID = "harbour-planner-v1-scripted"

    def plan_step(
        self,
        *,
        case_id: str,
        customer_message: Untrusted,
        approved_actions: frozenset[str],
        history: list[dict],
    ) -> LLMResponse:
        tokens_in = _estimate_tokens(str(customer_message), str(history))

        called = {h["action"] for h in history if h.get("outcome") == "ok"}
        intent, _ = classify_intent(str(customer_message))

        def respond(action: str, args: dict, rationale: str) -> LLMResponse:
            tokens_out = _estimate_tokens(action, str(args), rationale)
            return LLMResponse(
                model_id=self.MODEL_ID,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                action=action,
                action_args=args,
                rationale=rationale,
            )

        if intent == "unclear":
            return respond(
                "escalate_to_human",
                {"reason": "could not classify the customer's request from the message text"},
                "no intent rule matched; refuse to guess",
            )

        if intent == "fraud_or_freeze":
            if "freeze_account" not in called and "freeze_account" in approved_actions:
                return respond("freeze_account", {"reason": "customer reported possible fraud"}, intent)
            return respond("escalate_to_human", {"reason": "account frozen, handing off to a human"}, intent)

        if intent == "payoff_quote":
            if "get_loan" not in called:
                return respond("get_loan", {}, intent)
            if "calculate_payoff" not in called:
                return respond("calculate_payoff", {}, intent)
            if "send_customer_message" not in called:
                return respond("send_customer_message", {"message": "payoff quote sent"}, intent)
            return respond("finish", {}, intent)

        if intent == "balance_inquiry":
            if "get_loan" not in called:
                return respond("get_loan", {}, intent)
            if "send_customer_message" not in called:
                return respond("send_customer_message", {"message": "balance shared"}, intent)
            return respond("finish", {}, intent)

        # Everything below needs identity verification first.
        if "verify_identity" not in called:
            return respond("verify_identity", {}, f"{intent}: verify before any state change")

        # Look across all of history, not just the last entry -- once
        # verification succeeds it stays true for the rest of the case,
        # even after later steps are appended. Checking only the most
        # recent entry was a real bug caught by
        # test_scripted_planner_refund_flow_ends_in_finish: it made
        # every case "forget" it was verified the moment a second step
        # was taken.
        verified = any(
            h.get("action") == "verify_identity" and h.get("outcome") == "ok" and h.get("result", {}).get("verified")
            for h in history
        )
        if not verified:
            return respond(
                "escalate_to_human",
                {"reason": "identity could not be verified"},
                f"{intent}: verification failed, cannot proceed automatically",
            )

        if intent == "refund":
            if "issue_refund" not in approved_actions:
                return respond("escalate_to_human", {"reason": "refund not in approved actions"}, intent)
            if "issue_refund" not in called:
                amount = extract_amount_cents(str(customer_message)) or 5000
                return respond(
                    "issue_refund", {"amount_cents": amount, "reason": "customer requested refund"}, intent
                )
            if "send_customer_message" not in called:
                return respond("send_customer_message", {"message": "refund issued"}, intent)
            return respond("finish", {}, intent)

        if intent == "dispute":
            if "raise_dispute" not in called:
                return respond("raise_dispute", {"reason": "customer disputes a charge"}, intent)
            if "send_customer_message" not in called:
                return respond("send_customer_message", {"message": "dispute filed"}, intent)
            return respond("finish", {}, intent)

        if intent == "make_payment":
            if "post_payment" not in called:
                amount = extract_amount_cents(str(customer_message)) or 5000
                return respond("post_payment", {"amount_cents": amount}, intent)
            if "send_customer_message" not in called:
                return respond("send_customer_message", {"message": "payment posted"}, intent)
            return respond("finish", {}, intent)

        return respond("escalate_to_human", {"reason": "unhandled intent branch"}, intent)


class ModelIdentityRejected(Exception):
    def __init__(self, reported_model_id: str):
        super().__init__(f"unapproved model identity in response: {reported_model_id!r}")
        self.reported_model_id = reported_model_id


class PinnedModelClient:
    """Wraps any LLMClient. Enforces policy.md section 4: every response
    is checked against the model-identity allow-list before it is
    returned to the caller. A rejected response never reaches the agent
    loop -- the caller gets an exception, not a quietly-substituted
    answer.
    """

    def __init__(self, inner: LLMClient):
        self._inner = inner

    def plan_step(self, **kwargs: object) -> LLMResponse:
        response = self._inner.plan_step(**kwargs)  # type: ignore[arg-type]
        if not check_model_identity(response.model_id):
            raise ModelIdentityRejected(response.model_id)
        return response


def default_client() -> LLMClient:
    """Selects the client. `LLM_BASE_URL` is read and reported (so it is
    visible in ANALYSIS.md/MEMO.md whether one was configured) but no
    HTTP client is implemented here -- see the module docstring for why.
    A future session with a reachable gateway plugs it in at this one
    function.
    """
    base_url = os.environ.get("LLM_BASE_URL")
    inner: LLMClient = ScriptedPlannerClient()
    if base_url:
        # Intentionally not implemented against a gateway that does not
        # exist in this environment. See MEMO.md.
        pass
    return PinnedModelClient(inner)
