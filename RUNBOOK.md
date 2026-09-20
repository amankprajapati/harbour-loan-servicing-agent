# RUNBOOK.md — operating Harbour

Practical reference for running the service and investigating the kind
of incident this build was hardened against. Written for whoever is
on call, not whoever is reading the code.

## Running it

```
pip install -r requirements.txt
bash scripts/reproduce.sh          # everything, one command, exit 0/1
```

Individually, via `make`:

| command              | what it does                                                        |
|-----------------------|----------------------------------------------------------------------|
| `make test`           | offline pytest suite (100+ tests, no network, no running service)   |
| `make cases`          | (re)generates `cases/published/cases.json`                          |
| `make eval`           | runs the published case set against a fresh DB per case             |
| `make gateway-eval`   | runs the same case set under each of 4 simulated gateway faults     |
| `make detect`         | runs the defect detectors against a fresh eval pass                 |
| `make reproduce`      | the full `scripts/reproduce.sh` sequence, including a live service  |

To run the service itself:

```
PYTHONPATH=src python -m uvicorn harbour.service:app --host 127.0.0.1 --port 8000
```

`HARBOUR_DB_PATH` (default `harbour.sqlite`) and `HARBOUR_RESULTS_DIR`
(default `results/`) are the only environment variables the service
reads. A fresh database is seeded automatically the first time it's
opened; it is never re-seeded on later starts unless the file is deleted.

`LLM_BASE_URL` is read by `default_client()` in `llm_client.py` and
**reported but not implemented against** — see MEMO.md for why. Setting
it does not currently change behavior; the service still runs the
scripted planner.

## "The eval went green through an incident" — what to check

A green `make eval` only covers the 30 authored published cases. Before
trusting a green run during an actual incident:

1. Run `make gateway-eval` too. If it's ever *not* SAFE (a case fails
   open — money moves that the clean baseline wouldn't have moved, or a
   different amount), that is a real defect, not a flaky test — do not
   retry it away. `results/gateway_regression_report.json` has the
   exact case and the before/after money outcome.
2. Run `make detect`. Zero findings is a floor, not a ceiling — see
   ANALYSIS.md's per-symptom "failure mode nobody complained about"
   notes for what these detectors do *not* cover.

## "Answers changed and nothing in the repo changed" — model identity

- Check `results/ledger.jsonl` (or run `make detect`, which checks this
  automatically): every entry's `model_id` must be in
  `policy_engine.APPROVED_MODEL_IDS`. If a case's `final_reason` is
  `model_identity_rejected`, that case's audit log has a
  `model_identity_rejected` row recording exactly what was reported and
  when.
- To roll the allow-list forward (accept a new model version
  deliberately): edit `APPROVED_MODEL_IDS` in `src/harbour/policy_engine.py`
  and deploy. This is intentionally not a runtime config flag — a value
  that gates "which model is allowed to move money" should not be
  changeable without a code review, the same review discipline as the
  rest of policy.md.

## "The bill spiked" / "what did this one case cost"

- `harbour.ledger.cost_summary_by_case(ledger_path)` — one function
  call, keyed by `case_id`. No log grepping.
- Cross-reference with `results/traces/otlp.jsonl` via
  `harbour.tracing.read_spans_for_case(trace_path, case_id)` for
  wall-clock duration per tool call, if the spike looks latency-driven
  rather than volume-driven.
- The per-token rate table in `ledger.py` is a documented synthetic
  stand-in, not real provider pricing — dollar figures from this build
  are directionally useful (relative cost between cases), not a claim
  about production billing.

## "Money moved and I can't find a verification step"

- Pull the case's audit trail: `GET /case/{case_id}` (contract-covered)
  or query `audit_log WHERE case_id = ?` directly, ordered by `seq`.
  There must be an `identity_verified` row with `verified: true` at a
  lower `seq` than the `money_moved` row. If there isn't, that's not
  supposed to be reachable — `require_identity_verified` in
  `policy_engine.py` is the only code path that can write a
  `money_moved` row for a gated tool, and it cannot run without a prior
  verified row. Treat this as a real defect and open an incident against
  `policy_engine.py`, not against the specific case.
- `db.verify_audit_chain(conn, case_id)` confirms the audit rows
  themselves haven't been tampered with before trusting any of the
  above.

## "The agent did something nobody asked for"

- Pull `case_received`'s `approved_actions` payload for the case (first
  audit row) and compare against every `tool_call` row with
  `outcome: ok`. Every tool that ran must be in that set —
  `detect_action_outside_approved_set` in `detectors/invariants.py`
  automates exactly this check.
- Known, documented gap: this defense stops the action *set* from being
  expanded by untrusted text. It does not stop untrusted text from
  influencing the *parameters* of an action that was already going to
  run (e.g. nudging a refund's stated reason). See ANALYSIS.md, symptom
  6, "failure mode nobody complained about."

## Known limitations to keep in mind on call

- The default model client is a deterministic scripted planner, not a
  real LLM (see MEMO.md). It cannot handle a request it has no rule for
  — it escalates rather than guessing. A real gateway integration is the
  single biggest thing between this build and production.
- The 30 published cases and the defect detectors are this build's own
  stand-ins for the brief's 180 published cases and private detectors.
  Treat a clean run as "no known regression," not as "verified against
  the real grading harness."
