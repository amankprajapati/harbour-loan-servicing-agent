# PLAN.md — Harbour architecture and build log

## Design goals, stated plainly

Harbour is built to a specific standard: an AI support agent with access to
real money and real customer data should not be shippable unless six
specific failure modes are structurally impossible, not just discouraged in
a prompt. Those six failure modes — the ones that actually bite teams who
give an LLM agent tool access to a live financial system — are the design
target for this build, from the first commit:

1. An eval suite that stays green through a real incident.
2. Behavior that silently changes when the model provider rolls a new
   snapshot, with nothing in the repo changing.
3. Cost spikes with no way to attribute them to specific traffic.
4. No way to answer "what did resolving this one case cost."
5. Money moving with no enforced, recorded identity-verification step.
6. The agent acting on an instruction smuggled in through untrusted
   free text (a customer message, or a note read back from a tool).

Every claim about *why* a design choice prevents one of these is backed by a
concrete mechanism in the code and, where practical, a test that fails if the
mechanism regresses. ANALYSIS.md restates each target against its fix, with
evidence, at the end of the build.

## Working hypotheses for the six failure modes

Stated up front so the build has a target, not just a shape. Revisited and
either confirmed or revised in ANALYSIS.md once the system exists.

1. **Eval green through an incident.** A 40-case, thin, happy-path suite
   proves the model can follow instructions. It does not exercise policy
   edge cases, adversarial input, or gateway misbehaviour. Green tells you
   almost nothing. → the eval suite needs breadth across those axes, not
   just more happy-path cases.
2. **Answers changed on a Tuesday, nothing in the repo changed.** Classic
   symptom of an unpinned model version plus no response-identity check.
   The provider rolled a snapshot; nothing stopped the service from
   accepting it. → pin the model version in every request and verify the
   identity the provider reports back matches an allow-list, as a hard
   gate, not a log line.
3. **Bill spikes without traffic spiking.** No per-case, per-call cost
   attribution existed, so nobody could isolate it. This is very likely
   the same root cause as (4) — "what does one case cost" is unanswerable
   for the same reason a spike is unattributable: no ledger. → a gateway
   ledger keyed by case ID and tool-call span, queryable after the fact.
4. **A day spent grepping logs to guess a case's cost.** Same fix as (3).
5. **Money moved; no step established who the customer was.** Either the
   verification step ran and wasn't logged, or it didn't run at all before
   the money-moving tool executed. Both are the same underlying defect:
   identity verification is a convention, not an enforced precondition. →
   make it a hard, code-level gate on every money-moving tool, and log the
   gate's outcome as part of the same audit transaction as the money
   movement, so the two rows are inseparable by construction.
6. **Agent did something unrequested right after reading external
   free text.** Prompt injection via a tool-returned field the agent
   treated as instructions instead of data. → any text that did not
   originate in the system prompt or the platform's own policy layer is
   tagged untrusted at the point it enters context, and untrusted text is
   never allowed to expand the action set the policy layer already
   approved for that turn.

## Phases (one local commit each, in order)

- **P0 — scaffold.** This file, repo layout, license-free README stub,
  `.gitignore`. *(this commit)*
- **P1 — data model.** SQLite schema for customers, loans, transactions,
  disputes, audit_log; typed accessors; seed data generator with a fixed
  seed so runs are reproducible.
- **P2 — policy and tools.** `policy.md` as the single source of policy
  truth; 14 tools implementing it; every tool's preconditions expressed in
  code, not just in the doc.
- **P3 — policy engine (the enforcement layer).** The identity-verification
  gate, the untrusted-text tagging and containment, model-identity and
  model-version pinning. This is where symptoms 2, 5 and 6 actually get
  fixed, as import-time-enforced decorators/wrappers the tools cannot
  bypass by construction, not as something the agent prompt is merely
  asked to remember.
- **P4 — audit, tracing, ledger.** Hash-chained append-only audit log;
  OTLP-shaped span export; per-case/per-call token and cost ledger. Fixes
  symptoms 3 and 4.
- **P5 — LLM client and agent loop.** Pluggable client behind one
  interface; a deterministic policy-driven planner as the default
  implementation so the whole system runs and is testable with no network
  access, with the same interface ready to carry a real
  `LLM_BASE_URL`-backed client without changing anything downstream. This
  substitution is the single biggest limitation of this build and is
  called out again in MEMO.md.
- **P6 — service.** `POST /case` FastAPI endpoint implementing the
  production contract documented in `CONTRACT.md`.
- **P7 — cases.** A generated, documented set of cases with `goal_state`,
  spanning normal handling, policy edge cases, and adversarial input.
  Documented as authored, and small by design (30 cases) rather than a
  claim of exhaustive coverage — see ANALYSIS.md for what that does and
  doesn't prove.
- **P8 — eval suite.** Runner plus cases covering the breadth (1) calls
  for; `make eval`; a structured JSON report suitable for a CI gate.
- **P9 — gateway regression pack.** Four regressions applied as proxy
  middleware — stripped system prompt, truncated completions, scrambled
  tool arguments, unapproved model identity — plus eval assertions that
  fail under each and pass clean.
- **P10 — defect detectors.** Deterministic assertions over `audit_log`
  and the trace export that independently re-derive the system's safety
  claims from evidence, separately from the code paths that are supposed
  to guarantee them.
- **P11 — contract and checker.** `CONTRACT.md` plus
  `contract_check/check.py` runnable against the live service.
- **P12 — reproduce script, offline tests, final polish.**
- **P13 — ANALYSIS.md, RUNBOOK.md, MEMO.md.** The design write-ups,
  written last so they describe a system that actually exists.

## What "done" means here

`scripts/reproduce.sh` starts the service, seeds the database, runs the
eval suite clean and under each regression, runs the contract checker, runs
the detectors, and prints a summary — on a plain machine, no GPU, no network
required for the default (non-LLM) run mode.
