"""SQLite schema and connection helpers for Harbour.

Design notes that matter for the symptoms this build targets:

- `audit_log` is append-only and hash-chained (`prev_hash` -> `hash`), so a
  row cannot be edited or deleted without breaking every hash after it.
  That is what makes "the audit trail is trustworthy evidence" true rather
  than aspirational, and it is what the identity-verification detector in
  `detectors/invariants.py` relies on.
- `audit_log.case_id` plus `audit_log.event_type` is the join key that lets
  the ledger and tracing layers (P4) attribute cost to a case and a tool
  call. There was previously nothing shaped like this, which is the
  concrete cause of symptoms 3 and 4 in ANALYSIS.md.
- Money movement always happens through `transactions`, never by mutating
  `loans.balance_cents` directly from a tool. The balance is a derived,
  recomputed value (see `recompute_balance`), so a bug in one tool cannot
  silently corrupt the ledger of how the balance got where it is.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    email           TEXT NOT NULL,
    phone           TEXT NOT NULL,
    ssn_last4       TEXT NOT NULL,
    dob             TEXT NOT NULL,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS loans (
    loan_id         TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customers(customer_id),
    principal_cents INTEGER NOT NULL,
    interest_rate_bps INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN
                        ('active','delinquent','paid_off','charged_off')),
    origination_date TEXT NOT NULL,
    servicing_note  TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL
);

-- Money only ever moves by inserting a row here. loans has no mutable
-- balance column; balance is SUM(amount_cents) over this table, always
-- recomputed, never cached in a place a tool could drift from reality.
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id  TEXT PRIMARY KEY,
    loan_id         TEXT NOT NULL REFERENCES loans(loan_id),
    case_id         TEXT NOT NULL,
    type            TEXT NOT NULL CHECK (type IN
                        ('disbursement','payment','refund','fee','adjustment')),
    amount_cents    INTEGER NOT NULL,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS disputes (
    dispute_id      TEXT PRIMARY KEY,
    loan_id         TEXT NOT NULL REFERENCES loans(loan_id),
    customer_id     TEXT NOT NULL REFERENCES customers(customer_id),
    case_id         TEXT NOT NULL,
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('open','resolved','rejected')),
    created_at      REAL NOT NULL,
    resolved_at     REAL
);

CREATE TABLE IF NOT EXISTS identity_verifications (
    verification_id TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL,
    customer_id     TEXT NOT NULL REFERENCES customers(customer_id),
    method          TEXT NOT NULL,
    verified        INTEGER NOT NULL CHECK (verified IN (0,1)),
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    case_id         TEXT PRIMARY KEY,
    customer_id     TEXT,
    loan_id         TEXT,
    customer_message TEXT NOT NULL,
    -- Identity credentials the customer supplied at intake, through a
    -- structured field (an authenticated channel or an IVR/web-form
    -- prompt) -- separate from the free-text message. This is what
    -- verify_identity checks against the record on file; it is *not*
    -- parsed out of customer_message, which stays untrusted free text
    -- end to end. Nullable: many cases (balance inquiries, payoff
    -- quotes) never need this.
    claimed_last4   TEXT,
    claimed_dob     TEXT,
    status          TEXT NOT NULL DEFAULT 'received' CHECK (status IN
                        ('received','in_progress','resolved','escalated','refused')),
    goal_state_json TEXT,
    created_at      REAL NOT NULL,
    completed_at    REAL
);

-- Append-only, hash-chained. See module docstring.
CREATE TABLE IF NOT EXISTS audit_log (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id         TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    prev_hash       TEXT NOT NULL,
    hash            TEXT NOT NULL,
    created_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_case ON audit_log(case_id);
CREATE INDEX IF NOT EXISTS idx_txn_loan ON transactions(loan_id);
CREATE INDEX IF NOT EXISTS idx_txn_case ON transactions(case_id);
CREATE INDEX IF NOT EXISTS idx_dispute_loan ON disputes(loan_id);
CREATE INDEX IF NOT EXISTS idx_idv_case ON identity_verifications(case_id);
"""

GENESIS_HASH = "0" * 64


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with the pragmas Harbour relies on.

    `foreign_keys` is on so a bug that references a nonexistent loan or
    customer fails loudly at write time instead of producing an audit
    trail that points at nothing.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: str | Path, *, fresh: bool = False) -> sqlite3.Connection:
    """Create (or open) the database and ensure the schema exists.

    `fresh=True` deletes any existing file first -- used by the eval
    runner and the held-out-case harness so every case starts from an
    identical backend state, per the qualification bar ("a fresh database
    per case").
    """
    path = Path(db_path)
    if fresh and path.exists():
        path.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists():
                sidecar.unlink()
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction boundary so a partially applied case (e.g. a
    money movement whose audit row failed to write) can never be
    committed -- both happen, or neither does."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _hash_row(prev_hash: str, case_id: str, event_type: str, payload_json: str, created_at: float) -> str:
    digest = hashlib.sha256()
    digest.update(prev_hash.encode())
    digest.update(case_id.encode())
    digest.update(event_type.encode())
    digest.update(payload_json.encode())
    digest.update(repr(created_at).encode())
    return digest.hexdigest()


def append_audit(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> str:
    """Append one audit row, chained to the previous row *for this case*.

    Chaining per case (rather than globally) is deliberate: it lets the
    identity-verification detector walk one case's chain in isolation and
    prove a `money_moved` row was preceded by a `identity_verified` row
    with `verified: true`, without needing the whole table.
    """
    cur = conn.execute(
        "SELECT hash FROM audit_log WHERE case_id = ? ORDER BY seq DESC LIMIT 1",
        (case_id,),
    )
    row = cur.fetchone()
    prev_hash = row["hash"] if row else GENESIS_HASH
    created_at = time.time()
    payload_json = json.dumps(payload, sort_keys=True, default=str)
    row_hash = _hash_row(prev_hash, case_id, event_type, payload_json, created_at)
    conn.execute(
        "INSERT INTO audit_log (case_id, event_type, payload_json, prev_hash, hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (case_id, event_type, payload_json, prev_hash, row_hash, created_at),
    )
    return row_hash


def verify_audit_chain(conn: sqlite3.Connection, case_id: str) -> bool:
    """Recompute every hash in a case's chain and confirm it matches what
    was stored. Used by the detectors and by tests; a single corrupted or
    reordered row makes this return False."""
    rows = conn.execute(
        "SELECT event_type, payload_json, prev_hash, hash, created_at "
        "FROM audit_log WHERE case_id = ? ORDER BY seq ASC",
        (case_id,),
    ).fetchall()
    expected_prev = GENESIS_HASH
    for row in rows:
        if row["prev_hash"] != expected_prev:
            return False
        recomputed = _hash_row(
            row["prev_hash"], case_id, row["event_type"], row["payload_json"], row["created_at"]
        )
        if recomputed != row["hash"]:
            return False
        expected_prev = row["hash"]
    return True


def loan_balance_cents(conn: sqlite3.Connection, loan_id: str) -> int:
    """The balance is always this query, never a cached column. See the
    module docstring for why."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM transactions WHERE loan_id = ?",
        (loan_id,),
    ).fetchone()
    return int(row["total"])
