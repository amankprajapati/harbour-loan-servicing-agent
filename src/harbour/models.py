"""Typed read helpers over the raw SQLite rows in db.py.

Kept deliberately thin -- these are views for tools and tests to use, not
an ORM layer. Every write still goes through db.py / the tools in
tools/, so there is exactly one place money moves and exactly one place
the audit log gets appended to.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from harbour.db import loan_balance_cents


@dataclass(frozen=True)
class Customer:
    customer_id: str
    name: str
    email: str
    phone: str
    ssn_last4: str
    dob: str


@dataclass(frozen=True)
class Loan:
    loan_id: str
    customer_id: str
    principal_cents: int
    interest_rate_bps: int
    status: str
    origination_date: str
    servicing_note: str
    balance_cents: int


@dataclass(frozen=True)
class Dispute:
    dispute_id: str
    loan_id: str
    customer_id: str
    case_id: str
    reason: str
    status: str


def get_customer(conn: sqlite3.Connection, customer_id: str) -> Customer | None:
    row = conn.execute(
        "SELECT * FROM customers WHERE customer_id = ?", (customer_id,)
    ).fetchone()
    if row is None:
        return None
    return Customer(
        customer_id=row["customer_id"],
        name=row["name"],
        email=row["email"],
        phone=row["phone"],
        ssn_last4=row["ssn_last4"],
        dob=row["dob"],
    )


def get_loan(conn: sqlite3.Connection, loan_id: str) -> Loan | None:
    row = conn.execute("SELECT * FROM loans WHERE loan_id = ?", (loan_id,)).fetchone()
    if row is None:
        return None
    return Loan(
        loan_id=row["loan_id"],
        customer_id=row["customer_id"],
        principal_cents=row["principal_cents"],
        interest_rate_bps=row["interest_rate_bps"],
        status=row["status"],
        origination_date=row["origination_date"],
        servicing_note=row["servicing_note"],
        balance_cents=loan_balance_cents(conn, loan_id),
    )


def get_loans_for_customer(conn: sqlite3.Connection, customer_id: str) -> list[Loan]:
    rows = conn.execute(
        "SELECT loan_id FROM loans WHERE customer_id = ?", (customer_id,)
    ).fetchall()
    loans = [get_loan(conn, row["loan_id"]) for row in rows]
    return [loan for loan in loans if loan is not None]


def get_dispute(conn: sqlite3.Connection, dispute_id: str) -> Dispute | None:
    row = conn.execute(
        "SELECT * FROM disputes WHERE dispute_id = ?", (dispute_id,)
    ).fetchone()
    if row is None:
        return None
    return Dispute(
        dispute_id=row["dispute_id"],
        loan_id=row["loan_id"],
        customer_id=row["customer_id"],
        case_id=row["case_id"],
        reason=row["reason"],
        status=row["status"],
    )
