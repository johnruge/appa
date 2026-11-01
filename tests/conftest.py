"""Postgres-backed test fixtures.

Database tests run against the database in APPA_TEST_DATABASE_URL (written to .env by
scripts/dev-db.sh init). The schema is built with the real migrations, run as the
owner role; tests connect as the least-privilege app role, like production.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from appa import db, migrate
from appa.config import load_env

load_env()
TEST_URL = os.environ.get("APPA_TEST_DATABASE_URL", "")
TEST_OWNER_URL = os.environ.get("APPA_TEST_MIGRATIONS_DATABASE_URL", "")


@pytest.fixture(scope="session")
def pg_owner():
    if not (TEST_URL and TEST_OWNER_URL):
        pytest.skip("needs APPA_TEST_DATABASE_URL and APPA_TEST_MIGRATIONS_DATABASE_URL (run scripts/dev-db.sh init)")
    for url in (TEST_URL, TEST_OWNER_URL):
        # Tests delete every row: never point them at a real database.
        if not (make_url(url).database or "").endswith("_test"):
            raise RuntimeError(f"refusing to run tests against non-test database {make_url(url).database!r}")
    migrate.downgrade(TEST_OWNER_URL, "base")  # exercises every downgrade...
    migrate.upgrade(TEST_OWNER_URL)  # ...and every upgrade, from scratch
    engine = create_engine(TEST_OWNER_URL)
    yield engine
    engine.dispose()


@pytest.fixture()
def factory(pg_owner):
    with pg_owner.begin() as conn:
        for table in reversed(db.Base.metadata.sorted_tables):
            conn.execute(table.delete())
    return db.sessions(TEST_URL)
