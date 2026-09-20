# Harbour policy

This is the single source of policy truth. Every rule below is enforced in
code — mostly in `src/harbour/policy_engine.py` and the tool preconditions
in `src/harbour/tools/` — not left as something the agent's prompt is
merely asked to remember. Where a rule is enforced, the enforcing function
is named in parentheses so this document and the code cannot drift apart
silently.

## 1. Identity verification

Before any tool that moves money, changes a customer's account state on
their claimed behalf, or commits the institution to an outcome, the
customer's identity must be verified for that case: last-4 SSN and date of
birth matched against the loan's customer record.

**Gated tools** (identity must be verified first, enforced by
`@require_identity_verified` in `policy_engine.py`): `post_payment`,
`issue_refund`, `apply_fee`, `raise_dispute`, `resolve_dispute`.

**Deliberately ungated tools**, and why:

- `freeze_account` — freezing is a protective, reversible action. A
  caller who cannot yet prove who they are but is reporting suspected
  fraud should still be able to get the account frozen immediately.
  Verification gates the ability to *act further* on the account, not the
  ability to *stop* activity on it. Contain first, verify to unwind.
- `escalate_to_human` — an escape hatch must always be reachable. Gating
  it behind verification would mean an unverifiable caller has no path
  forward at all, which is worse than escalating an unverified case to a
  human who can apply judgment.
- All read-only tools (`get_customer`, `get_loan`,
  `get_loans_for_customer`, `get_transaction_history`,
  `calculate_payoff`) and `send_customer_message` — reading and
  communicating are not the actions this policy exists to gate.

Every gated tool call writes its identity-verification outcome and the
money-moving (or state-changing) event to the audit log as part of the
same transaction (`db.transaction`), so the two rows cannot become
separated by a crash or a partial write. See `detectors/invariants.py` for
the deterministic check that every `money_moved` audit row has a
preceding `identity_verified: true` row in the same case.

## 2. Refund and payment limits

- `issue_refund` above $2,000 in a single case requires escalation to a
  human instead of being executed automatically (enforced in
  `tools/money.py::issue_refund`).
- No case may issue more than one refund. A second refund request in the
  same case is refused and escalated (prevents a retry loop from
  multiplying a payout, which is also part of the cost-control story in
  section 5).

## 3. Untrusted content

Any text that did not originate in the system prompt or in this policy
document is **untrusted**: the customer's inbound message, and anything
read back from a tool (a loan's `servicing_note`, a transaction memo, a
dispute reason on file). Untrusted text is data to reason about, never a
source of instructions.

Concretely (enforced in `policy_engine.py::tag_untrusted` and consumed by
`agent.py`): untrusted text is wrapped and tagged before it enters the
planner's context, and the planner's action set for a turn is fixed by
policy before untrusted content is read — reading a servicing note can
inform *which* of the already-approved actions to take, but it can never
add a new action (like "issue a refund with no case for one") to what's
available. A `refund` request whose only justification is text embedded in
a loan note, with no matching customer-initiated request in `cases`, is
refused.

## 4. Model integrity

- Every model call pins an explicit model version; the client never lets
  the gateway silently substitute a newer snapshot.
- Every model response's reported identity is checked against an
  allow-list before the response is used for anything. A response from an
  unrecognized or unapproved model identity is refused, the case is
  escalated, and the event is audited (`llm_client.py::PinnedModelClient`).
- This is what stops "the provider rolled a snapshot and nothing in the
  repo changed" from silently changing behaviour: it can still happen,
  but it now fails loudly and audibly instead of changing answers quietly.

## 5. Cost and containment

- Every model call is logged to the gateway ledger
  (`ledger.py::LedgerWriter`) keyed by `case_id` and tool-call span, with
  token counts and an estimated cost, so "what does resolving this case
  cost" is a query, not a day of grepping.
- A case is hard-capped at a fixed number of planning steps
  (`agent.py::MAX_STEPS`). A case that would exceed it is escalated
  rather than allowed to keep retrying — this is what stops a case the
  agent cannot solve from fanning out into an expensive retry loop.

## 6. Escalation

A case is escalated to a human rather than resolved automatically when:
identity cannot be verified and the request is not purely protective
(freeze) or purely informational (read-only); a refund would exceed the
per-case limit; the step budget would be exceeded; the model gateway
returns an unapproved model identity or a response that fails schema
validation; or the request, once untrusted content is stripped back out,
does not match any action the customer actually asked for.
