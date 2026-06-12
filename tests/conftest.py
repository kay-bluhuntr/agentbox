"""Shared test fixtures.

Tests run against SQLite by default (zero setup). Set
AGENTBOX_TEST_DATABASE_URL to run the identical suite against a real
Postgres — which is exactly what CI does, so dialect-specific behaviour
(timezone handling, JSON columns, advisory locks staying out of the way)
is exercised before merge.
"""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import agentbox.models as models
from agentbox.models import Base


def _test_engine(tmp_path):
    url = os.environ.get("AGENTBOX_TEST_DATABASE_URL")
    if url:
        engine = create_engine(url)
        # Shared database across tests: start each test from a clean schema.
        Base.metadata.drop_all(engine)
    else:
        engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def db(monkeypatch, tmp_path):
    """Point the app at a throwaway database (SQLite locally, Postgres in CI)."""
    engine = _test_engine(tmp_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(models, "_engine", engine)
    monkeypatch.setattr(models, "_session_factory", factory)
    yield factory
    engine.dispose()
