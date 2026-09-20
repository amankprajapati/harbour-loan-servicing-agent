# Harbour

Harbour is a reference implementation of a **loan-servicing support agent
built with real guardrails**: it reads a customer message, looks up the
loan, checks policy, calls tools, and moves money or raises a dispute —
the same shape as any LLM agent given access to a live financial system.

The difference is what surrounds the agent loop. Most agent demos stop at
"the model calls a tool." Harbour is built around the parts that actually
matter once real money and real customer data are involved: every
money-moving action is gated on a verified identity at the code level (not
a prompt instruction), every action a case can take is locked in before
any untrusted text is ever read, every dollar spent is attributable back
to the case that spent it, and the whole audit trail is tamper-evident.

## Why this is useful

If you're building — or reviewing — an agent that's allowed to touch
money, PII, or anything else with real consequences, Harbour is a working
example of the patterns that make that safe to ship, not just a list of
best practices:

- **A concrete answer to "how do I stop prompt injection?"** that isn't
  "tell the model not to." The fix here is structural: the set of actions
  a case may take is computed once, from the customer's own message,
  *before* any tool output (which may contain attacker-controlled text)
  is ever read — and it's enforced as a hard gate in the orchestration
  loop, independent of whether the model cooperates.
- **A pattern for auditable, tamper-evident logging** (a hash-chained
  audit log) that turns "prove identity was checked before this refund
  went out" into a query, not a trust exercise.
- **A pattern for cost attribution** in agentic systems: every model call
  is logged to a ledger keyed by case ID, so "what did resolving this one
  case cost" is a function call instead of a day of grepping logs.
- **A pattern for surviving a bad model-gateway rollout**: every model
  response's identity is checked against an allow-list before it's
  trusted, so a silently-rolled model snapshot fails loudly instead of
  quietly changing behavior.
- **A gateway regression pack** you can reuse: four wrappers that
  simulate real proxy-layer failures (stripped context, truncated
  completions, corrupted tool arguments, spoofed model identity) so you
  can prove your system fails *safe* under each one, not just that it
  works when nothing goes wrong.
- **A worked example of defense-in-depth for AI agents**, end to end:
  policy-as-code, structural prompt-injection resistance, tamper-evident
  audit logging, cost/latency containment, and independent detectors that
  re-derive every one of those properties from evidence rather than
  trusting the code path that's supposed to produce it.

It's a fork-and-adapt starting point for anyone building a support,
operations, or back-office agent that needs these guarantees, and a
concrete reference for the specific problem of "how do I make an LLM
agent that moves money safely."

## What's in the box

- **`src/harbour/`** — the agent: 14 policy-gated tools, a hash-chained
  audit log, an OTLP-style tracer and per-case cost ledger, a pluggable
  LLM client (a deterministic scripted planner by default, since no live
  model gateway is wired in — see `MEMO.md`), and the orchestration loop
  that ties it together.
- **`policy.md`** — the single source of policy truth, with every rule
  mapped to the function that actually enforces it.
- **`cases/`** — a generated, documented set of test cases spanning
  normal handling, policy edge cases, and adversarial prompt-injection
  input.
- **`eval/`** — the eval runner, plus a gateway regression pack that
  re-runs every case under four simulated proxy-layer failures.
- **`detectors/`** — independent, deterministic checks over the audit
  trail and cost ledger that verify the system's own safety claims from
  evidence, not from re-running the code.
- **`contract_check/`** — a black-box HTTP contract checker for the
  service's one public endpoint, runnable against a live process.
- **`tests/`** — 100+ tests covering all of the above.
- **`ANALYSIS.md` / `RUNBOOK.md` / `MEMO.md`** — the design rationale
  (per hardening target: root cause, fix, evidence, known gaps), an
  on-call operational reference, and a one-page honest summary of what's
  solid and what still needs a real model gateway.

## Quick start

```
pip install -r requirements.txt
bash scripts/reproduce.sh
```

This runs the offline test suite, generates the case set, runs the eval
suite clean and under each of the four gateway regressions, runs the
defect detectors, starts the service as a real process, and runs the
contract checker against it — printing one pass/fail summary at the end.
No GPU or network access required: the default model client is a
deterministic scripted planner (see `src/harbour/llm_client.py` for why,
and how to swap in a real one).

Individual pieces via `make`: `make test`, `make eval`, `make gateway-eval`,
`make detect`, `make reproduce`.

To run the service on its own:

```
PYTHONPATH=src python -m uvicorn harbour.service:app --host 127.0.0.1 --port 8000
```

See `CONTRACT.md` for the API surface, `PLAN.md` for the architecture and
build log, and `RUNBOOK.md` for how to operate and debug it.
