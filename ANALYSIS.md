# ANALYSIS.md — Harbour, Open Problem 01

## What this document is

PLAN.md said this once and it still governs everything below: there is no
inherited Harbour codebase, published case set, checker, or reachable
model gateway anywhere in this environment. This is not a post-mortem of
someone else's incident. It is a from-scratch build, architected against
the six symptoms the brief describes, with the working hypothesis for
each symptom stated in PLAN.md *before* the corresponding code was
written, and revisited here now that the system exists and has been run.

Every claim below points at a real file, a real test, and — where the
claim is "this is detectable," not just "this is fixed" — a real
detector or regression that was actually run and actually failed on a
corrupted fixture before this write-up was drafted (see
`tests/test_detectors.py` and `tests/test_gateway_regressions.py`).

## Symptom-by-symptom

### 1. Eval green through the incident

**Root cause (hypothesis, confirmed):** a thin, happy-path suite only
tests "does the model follow a clean instruction." It says nothing about
policy edge cases, adversarial input, or a misbehaving gateway — exactly
the three axes an incident actually lives on.

**Fix:** `cases/published/cases.json` (30 cases, standing in for the
brief's 180) spans normal handling across every intent, policy edge
cases (refund over the auto-limit, failed identity verification), and
two adversarial prompt-injection scenarios. `eval/gateway_regression_runner.py`
adds a fourth axis on top: the same 30 cases run again under each of four
simulated gateway faults.

**Evidence:** `make eval` — 30/30 against goal_state. `make gateway-eval`
— baseline 100% correct; under each regression, 7–20 of 30 cases visibly
diverge from the clean baseline (that divergence *is* the signal a green
suite alone would have missed), and the money-safety invariant holds in
every single case across all four regressions (`results/gateway_regression_report.json`).

**Failure mode nobody complained about:** the eval suite as built still
only measures *this build's* 30 authored cases. It was never run against
the brief's real 240 (180 + 60 held-out), because they don't exist here.
A green run of this suite is evidence the mechanisms work against the
scenarios I thought to author — not proof against scenarios I didn't
think of. See MEMO.md.

### 2. Answers changed on a Tuesday; nothing in the repo changed

**Root cause (hypothesis, confirmed):** an unpinned model version, no
response-identity check. If the gateway rolls a snapshot, the service has
no way to know and no reason to refuse it.

**Fix:** `src/harbour/llm_client.py::PinnedModelClient` wraps every
client and checks `response.model_id` against
`policy_engine.APPROVED_MODEL_IDS` before the response is used for
anything. A rejected response raises `ModelIdentityRejected`, which
`agent.py::handle_case` catches on the very first model call, audits as
`model_identity_rejected`, and escalates — before any tool ever runs.

**Evidence:** `tests/test_llm_client.py` (unit level) and
`tests/test_gateway_regressions.py::test_unapproved_model_identity_is_rejected_before_any_tool_call`
(end to end through the real agent loop: `result.final_reason ==
"model_identity_rejected"`, zero tool calls). `detectors/invariants.py::detect_unapproved_model_identity_in_ledger`
re-checks the same property independently, from the cost ledger rather
than the code path, and found zero findings across the full published set.

**Failure mode nobody complained about:** the allow-list
(`APPROVED_MODEL_IDS`) is a static frozenset in code. Rotating it is a
deploy, not a config change — deliberately, since a policy that can be
silently loosened by an unreviewed config edit has the same shape of
problem as the symptom it's fixing. RUNBOOK.md says so explicitly, so
this isn't a surprise at 2am.

### 3 & 4. Bill spikes without traffic spiking; a day spent grepping logs to guess one case's cost

Treated as one symptom with one fix, per PLAN.md's original hypothesis
— confirmed.

**Root cause:** no record ever tied a token count, a model call, or a
dollar figure to a specific case. Both "why did the bill spike" and
"what did this one case cost" are unanswerable for the same reason: there
was no ledger, so there was nothing to query.

**Fix:** `src/harbour/ledger.py::LedgerWriter` appends one JSON line per
model call — `case_id`, `call_id` (joins to a trace span), `model_id`,
token counts, an estimated cost — and `cost_summary_by_case()` turns
"what did case X cost" into one function call instead of a day of
grepping. `src/harbour/tracing.py` gives the same `case_id` join key to
wall-clock spans, OTLP-shaped, so a spike can be correlated to specific
tool calls and their durations, not just dollars.

**Evidence:** `tests/test_agent.py::test_every_case_produces_trace_spans_and_ledger_entries`.
A real number, not a hypothetical: running the full 30-case published set
produced 85 model calls totaling **$0.0349** (~$0.0012/case average, most
expensive single case $0.0018 over 4 calls) — a query against
`results/ledger.jsonl`, not a log grep.
`detectors/invariants.py::detect_unattributed_cost` independently checks
that every ledger entry actually carries a case_id and a non-negative
cost; zero findings.

**Failure mode nobody complained about:** the per-token cost table in
`ledger.py` is a documented, clearly-labelled stand-in (there is no real
gateway to get real provider rates from). The *mechanism* — attribute
every dollar to a case — is real and load-bearing. The specific dollar
figures are not a claim about any real provider's pricing.

### 5. Money moved; no step established who the customer was

**Root cause (hypothesis, confirmed):** identity verification was a
convention the agent was supposed to follow, not a precondition the code
enforced. Either the check ran and wasn't logged, or it silently didn't
run at all before a money-moving tool executed.

**Fix:** `policy_engine.py::require_identity_verified` wraps every
money- and state-changing tool (`post_payment`, `issue_refund`,
`apply_fee`, `raise_dispute`, `resolve_dispute`). The wrapped function's
body cannot execute unless a `verified=1` row already exists in
`identity_verifications` for that case's customer — there is no code path
where the money-moving logic runs and the gate is merely "supposed to
have run before it." The verification outcome and the money movement are
written to the audit log inside the same `db.transaction()` block as the
`transactions` insert (`tools/money.py`), so a crash between the two is
structurally impossible, not just unlikely.

**Evidence:** `tests/test_agent.py::test_refund_case_with_correct_credentials_moves_money`
asserts the ordering directly from the audit log's own sequence numbers,
not from test bookkeeping. `detectors/invariants.py::detect_money_without_verified_identity`
re-derives the same property independently by walking every case's audit
chain; zero findings across the published set, and the detector's own
test (`test_detect_money_without_verified_identity_fires_on_fabricated_event`)
confirms it actually fires on a fabricated `money_moved` row with no
preceding verification.

**Failure mode nobody complained about:** `require_identity_verified`
assumes its wrapped function's first positional argument (after `ctx`) is
a `loan_id` it can resolve to a customer. A tool added later with a
different signature shape would silently bypass the decorator's
resolution logic rather than erroring at call time — this is an
assumption baked into the decorator, not something enforced by a type
system here. Worth a stricter signature check if this file sees a sixth
gated tool.

### 6. The agent acted on an embedded instruction from untrusted free text

**Root cause (hypothesis, confirmed):** a tool's returned data (a
servicing note, in the seeded reproduction) was read into the same
context the model used to decide what to do next, with nothing
distinguishing "the customer asked for this" from "this string, wherever
it came from, said to do this."

**Fix:** two layers, not one. `policy_engine.py::Untrusted` is a real
`str` subtype (`isinstance`-checkable, not a naming convention) applied
to the customer's message at the point it enters the agent loop. More
importantly, the *action set* for a case (`classify_intent()` in
`llm_client.py`) is computed exactly once, from the customer's own
message, before any tool has run and therefore before any untrusted tool
output has ever been read — and `agent.py::handle_case` enforces
`response.action in approved_actions` as a hard gate on *every* step,
independent of whether the client that proposed the action already tried
to respect it. Reading a malicious note can inform which already-approved
action to take; it structurally cannot add a new one.

**Evidence:** `tests/test_agent.py::test_prompt_injection_in_servicing_note_does_not_trigger_refund`
and `test_prompt_injection_even_if_agent_reads_the_note_first` — the
second explicitly proves the agent *does* read the note (via a
legitimate `get_loan` call for a balance inquiry) and still never issues
the refund it asks for. `detectors/invariants.py::detect_action_outside_approved_set`
re-checks the same property from the audit trail across the full
published set (including both injection cases: `PUB-0028`–`PUB-0030`);
zero findings.
`tests/test_gateway_regressions.py::test_stripped_system_prompt_cannot_skip_identity_verification`
additionally proves this holds even when the *model itself* is simulated
to behave with no regard for approved-action scoping at all — the
enforcement lives in the agent loop, not in the model's cooperation.

**Failure mode nobody complained about:** the defense is structural
against expanding the *action set*. It does not, and is not meant to,
prevent the model from being *misled within* an already-approved action
— e.g. an injected note that tries to change a refund's dollar amount or
justification text within a case where a refund really is approved. That
narrower class of injection is out of scope for this build; RUNBOOK.md
flags it as a known gap, not a claim of full coverage.

## General findings that don't map to a single symptom

- **Audit chain tampering** is independently checked (`detect_audit_chain_tampering`)
  because every other detector's evidence depends on the audit_log rows
  being trustworthy in the first place. This is the check that earns that
  trust rather than assuming it.
- **Transaction/audit drift** (`detect_transaction_audit_drift`) verifies
  the "written atomically, by construction" claim in `tools/money.py`'s
  own docstring by comparing the `transactions` table and the audit log
  directly, rather than trusting the docstring. Zero findings; the one
  real bug this style of double-checking caught during the build (a
  directory-creation gap in `db.py::connect`, fixed in Phase 7) is
  documented in `tests/test_db.py::test_init_db_creates_missing_parent_directory`.
