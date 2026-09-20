"""Phase 11: runs contract_check's own check functions against the
FastAPI app in-process (via httpx's ASGI transport) so the contract
checker participates in the regular offline test suite, in addition to
having been run against a real `uvicorn` process by hand (see RUNBOOK.md)
-- the same "don't only trust TestClient" discipline this build has
followed since the agent-loop step-count bug it originally caught.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from contract_check import check as contract_check
from harbour import db as db_module
from harbour.service import create_app


@pytest.fixture()
def live_client(tmp_path):
    # TestClient wraps the ASGI app in a way that manages its lifecycle
    # correctly under a plain sync client -- contract_check's own check
    # functions only ever call .get/.post/.headers/.json(), the same
    # surface a real httpx.Client exposes, so they run unmodified here.
    app = create_app(db_path=tmp_path / "contract.sqlite", fresh_db=True)
    client = TestClient(app)
    yield client, tmp_path / "contract.sqlite"
    client.close()


def test_health_check_passes(live_client):
    client, _ = live_client
    assert contract_check.check_health(client).passed


def test_missing_message_check_passes(live_client):
    client, _ = live_client
    assert contract_check.check_post_case_missing_message_is_422(client).passed


def test_response_shape_check_passes(live_client):
    client, _ = live_client
    result = contract_check.check_post_case_response_shape(client)
    assert result.passed, result.detail


def test_no_idempotency_check_passes(live_client):
    client, _ = live_client
    result = contract_check.check_no_idempotency(client)
    assert result.passed, result.detail


def test_unknown_case_404_check_passes(live_client):
    client, _ = live_client
    assert contract_check.check_get_unknown_case_is_404(client).passed


def test_roundtrip_check_passes(live_client):
    client, _ = live_client
    result = contract_check.check_post_then_get_roundtrip(client)
    assert result.passed, result.detail


def test_refund_check_passes_with_a_real_known_account(live_client):
    client, db_path = live_client
    conn = db_module.connect(db_path)
    row = conn.execute("SELECT loan_id, customer_id FROM loans LIMIT 1").fetchone()
    conn.close()
    result = contract_check.check_refund_case_with_known_account(client, (row["customer_id"], row["loan_id"]))
    assert result.passed, result.detail


def test_refund_check_skips_cleanly_with_no_known_account(live_client):
    client, _ = live_client
    result = contract_check.check_refund_case_with_known_account(client, None)
    assert result.passed  # a skip, not a failure


def test_peek_known_loan_returns_none_for_missing_file(tmp_path):
    assert contract_check._peek_known_loan(tmp_path / "does-not-exist.sqlite") is None


def test_intentionally_broken_contract_is_caught(tmp_path):
    """A negative control: point the response-shape check at a service
    whose response is missing a required field, and confirm the checker
    actually fails it rather than passing everything unconditionally."""
    from fastapi import FastAPI

    broken_app = FastAPI()

    @broken_app.get("/health")
    def health():
        return {"status": "ok"}

    @broken_app.post("/case")
    def post_case():
        return {"case_id": "CASE-123"}  # missing status/final_reason/steps_taken

    client = TestClient(broken_app)
    try:
        result = contract_check.check_post_case_response_shape(client)
    finally:
        client.close()
    assert not result.passed
