"""POST /case -- the one contract this service has to keep. Documented
in full in CONTRACT.md: a case comes in, the service resolves it
against Harbour's tools and policy, and the caller gets back a result
describing what happened, synchronously. This module implements that
contract; contract_check/check.py tests a running service against it.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from harbour import agent as agent_module
from harbour import db as db_module
from harbour import seed as seed_module

RESULTS_DIR = Path(os.environ.get("HARBOUR_RESULTS_DIR", "results"))
DB_PATH = Path(os.environ.get("HARBOUR_DB_PATH", "harbour.sqlite"))
TRACE_PATH = RESULTS_DIR / "traces" / "otlp.jsonl"
LEDGER_PATH = RESULTS_DIR / "ledger.jsonl"


class CaseRequest(BaseModel):
    customer_message: str
    customer_id: str | None = None
    loan_id: str | None = None
    claimed_last4: str | None = None
    claimed_dob: str | None = None


class CaseResponse(BaseModel):
    case_id: str
    status: str
    final_reason: str
    steps_taken: int


def create_app(*, db_path: str | Path | None = None, fresh_db: bool = False) -> FastAPI:
    """Factory rather than a module-level app instance, so tests (and
    the eval runner, and the held-out-case harness) can each get their
    own database file per the qualification bar ("a fresh database per
    case") without import-order surprises.
    """
    app = FastAPI(title="Harbour")
    resolved_db_path = Path(db_path) if db_path is not None else DB_PATH

    def get_conn() -> db_module.sqlite3.Connection:
        conn = db_module.init_db(resolved_db_path, fresh=False)
        return conn

    if fresh_db or not resolved_db_path.exists():
        conn = db_module.init_db(resolved_db_path, fresh=True)
        seed_module.seed_database(conn)
        conn.commit()
        conn.close()

    @app.post("/case", response_model=CaseResponse)
    def post_case(request: CaseRequest) -> CaseResponse:
        conn = get_conn()
        try:
            case_id = f"CASE-{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO cases (case_id, customer_id, loan_id, customer_message, "
                "claimed_last4, claimed_dob, created_at) VALUES (?, ?, ?, ?, ?, ?, strftime('%s','now'))",
                (
                    case_id,
                    request.customer_id,
                    request.loan_id,
                    request.customer_message,
                    request.claimed_last4,
                    request.claimed_dob,
                ),
            )
            conn.commit()

            result = agent_module.handle_case(
                conn, case_id, trace_path=TRACE_PATH, ledger_path=LEDGER_PATH
            )
            return CaseResponse(
                case_id=result.case_id,
                status=result.status,
                final_reason=result.final_reason,
                steps_taken=len(result.steps),
            )
        finally:
            conn.close()

    @app.get("/case/{case_id}")
    def get_case(case_id: str) -> dict[str, Any]:
        conn = get_conn()
        try:
            row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="case not found")
            audit_rows = conn.execute(
                "SELECT event_type, payload_json, created_at FROM audit_log WHERE case_id = ? ORDER BY seq",
                (case_id,),
            ).fetchall()
            return {
                "case_id": row["case_id"],
                "status": row["status"],
                "customer_id": row["customer_id"],
                "loan_id": row["loan_id"],
                "audit_trail": [dict(r) for r in audit_rows],
            }
        finally:
            conn.close()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
