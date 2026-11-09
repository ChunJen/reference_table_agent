"""Engine + metadata tables. Dialect-agnostic (SQLAlchemy Core)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import (
    BigInteger, Column, DateTime, Integer, MetaData, Table, Unicode, UnicodeText,
    create_engine, inspect, text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateSchema

from .config import Settings

# System columns stamped on every reference-table row.
SYS_UPDATED_AT = "sys_updated_at"
SYS_UPDATED_BY = "sys_updated_by"
SYS_VERSION = "sys_version"
HIST_VERSION = "hist_version"   # snapshot version column in ref_hist tables
SYSTEM_COLUMNS = (SYS_UPDATED_AT, SYS_UPDATED_BY, SYS_VERSION)


def make_engine(settings: Settings) -> Engine:
    kwargs: dict = {"pool_pre_ping": True, "future": True, **settings.db_engine_options}
    return create_engine(settings.sqlalchemy_url(), **kwargs)


def now_local(settings: Settings) -> datetime:
    """Naive local timestamp (報表顯示較直覺，不帶時區)."""
    return datetime.now(ZoneInfo(settings.timezone)).replace(tzinfo=None, microsecond=0)


class MetaTables:
    """Agent's own bookkeeping tables in schema_meta."""

    def __init__(self, schema: str):
        self.metadata = MetaData(schema=schema)
        m = self.metadata
        self.registry = Table(
            "ref_registry", m,
            Column("table_name", Unicode(64), primary_key=True),
            Column("display_name", Unicode(200), nullable=False),
            Column("description", UnicodeText),
            Column("key_columns", UnicodeText, nullable=False),     # JSON list
            Column("columns_json", UnicodeText, nullable=False),    # JSON list[ColumnSpec]
            Column("current_version", Integer, nullable=False),
            Column("status", Unicode(20), nullable=False, default="active"),
            Column("created_at", DateTime, nullable=False),
            Column("created_by", Unicode(128), nullable=False),
            Column("updated_at", DateTime, nullable=False),
            Column("updated_by", Unicode(128), nullable=False),
        )
        self.version_log = Table(
            "ref_version_log", m,
            Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
            Column("table_name", Unicode(64), nullable=False, index=True),
            Column("version", Integer, nullable=False),
            # create / full_replace / partial / rollback
            Column("operation", Unicode(20), nullable=False),
            Column("parent_version", Integer),     # 退一版時要回到的版本
            Column("rollback_to", Integer),        # operation=rollback 時，還原自哪一版
            Column("rows_inserted", Integer, nullable=False, default=0),
            Column("rows_updated", Integer, nullable=False, default=0),
            Column("rows_deleted", Integer, nullable=False, default=0),
            Column("row_count", Integer, nullable=False, default=0),
            Column("columns_added", UnicodeText),
            Column("changed_at", DateTime, nullable=False),
            Column("changed_by", Unicode(128), nullable=False),
            Column("changed_by_name", Unicode(200)),
            Column("session_id", Unicode(64)),
            Column("summary", UnicodeText),
            Column("source_note", UnicodeText),    # 原文的申請人/單號/原因等
        )
        self.sessions = Table(
            "ref_change_session", m,
            Column("session_id", Unicode(64), primary_key=True),
            Column("user_id", Unicode(128), nullable=False, index=True),
            Column("user_name", Unicode(200)),
            # drafting / awaiting_confirm / applied / cancelled / expired / failed
            Column("status", Unicode(20), nullable=False),
            Column("kind", Unicode(20), nullable=False, default="change"),  # change / rollback
            Column("revision", Integer, nullable=False, default=0),
            Column("table_name", Unicode(64)),
            Column("base_version", Integer),
            Column("proposal_json", UnicodeText),
            Column("messages_json", UnicodeText),   # Claude conversation (append-only)
            Column("applied_version", Integer),
            Column("created_at", DateTime, nullable=False),
            Column("updated_at", DateTime, nullable=False),
        )
        self.inbound = Table(
            "ref_inbound_message", m,
            Column("msg_key", Unicode(128), primary_key=True),  # 訊息 ID 去重
            Column("user_id", Unicode(128), nullable=False),
            Column("msg_type", Unicode(32), nullable=False),
            Column("content", UnicodeText),
            Column("received_at", DateTime, nullable=False),
        )


def ensure_schemas(engine: Engine, schemas: list[str]) -> None:
    with engine.begin() as conn:
        existing = set(inspect(conn).get_schema_names())
        for s in schemas:
            if s in existing:
                continue
            conn.execute(CreateSchema(s))


def init_db(engine: Engine, settings: Settings) -> MetaTables:
    ensure_schemas(engine, [settings.schema_ref, settings.schema_hist, settings.schema_meta])
    meta = MetaTables(settings.schema_meta)
    meta.metadata.create_all(engine)
    return meta
