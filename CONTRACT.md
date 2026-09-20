# CONTRACT.md — Harbour's `POST /case` contract

This is the one thing every other change to this system has to preserve:
`POST /case` keeps its contract; everything behind it is free to change.
It's kept deliberately small — a thin, stable surface is what makes it
possible to change the implementation behind it with confidence — and
written so `contract_check/check.py` can test a running service against
it as a black box, without importing any of Harbour's own code to decide
whether a response is valid.

## Endpoints

### `POST /case`

Submit one customer case for the service to resolve. Synchronous: the
response is the final outcome of that case, not a ticket to poll. There
is no separate "submit" / "poll for result" split -- a case that needs
more than a bounded number of steps escalates to a human rather than
returning a pending state, so the caller never has to wait on it.

**Request body** (`application/json`):

| field              | type          | required | notes                                                                 |
|--------------------|---------------|----------|------------------------------------------------------------------------|
| `customer_message` | string        | yes      | The customer's own free-text message. Treated as untrusted input.     |
| `customer_id`      | string \| null| no       | Known account context from intake, if the channel already has it.     |
| `loan_id`          | string \| null| no       | Same.                                                                  |
| `claimed_last4`    | string \| null| no       | Last 4 of SSN as supplied through a structured intake field.           |
| `claimed_dob`      | string \| null| no       | Date of birth as supplied through a structured intake field.           |

A request missing `customer_message`, or with a non-string value for
any field, is a validation error (`422`), not a `200` with an
escalated case.

**Response** (`200`, `application/json`):

| field          | type    | notes                                                                 |
|----------------|---------|------------------------------------------------------------------------|
| `case_id`      | string  | Always begins with `CASE-`. Unique per request -- see "no idempotency" below. |
| `status`       | string  | One of `resolved`, `escalated`. (`received`/`in_progress`/`refused` are internal-only states never observed in a response, since the endpoint is synchronous.) |
| `final_reason` | string  | A short machine-readable reason code, e.g. `finished`, `planner_escalated`, `tool_denied`, `action_outside_approved_set`, `model_identity_rejected`, `step_budget_exceeded`, `tool_error`. Never empty. |
| `steps_taken`  | integer | `>= 0`. The number of tool-call attempts recorded for this case, including any that were denied or errored -- a case that failed on its first step reports `1`, not `0`. |

**Guarantees a caller can rely on:**

- **No idempotency.** Each `POST /case` creates a new case with a new
  `case_id`, even if the body is byte-identical to a previous request.
  A caller that wants to avoid double-submission is responsible for its
  own dedup before calling this endpoint.
- **Bounded latency.** The service never blocks indefinitely; a case
  that cannot be resolved in a bounded number of steps escalates rather
  than hanging.
- **`status: resolved` never means money moved without a recorded
  verification step**, and never means an action outside the scope the
  service itself approved for the case ran. Those are enforced
  server-side, not something the caller has to check.

### `GET /case/{case_id}`

Look up a previously submitted case.

- `200` with a JSON body containing at least `case_id`, `status`,
  `customer_id`, `loan_id`, and `audit_trail` (a non-empty, ordered list
  of the case's audit events).
- `404` if `case_id` does not exist.

### `GET /health`

- `200` with `{"status": "ok"}`. No auth, no dependencies checked --
  this is a liveness probe, not a readiness probe.

## What is explicitly *not* part of this contract

Everything else: which of the 14 tools exist, `policy.md`'s specific
rules, the database schema, the LLM client interface, the trace/ledger
export formats. Those are implementation and may change freely, per the
brief's own framing -- `contract_check/check.py` deliberately does not
test any of them.
