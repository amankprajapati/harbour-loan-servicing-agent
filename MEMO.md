# MEMO.md — project summary

**Status:** All planned phases complete and committed; 100+ tests green;
reproducible end to end via `bash scripts/reproduce.sh`.

## The one thing to know before reading the rest

This is a from-scratch build, not a patch to an existing system. There
was no live model gateway available while building it, so the default
model client is a deterministic scripted planner (see below) — that
substitution is the single most important thing to understand about
what this project does and doesn't prove. Everything else here should be
read as "here's what a hardened version of this kind of agent looks
like," with the one honest caveat stated up front rather than buried.

## What actually got fixed, in one line each

1. **Eval green through an incident** — the published set now spans
   policy edge cases and adversarial input, and a second suite
   (`make gateway-eval`) re-runs it under four simulated gateway faults.
2. **Snapshot rolled, nothing in the repo changed** — every model
   response's identity is checked against an allow-list before use;
   an unapproved identity is refused before any tool runs, loudly.
3 & 4. **Cost unattributable / a day to cost out one case** — every
   model call is logged to a ledger keyed by case ID; cost-per-case is a
   function call. Measured on the published set: 85 model calls, $0.0349
   total, ~$0.0012/case average.
5. **Money moved, no verification on record** — identity verification is
   a code-level precondition on every money-moving tool, not a
   convention; the two facts are written atomically.
6. **Acted on an embedded instruction from untrusted text** — the set of
   actions available to a case is fixed *before* any untrusted text is
   ever read, and enforced as a hard gate at the orchestration layer, not
   trusted to the model's cooperation.

Each of these has a dedicated regression test, and where practical an
independent detector that re-derives the same property from the audit
trail rather than from the code path that's supposed to guarantee it.
Full mapping in ANALYSIS.md.

## Honest limitations

- **The model client is a deterministic scripted planner, not a real
  LLM.** There was no model gateway available while building this. Every
  design decision — the `LLMClient` protocol, the pinning wrapper, the
  intent-then-action-set gate — is written so a real gateway-backed
  client drops in behind the same interface with zero changes downstream
  (`src/harbour/llm_client.py::default_client`), but that swap has not
  actually happened or been tested against a real model. This is the
  single biggest gap between this project and something I'd sign off on
  shipping against production traffic.
- **The 30 published cases and the defect detectors were authored by
  the same person who wrote the fixes**, which is not the same as
  independent verification. They're real, they run, and they genuinely
  catch the defect classes they target (verified by deliberately
  corrupting fixtures and confirming the detectors fire) — but a larger,
  independently authored case set and independently written detectors
  would be a meaningfully stronger bar.
- **The injection defense has a known narrower gap.** It stops untrusted
  text from expanding *which* actions a case can take. It does not
  guard against untrusted text influencing the *parameters* of an action
  already approved for other reasons. Documented in ANALYSIS.md and
  RUNBOOK.md rather than silently left out.

## What I'd do next with more time or a real gateway

1. Wire `default_client()` to an actual `LLM_BASE_URL` and re-run the
   full eval and gateway-regression pack against it — that's the real
   test of whether the architecture holds up off the scripted planner.
2. Get an independent set of eyes (or a genuinely held-out case set) on
   the published cases and detectors, since I wrote both the fixes and
   the checks for them here.
3. Tighten the injection defense to cover parameter-level influence, not
   just action-set expansion, if that's a risk this product actually
   cares about.

Everything referenced above is runnable right now: `bash scripts/reproduce.sh`
from a clean checkout, no GPU, no network.
