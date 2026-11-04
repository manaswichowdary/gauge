import os

import pytest

from gauge.db import Base, init_db, make_engine, make_session_factory


@pytest.fixture
def factory(tmp_path):
    """A clean database for each test.

    By default this is a throwaway SQLite file. Set TEST_DATABASE_URL to run the same tests on
    Postgres (CI does this). Tables are dropped first so tests never see each other's rows.
    """
    engine = make_engine(os.getenv("TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/t.db")
    Base.metadata.drop_all(engine)
    init_db(engine)
    yield make_session_factory(engine)
    engine.dispose()
