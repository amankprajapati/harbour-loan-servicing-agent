"""Shared pytest fixtures. Every test gets a fresh, seeded, in-memory-ish
SQLite database (a real temp file, since some tests exercise WAL mode and
multiple connections) so tests never share state or ordering."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from harbour import db as db_module
from harbour import seed as seed_module


@pytest.fixture()
def fresh_db(tmp_path):
    db_path = tmp_path / "harbour_test.sqlite"
    conn = db_module.init_db(db_path, fresh=True)
    yield conn
    conn.close()


@pytest.fixture()
def seeded_db(fresh_db):
    seed_module.seed_database(fresh_db, seed=42, n_customers=5)
    fresh_db.commit()
    return fresh_db
