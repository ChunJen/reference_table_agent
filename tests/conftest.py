import os
import uuid

import pytest
from sqlalchemy import MetaData, inspect, text
from sqlalchemy.schema import DropSchema

from app.config import Settings
from app.db import init_db, make_engine
from app.repository import RefRepository

TEST_DB_URL = os.environ.get("TEST_DB_URL", "")   # <dialect+driver>://<user>:<pwd>@<host>:<port>/<db>


@pytest.fixture
def settings(tmp_path):
    tag = uuid.uuid4().hex[:8]
    return Settings(
        _env_file=None,
        db_url=TEST_DB_URL,
        schema_ref=f"t{tag}_ref", schema_hist=f"t{tag}_hist", schema_meta=f"t{tag}_meta",
        export_dir=tmp_path / "exports",
        create_display_views=True,
    )


@pytest.fixture
def engine(settings):
    if not TEST_DB_URL:
        pytest.skip("TEST_DB_URL not set")
    try:
        eng = make_engine(settings)
        with eng.connect():
            pass
    except Exception as e:  # pragma: no cover
        pytest.skip(f"test database unavailable: {e}")
    yield eng
    # 清掉本次測試的 schema
    with eng.begin() as conn:
        for s in (settings.schema_ref, settings.schema_hist, settings.schema_meta):
            insp = inspect(conn)
            if s not in insp.get_schema_names():
                continue
            q = eng.dialect.identifier_preparer.quote
            for v in insp.get_view_names(schema=s):
                conn.execute(text(f"DROP VIEW {q(s)}.{q(v)}"))
            md = MetaData(schema=s)
            md.reflect(conn, schema=s)
            md.drop_all(conn)
            conn.execute(DropSchema(s))
    eng.dispose()


@pytest.fixture
def repo(engine, settings):
    meta = init_db(engine, settings)
    return RefRepository(engine, settings, meta)
