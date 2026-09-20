"""The orchestration loop: reads a case, plans one step at a time via an
LLMClient, executes the chosen tool, and stops when the model finishes,
escalates, or the step budget runs out.

Two things here are load-bearing for the symptoms this build targets,
and are enforced at *this* layer, not just inside the scripted planner:

1. `approved_actions` is computed once, from `cases.customer_message`
   only, before the first tool call -- and every proposed action is
   checked against it on every step, even though the scripted planner
   already tries to respect it. A future real LLM-backed client is not
   trusted to have gotten policy.md section 3 right on its own; the loop
   enforces it regardless of what produced the proposal. This is the
   actual fix for symptom 6, not just the scripted client's cooperation
   with it.
2. `MAX_STEPS` bounds every case. A case that would exceed it is
   escalated, not retried -- this is what stops a case the agent cannot
   solve from turning into the runaway retry loop the cost-cap
   qualification bar exists to catch.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harbour import db as db_module
from harbour import ledger as ledger_module
from harbour import tracing as tracing_module
from harbour.llm_client import LLMClient, ModelIdentityRejected, classify_intent, default_client
from harbour.policy_engine import PolicyDenied, ToolContext, Untrusted, tag_untrusted
from harbour.tools import TOOLS, call_tool

MAX_STEPS = 8

# Maps a tool's parameter name to the case-row column that supplies it,
# for context a planner should never need to invent. `loan_id` and
# `customer_id` are established at intake, the same way a support system
# authenticates which account a channel maps to before an agent (human or
# automated) ever looks at the ticket. `provided_last4`/`provided_dob` are
# what the customer supplied through a structured intake field -- see the
# `cases.claimed_last4`/`claimed_dob` comment in db.py; they are
# deliberately not parsed out of the free-text message. Injected by
# signature, following each tool's `__wrapped__` chain back past
# `@audited`/`@require_identity_verified` to the real parameter list, so a
# planner-supplied value always wins if one was already given.
_CASE_CONTEXT_FIELD_MAP = {
    "loan_id": "loan_id",
    "customer_id": "customer_id",
    "provided_last4": "claimed_last4",
    "provided_dob": "claimed_dob",
}


def _with_case_context(tool_name: str, action_args: dict[str, Any], case_row: Any) -> dict[str, Any]:
    func = TOOLS[tool_name]
    params = inspect.signature(func).parameters
    merged = dict(action_args)
    for param_name, column_name in _CASE_CONTEXT_FIELD_MAP.items():
        if param_name in params and param_name not in merged:
            value = case_row[column_name]
            if value is not None:
                merged[param_name] = value
    return merged


@dataclass
class CaseResult:
    case_id: str
    status: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    final_reason: str = ""


def handle_case(
    conn: db_module.sqlite3.Connection,
    case_id: str,
    *,
    client: LLMClient | None = None,
    trace_path: str | Path = "results/traces/otlp.jsonl",
    ledger_path: str | Path = "results/ledger.jsonl",
) -> CaseResult:
    client = client or default_client()
    case_row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if case_row is None:
        raise ValueError(f"no such case: {case_id}")

    customer_message: Untrusted = tag_untrusted(case_row["customer_message"])
    intent, approved_actions = classify_intent(str(customer_message))

    tracer = tracing_module.Tracer(trace_path, case_id=case_id)
    ledger_writer = ledger_module.LedgerWriter(ledger_path)
    ctx = ToolContext(conn=conn, case_id=case_id)

    db_module.append_audit(
        conn,
        case_id=case_id,
        event_type="case_received",
        payload={"intent": intent, "approved_actions": sorted(approved_actions)},
    )
    conn.commit()

    # `result.steps` and `history` are deliberately the same list object,
    # not copies kept in sync by hand: a step recorded via
    # `history.append(...)` is immediately visible on `result.steps` too,
    # including on every early-return escalation path below. Keeping two
    # separate lists was a real bug (caught by manually smoke-testing the
    # running service, not by a unit test): a case that escalated
    # mid-step reported `steps_taken: 0` because the failing step was
    # appended to `history` but the early `return` skipped the separate
    # `result.steps.append(...)` line.
    history: list[dict[str, Any]] = []
    result = CaseResult(case_id=case_id, status="in_progress", steps=history)

    with tracer.span("case", intent=intent):
        for step_index in range(MAX_STEPS):
            with tracer.span("model_call", step=step_index) as model_span:
                try:
                    response = client.plan_step(
                        case_id=case_id,
                        customer_message=customer_message,
                        approved_actions=approved_actions,
                        history=history,
                    )
                except ModelIdentityRejected as exc:
                    model_span.status = "error"
                    db_module.append_audit(
                        conn,
                        case_id=case_id,
                        event_type="model_identity_rejected",
                        payload={"reported_model_id": exc.reported_model_id},
                    )
                    conn.commit()
                    _escalate(conn, ctx, "unapproved model identity in gateway response")
                    result.status, result.final_reason = "escalated", "model_identity_rejected"
                    return result

            ledger_writer.record(
                ledger_module.LedgerEntry(
                    case_id=case_id,
                    call_id=model_span.span_id,
                    model_id=response.model_id,
                    tokens_in=response.tokens_in,
                    tokens_out=response.tokens_out,
                    cost_usd=ledger_module.estimate_cost_usd(response.tokens_in, response.tokens_out),
                    created_at=0.0,
                )
            )

            # Hard gate, independent of whether the client already tried
            # to respect approved_actions. See module docstring point 1.
            if response.action not in approved_actions and response.action != "finish":
                db_module.append_audit(
                    conn,
                    case_id=case_id,
                    event_type="policy_denied",
                    payload={
                        "reason": "action_outside_approved_set",
                        "proposed_action": response.action,
                        "approved_actions": sorted(approved_actions),
                    },
                )
                conn.commit()
                _escalate(conn, ctx, f"planner proposed {response.action!r}, outside the approved action set")
                result.status, result.final_reason = "escalated", "action_outside_approved_set"
                return result

            if response.action == "finish":
                result.status, result.final_reason = "resolved", "finished"
                break

            call_args = _with_case_context(response.action, response.action_args, case_row)
            with tracer.span("tool_call", tool=response.action) as tool_span:
                try:
                    tool_result = call_tool(response.action, ctx, **call_args)
                    history.append(
                        {"action": response.action, "args": call_args, "result": tool_result, "outcome": "ok"}
                    )
                except PolicyDenied as exc:
                    tool_span.status = "error"
                    history.append(
                        {"action": response.action, "args": call_args, "outcome": "denied", "reason": exc.reason}
                    )
                    conn.commit()
                    if response.action == "escalate_to_human":
                        # escalate_to_human itself should never be denied,
                        # but if it somehow is, fail toward escalation
                        # rather than silently ending the case.
                        pass
                    _escalate(conn, ctx, f"tool {response.action!r} denied: {exc.reason}")
                    result.status, result.final_reason = "escalated", "tool_denied"
                    return result
                except Exception as exc:  # noqa: BLE001 - deliberately broad: any tool
                    # failure escalates rather than crashing the case.
                    tool_span.status = "error"
                    history.append(
                        {"action": response.action, "args": call_args, "outcome": "error", "reason": str(exc)}
                    )
                    conn.commit()
                    _escalate(conn, ctx, f"tool {response.action!r} raised: {exc}")
                    result.status, result.final_reason = "escalated", "tool_error"
                    return result

            conn.commit()

            if response.action == "escalate_to_human":
                result.status, result.final_reason = "escalated", "planner_escalated"
                break
        else:
            _escalate(conn, ctx, f"exceeded step budget ({MAX_STEPS})")
            result.status, result.final_reason = "escalated", "step_budget_exceeded"

    conn.execute(
        "UPDATE cases SET status = ?, completed_at = strftime('%s','now') WHERE case_id = ?",
        (result.status, case_id),
    )
    conn.commit()
    return result


def _escalate(conn: db_module.sqlite3.Connection, ctx: ToolContext, reason: str) -> None:
    call_tool("escalate_to_human", ctx, reason=reason)
    conn.commit()
