"""Phase 1 tests: schema, seed determinism, balance derivation, and the
audit hash chain (including that tampering is detected -- this is the
property symptom 5's fix depends on)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from harbour import db as db_module
from harbour import models
from harbour import seed as seed_module


def test_schema_creates_all_tables(fresh_db: sqlite3.Connection):
    tables = {
        row["name"]
        for row in fresh_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    expected = {
        "customers",
        "loans",
        "transactions",
        "disputes",
        "identity_verifications",
        "cases",
        "audit_log",
    }
    assert expected.issubset(tables)


def test_seed_is_deterministic(tmp_path):
    db_a = db_module.init_db(tmp_path / "a.sqlite", fresh=True)
    db_b = db_module.init_db(tmp_path / "b.sqlite", fresh=True)
    seed_module.seed_database(db_a, seed=7, n_customers=6)
    seed_module.seed_database(db_b, seed=7, n_customers=6)
    db_a.commit()
    db_b.commit()

    customers_a = db_a.execute("SELECT * FROM customers ORDER BY customer_id").fetchall()
    customers_b = db_b.execute("SELECT * FROM customers ORDER BY customer_id").fetchall()
    assert [dict(r) for r in customers_a] == [dict(r) for r in customers_b]

    loans_a = db_a.execute("SELECT * FROM loans ORDER BY loan_id").fetchall()
    loans_b = db_b.execute("SELECT * FROM loans ORDER BY loan_id").fetchall()
    assert [dict(r) for r in loans_a] == [dict(r) for r in loans_b]


def test_balance_is_derived_from_transactions_not_cached(seeded_db: sqlite3.Connection):
    loan_row = seeded_db.execute("SELECT loan_id FROM loans LIMIT 1").fetchone()
    loan_id = loan_row["loan_id"]
    balance_before = db_module.loan_balance_cents(seeded_db, loan_id)

    # Insert a payment the "normal" way: as a transactions row.
    seeded_db.execute(
        "INSERT INTO transactions (transaction_id, loan_id, case_id, type, amount_cents, created_at) "
        "VALUES ('TXN-TEST-1', ?, 'CASE-TEST', 'payment', 5000, 0)",
        (loan_id,),
    )
    seeded_db.commit()
    balance_after = db_module.loan_balance_cents(seeded_db, loan_id)
    assert balance_after == balance_before + 5000

    # There is no other column to mutate: loans has no balance_cents
    # field at all, so there is no second, driftable source of truth.
    loan_columns = {row["name"] for row in seeded_db.execute("PRAGMA table_info(loans)").fetchall()}
    assert "balance_cents" not in loan_columns


def test_audit_chain_verifies_when_untampered(fresh_db: sqlite3.Connection):
    case_id = "CASE-1"
    db_module.append_audit(fresh_db, case_id=case_id, event_type="case_received", payload={"x": 1})
    db_module.append_audit(fresh_db, case_id=case_id, event_type="identity_verified", payload={"verified": True})
    db_module.append_audit(fresh_db, case_id=case_id, event_type="money_moved", payload={"amount_cents": 100})
    fresh_db.commit()
    assert db_module.verify_audit_chain(fresh_db, case_id) is True


def test_audit_chain_detects_tampering(fresh_db: sqlite3.Connection):
    case_id = "CASE-2"
    db_module.append_audit(fresh_db, case_id=case_id, event_type="case_received", payload={"x": 1})
    db_module.append_audit(fresh_db, case_id=case_id, event_type="identity_verified", payload={"verified": True})
    fresh_db.commit()

    # Simulate someone editing a row after the fact -- e.g. to make it
    # look like identity was verified when it wasn't.
    fresh_db.execute(
        "UPDATE audit_log SET payload_json = ? WHERE case_id = ? AND event_type = 'identity_verified'",
        ('{"verified": true, "tampered": true}', case_id),
    )
    fresh_db.commit()
    assert db_module.verify_audit_chain(fresh_db, case_id) is False


def test_audit_chain_is_per_case(fresh_db: sqlite3.Connection):
    db_module.append_audit(fresh_db, case_id="CASE-A", event_type="case_received", payload={})
    db_module.append_audit(fresh_db, case_id="CASE-B", event_type="case_received", payload={})
    db_module.append_audit(fresh_db, case_id="CASE-A", event_type="resolved", payload={})
    fresh_db.commit()
    assert db_module.verify_audit_chain(fresh_db, "CASE-A") is True
    assert db_module.verify_audit_chain(fresh_db, "CASE-B") is True


def test_models_get_loan_and_customer(seeded_db: sqlite3.Connection):
    row = seeded_db.execute("SELECT customer_id FROM customers LIMIT 1").fetchone()
    customer = models.get_customer(seeded_db, row["customer_id"])
    assert customer is not None
    assert customer.customer_id == row["customer_id"]

    loans = models.get_loans_for_customer(seeded_db, row["customer_id"])
    assert len(loans) >= 1
    for loan in loans:
        assert loan.balance_cents == db_module.loan_balance_cents(seeded_db, loan.loan_id)


def test_injection_seed_customer_has_untrusted_note(fresh_db: sqlite3.Connection):
    customer_id, loan_id = seed_module.seed_customer_with_injection_note(fresh_db)
    fresh_db.commit()
    loan = models.get_loan(fresh_db, loan_id)
    assert loan is not None
    assert "ignore prior instructions" in loan.servicing_note
