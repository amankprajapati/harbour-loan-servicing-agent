from __future__ import annotations

from fastapi.testclient import TestClient

from harbour import db as db_module
from harbour.service import create_app


def _client(tmp_path, name: str) -> TestClient:
    app = create_app(db_path=tmp_path / f"{name}.sqlite", fresh_db=True)
    return TestClient(app)


def test_health(tmp_path):
    client = _client(tmp_path, "health")
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_post_case_balance_inquiry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(db_path=tmp_path / "svc.sqlite", fresh_db=True)
    client = TestClient(app)

    conn = db_module.connect(tmp_path / "svc.sqlite")
    loan_row = conn.execute("SELECT loan_id, customer_id FROM loans LIMIT 1").fetchone()
    conn.close()

    resp = client.post(
        "/case",
        json={
            "customer_message": "what's my balance?",
            "customer_id": loan_row["customer_id"],
            "loan_id": loan_row["loan_id"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["case_id"].startswith("CASE-")


def test_post_case_refund_with_correct_credentials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(db_path=tmp_path / "svc2.sqlite", fresh_db=True)
    client = TestClient(app)

    conn = db_module.connect(tmp_path / "svc2.sqlite")
    loan_row = conn.execute("SELECT loan_id, customer_id FROM loans LIMIT 1").fetchone()
    customer = conn.execute(
        "SELECT ssn_last4, dob FROM customers WHERE customer_id = ?", (loan_row["customer_id"],)
    ).fetchone()
    balance_before = db_module.loan_balance_cents(conn, loan_row["loan_id"])
    conn.close()

    resp = client.post(
        "/case",
        json={
            "customer_message": "please refund $10 for a duplicate fee",
            "customer_id": loan_row["customer_id"],
            "loan_id": loan_row["loan_id"],
            "claimed_last4": customer["ssn_last4"],
            "claimed_dob": customer["dob"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"

    conn = db_module.connect(tmp_path / "svc2.sqlite")
    balance_after = db_module.loan_balance_cents(conn, loan_row["loan_id"])
    conn.close()
    assert balance_after == balance_before + 1000


def test_get_case_returns_audit_trail(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(db_path=tmp_path / "svc3.sqlite", fresh_db=True)
    client = TestClient(app)

    conn = db_module.connect(tmp_path / "svc3.sqlite")
    loan_row = conn.execute("SELECT loan_id, customer_id FROM loans LIMIT 1").fetchone()
    conn.close()

    post_resp = client.post(
        "/case",
        json={
            "customer_message": "what's my balance?",
            "customer_id": loan_row["customer_id"],
            "loan_id": loan_row["loan_id"],
        },
    )
    case_id = post_resp.json()["case_id"]

    get_resp = client.get(f"/case/{case_id}")
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["case_id"] == case_id
    assert len(body["audit_trail"]) > 0


def test_get_unknown_case_is_404(tmp_path):
    client = _client(tmp_path, "notfound")
    resp = client.get("/case/CASE-DOES-NOT-EXIST")
    assert resp.status_code == 404


def test_fresh_db_seeds_customers(tmp_path):
    db_path = tmp_path / "seeded.sqlite"
    create_app(db_path=db_path, fresh_db=True)
    conn = db_module.connect(db_path)
    count = conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"]
    conn.close()
    assert count > 0
