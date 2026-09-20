# Harbour

A loan-servicing support agent: reads a customer message, looks up the loan,
checks policy, calls tools, and moves money or raises a dispute.

This build exists to satisfy Open Problem 01, "Ship the Thing You
Inherited." See `PLAN.md` for how the work is sequenced and what it does
and doesn't claim, `CONTRACT.md` for the one HTTP contract that has to
survive everything else changing, and `ANALYSIS.md` / `RUNBOOK.md` /
`MEMO.md` for the required write-ups (added in the final phase).

## Status

Phases P0–P12 complete: data model, policy engine, tools, audit/tracing/
ledger, agent loop, service, published cases, eval suite, gateway
regression pack, defect detectors, contract checker, reproduce script.
See `PLAN.md` for the full phase list.

## Quick start

```
pip install -r requirements.txt
bash scripts/reproduce.sh
```

This runs the offline test suite, generates the published case set, runs
the eval suite clean and under each of the four gateway regressions, runs
the defect detectors, starts the service as a real process, and runs the
contract checker against it -- printing one pass/fail summary at the end.
No GPU or network access required: the default model client is a
deterministic scripted planner (see `src/harbour/llm_client.py` for why).

Individual pieces via `make`: `make test`, `make eval`, `make gateway-eval`,
`make detect`, `make reproduce`.
