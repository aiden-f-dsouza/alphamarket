"""Append-only triggers exist in real Postgres (invariant 3). Runs only when
db_test is up; the SQLite suite covers the ORM guard."""

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.pg


def test_append_only_triggers_are_installed(pg_session_factory):
    with pg_session_factory() as db:
        names = set(db.scalars(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")).all())
    assert any("fills" in n for n in names), names
    assert any("settlements" in n for n in names), names
