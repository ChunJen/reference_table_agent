"""Deterministic data layer: plan (diff/validate), apply, rollback, export.

All writes happen in ONE transaction:
  DDL (create / add column) -> DML diff (stamped with time + sender + version)
  -> full snapshot into ref_hist -> registry + version log.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column, DateTime, Integer, MetaData, PrimaryKeyConstraint, Table, Unicode, and_,
    bindparam, delete, func, insert, inspect, literal, select, text, update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import SetColumnComment

from .config import Settings
from .db import (
    HIST_VERSION, SYS_UPDATED_AT, SYS_UPDATED_BY, SYS_VERSION, MetaTables, now_local,
)
from .models import ColumnSpec, Proposal


class RepoError(Exception):
    """Validation / business error; message is shown to the user."""


class StaleVersionError(RepoError):
    pass


@dataclass
class TableInfo:
    table_name: str
    display_name: str
    description: str | None
    key_columns: list[str]
    columns: list[ColumnSpec]
    current_version: int
    updated_at: Any = None
    updated_by: str | None = None

    def col(self, name: str) -> ColumnSpec | None:
        return next((c for c in self.columns if c.name == name), None)


@dataclass
class Plan:
    action: str                       # create_table / update_table / rollback
    table_name: str
    display_name: str
    base_version: int | None          # None = 新表
    key_columns: list[str]
    columns: list[ColumnSpec]         # 套用後的完整欄位
    new_columns: list[ColumnSpec]
    inserts: list[dict] = field(default_factory=list)
    updates: list[tuple[dict, dict, dict]] = field(default_factory=list)  # (key, changes, before)
    deletes: list[dict] = field(default_factory=list)                     # full current rows
    unchanged: int = 0
    warnings: list[str] = field(default_factory=list)
    row_count_after: int = 0
    rollback_to: int | None = None

    @property
    def has_changes(self) -> bool:
        return bool(self.inserts or self.updates or self.deletes or self.new_columns
                    or self.action == "create_table")


@dataclass
class ApplyResult:
    table_name: str
    version: int
    inserted: int
    updated: int
    deleted: int
    row_count: int
    operation: str


def _norm(v: Any) -> Any:
    if isinstance(v, Decimal):
        return v.normalize()
    if isinstance(v, str):
        return v.strip()
    return v


def _key_norm(v: Any) -> Any:
    # 鍵值比對不分大小寫/前後空白：與常見不分大小寫的 collation 一致，換資料庫時行為不變
    if isinstance(v, str):
        return v.strip().casefold()
    return _norm(v)


class RefRepository:
    def __init__(self, engine: Engine, settings: Settings, meta: MetaTables):
        self.engine = engine
        self.s = settings
        self.meta = meta

    # ------------------------------------------------------------------ catalog
    def _row_to_info(self, r) -> TableInfo:
        return TableInfo(
            table_name=r.table_name,
            display_name=r.display_name,
            description=r.description,
            key_columns=json.loads(r.key_columns),
            columns=[ColumnSpec(**c) for c in json.loads(r.columns_json)],
            current_version=r.current_version,
            updated_at=r.updated_at,
            updated_by=r.updated_by,
        )

    def list_tables(self) -> list[TableInfo]:
        reg = self.meta.registry
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(reg).where(reg.c.status == "active").order_by(reg.c.table_name)
            ).all()
        return [self._row_to_info(r) for r in rows]

    def get_table(self, name: str, conn: Connection | None = None, lock: bool = False) -> TableInfo | None:
        reg = self.meta.registry
        q = select(reg).where(reg.c.table_name == name.strip().lower(), reg.c.status == "active")
        if lock:
            q = q.with_for_update()
        if conn is None:
            with self.engine.connect() as c:
                r = c.execute(q).first()
        else:
            r = conn.execute(q).first()
        return self._row_to_info(r) if r else None

    def row_count(self, info: TableInfo) -> int:
        t = self._sa_table(info.table_name, info.columns, info.key_columns)
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(t)).scalar_one()

    # ------------------------------------------------------------------ sa tables
    def _sa_table(self, name: str, columns: list[ColumnSpec], keys: list[str],
                  hist: bool = False, md: MetaData | None = None) -> Table:
        md = md or MetaData()
        cols = []
        if hist:
            cols.append(Column(HIST_VERSION, Integer, nullable=False))
        for c in columns:
            cols.append(Column(c.name, c.sa_type(), nullable=(c.nullable and c.name not in keys),
                               comment=c.display_name))
        cols += [
            Column(SYS_UPDATED_AT, DateTime, nullable=False, comment="資料更新時間"),
            Column(SYS_UPDATED_BY, Unicode(128), nullable=False, comment="資料更新者"),
            Column(SYS_VERSION, Integer, nullable=False, comment="資料版本"),
        ]
        pk = ([HIST_VERSION] if hist else []) + list(keys)
        return Table(name, md, *cols, PrimaryKeyConstraint(*pk, name=f"pk_{name}{'_h' if hist else ''}"),
                     schema=self.s.schema_hist if hist else self.s.schema_ref)

    def _load_rows(self, conn: Connection, info: TableInfo) -> list[dict]:
        t = self._sa_table(info.table_name, info.columns, info.key_columns)
        order = [t.c[k] for k in info.key_columns]
        return [dict(r._mapping) for r in conn.execute(select(t).order_by(*order))]

    def fetch_rows(self, name: str, limit: int = 50, keyword: str | None = None,
                   column: str | None = None) -> tuple[TableInfo, list[dict], int]:
        info = self.get_table(name)
        if not info:
            raise RepoError(f"找不到參照表 {name}")
        with self.engine.connect() as conn:
            rows = self._load_rows(conn, info)
        total = len(rows)
        if keyword:
            kw = keyword.strip().casefold()
            cols = [column] if column else [c.name for c in info.columns]
            rows = [r for r in rows if any(kw in str(r.get(c) or "").casefold() for c in cols)]
        return info, rows[:limit], total

    # ------------------------------------------------------------------ planning
    def plan(self, p: Proposal, conn: Connection | None = None) -> Plan:
        if conn is None:
            with self.engine.connect() as c:
                return self._plan(p, c)
        return self._plan(p, conn)

    def _plan(self, p: Proposal, conn: Connection) -> Plan:
        info = self.get_table(p.table_name, conn)
        if len(p.rows) > self.s.max_rows_per_change:
            raise RepoError(f"單次最多 {self.s.max_rows_per_change} 筆，本次 {len(p.rows)} 筆")

        if p.action == "create_table":
            if info:
                raise RepoError(f"參照表 {p.table_name}（{info.display_name}）已存在，請改用更新")
            if inspect(conn).has_table(p.table_name, schema=self.s.schema_ref):
                raise RepoError(f"資料庫已存在同名資料表 {self.s.schema_ref}.{p.table_name}（非本 Agent 管理）")
            columns = [c.model_copy(update={"nullable": c.nullable and c.name not in p.key_columns})
                       for c in p.columns]
            plan = Plan("create_table", p.table_name, p.display_name or p.table_name, None,
                        list(p.key_columns), columns, [])
            current: list[dict] = []
        else:
            if not info:
                raise RepoError(f"找不到參照表 {p.table_name}，若是新表請用 create_table")
            existing_names = {c.name for c in info.columns}
            dup = [c.name for c in p.columns if c.name in existing_names]
            if dup:
                raise RepoError(f"欄位 {dup} 已存在；update_table 的 columns 只放要新增的欄位")
            if p.key_columns and list(p.key_columns) != info.key_columns:
                raise RepoError(f"不支援變更主鍵（目前主鍵 {info.key_columns}）")
            new_cols = [c.model_copy(update={"nullable": True}) for c in p.columns]
            plan = Plan("update_table", info.table_name, info.display_name, info.current_version,
                        info.key_columns, info.columns + new_cols, new_cols)
            current = self._load_rows(conn, info)

        self._diff(plan, p.mode, p.rows, p.delete_keys, current)
        return plan

    def _diff(self, plan: Plan, mode: str, rows: list[dict], delete_keys: list[dict],
              current: list[dict]) -> None:
        cols = {c.name: c for c in plan.columns}
        keys = plan.key_columns
        errors: list[str] = []

        def key_of(d: dict) -> tuple:
            return tuple(_key_norm(d.get(k)) for k in keys)

        cur_by_key = {key_of(r): r for r in current}
        seen: set[tuple] = set()
        incoming: dict[tuple, dict] = {}
        for i, raw in enumerate(rows, 1):
            raw = {str(k).strip().lower(): v for k, v in raw.items()}
            unknown = [k for k in raw if k not in cols]
            if unknown:
                errors.append(f"第 {i} 筆含未知欄位 {unknown}")
                continue
            missing_keys = [k for k in keys if raw.get(k) in (None, "")]
            if missing_keys:
                errors.append(f"第 {i} 筆缺少主鍵欄位 {missing_keys}")
                continue
            row = {}
            for k, v in raw.items():
                try:
                    row[k] = cols[k].coerce(v)
                except ValueError as e:
                    errors.append(f"第 {i} 筆：{e}")
            kk = key_of(row)
            if kk in seen:
                errors.append(f"第 {i} 筆主鍵重複：{[row.get(k) for k in keys]}")
                continue
            seen.add(kk)
            incoming[kk] = row
        if errors:
            raise RepoError("資料驗證失敗：\n- " + "\n- ".join(errors[:30]))

        for kk, row in incoming.items():
            before = cur_by_key.get(kk)
            if before is None:
                full = {c: row.get(c) for c in cols}
                nn = [c for c, spec in cols.items()
                      if full[c] is None and not (spec.nullable and c not in keys)]
                if nn:
                    errors.append(f"新增 {[row[k] for k in keys]} 缺少必填欄位 {nn}")
                plan.inserts.append(full)
                continue
            # full_replace: 未提供的欄位視為空值；partial: 只改有提供的欄位
            provided = cols.keys() if mode == "full_replace" else row.keys()
            changes = {c: row.get(c) for c in provided
                       if c not in keys and _norm(row.get(c)) != _norm(before.get(c))}
            if changes:
                key = {k: before[k] for k in keys}   # 用資料庫裡的實際鍵值
                plan.updates.append((key, changes, {c: before.get(c) for c in changes}))
            else:
                plan.unchanged += 1

        if mode == "full_replace":
            plan.deletes = [r for kk, r in cur_by_key.items() if kk not in incoming]
        else:
            for dk in delete_keys:
                dk = {str(k).strip().lower(): v for k, v in dk.items()}
                try:
                    kk = tuple(_key_norm(cols[k].coerce(dk.get(k))) for k in keys)
                except (ValueError, KeyError) as e:
                    errors.append(f"刪除條件 {dk} 無效：{e}")
                    continue
                if kk in incoming:
                    errors.append(f"{dk} 同時出現在新增/修改與刪除")
                elif kk in cur_by_key:
                    plan.deletes.append(cur_by_key[kk])
                else:
                    plan.warnings.append(f"要刪除的 {dk} 不存在，略過")
        if errors:
            raise RepoError("資料驗證失敗：\n- " + "\n- ".join(errors[:30]))
        plan.row_count_after = len(current) + len(plan.inserts) - len(plan.deletes)

    # ------------------------------------------------------------------ apply
    def apply(self, p: Proposal, user_id: str, user_name: str | None, session_id: str | None,
              expected_base_version: int | None) -> ApplyResult:
        with self.engine.begin() as conn:
            info = self.get_table(p.table_name, conn, lock=True)
            cur_ver = info.current_version if info else None
            if cur_ver != expected_base_version:
                raise StaleVersionError(
                    f"{p.table_name} 目前版本 v{cur_ver}，與預覽時的 v{expected_base_version} 不同"
                    "（期間有人更新過），請重新預覽後再確認")
            plan = self._plan(p, conn)
            if not plan.has_changes:
                raise RepoError("與目前資料相同，沒有需要更新的內容")
            op = "create" if plan.action == "create_table" else p.mode
            return self._write(conn, plan, op, user_id, user_name, session_id,
                               p.summary, p.source_note, p.description)

    def _write(self, conn: Connection, plan: Plan, op: str, user_id: str, user_name: str | None,
               session_id: str | None, summary: str | None, source_note: str | None,
               description: str | None = None) -> ApplyResult:
        ts = now_local(self.s)
        new_ver = (plan.base_version or 0) + 1
        stamp = {SYS_UPDATED_AT: ts, SYS_UPDATED_BY: user_id, SYS_VERSION: new_ver}
        t = self._sa_table(plan.table_name, plan.columns, plan.key_columns)
        h = self._sa_table(plan.table_name, plan.columns, plan.key_columns, hist=True)

        if plan.action == "create_table":
            t.create(conn)
            if not inspect(conn).has_table(plan.table_name, schema=self.s.schema_hist):
                h.create(conn)
        for c in plan.new_columns:
            self._add_column(conn, self.s.schema_ref, plan.table_name, c)
            self._add_column(conn, self.s.schema_hist, plan.table_name, c)

        keys = plan.key_columns
        if plan.deletes:
            stmt = delete(t).where(and_(*[t.c[k] == bindparam(f"_k_{k}") for k in keys]))
            conn.execute(stmt, [{f"_k_{k}": r[k] for k in keys} for r in plan.deletes])
        for key, changes, _before in plan.updates:
            stmt = (update(t).where(and_(*[t.c[k] == key[k] for k in keys]))
                    .values(**changes, **stamp))
            conn.execute(stmt)
        if plan.inserts:
            conn.execute(insert(t), [{**r, **stamp} for r in plan.inserts])

        # 完整快照：退版時以此還原
        all_cols = [c.name for c in plan.columns] + [SYS_UPDATED_AT, SYS_UPDATED_BY, SYS_VERSION]
        conn.execute(
            insert(h).from_select(
                [HIST_VERSION] + all_cols,
                select(literal(new_ver, Integer).label(HIST_VERSION), *[t.c[c] for c in all_cols]),
            )
        )
        row_count = conn.execute(select(func.count()).select_from(t)).scalar_one()

        # parent: 「退一版」要回到哪個版本
        vl = self.meta.version_log
        if plan.action == "rollback":
            parent = conn.execute(select(vl.c.parent_version).where(
                vl.c.table_name == plan.table_name, vl.c.version == plan.rollback_to)).scalar()
        else:
            parent = plan.base_version

        reg = self.meta.registry
        cols_json = json.dumps([c.model_dump(exclude_none=True) for c in plan.columns], ensure_ascii=False)
        if plan.action == "create_table":
            conn.execute(insert(reg).values(
                table_name=plan.table_name, display_name=plan.display_name, description=description,
                key_columns=json.dumps(keys), columns_json=cols_json, current_version=new_ver,
                status="active", created_at=ts, created_by=user_id, updated_at=ts, updated_by=user_id))
        else:
            vals = dict(current_version=new_ver, columns_json=cols_json, updated_at=ts, updated_by=user_id)
            if description:
                vals["description"] = description
            conn.execute(update(reg).where(reg.c.table_name == plan.table_name).values(**vals))

        conn.execute(insert(vl).values(
            table_name=plan.table_name, version=new_ver, operation=op, parent_version=parent,
            rollback_to=plan.rollback_to, rows_inserted=len(plan.inserts),
            rows_updated=len(plan.updates), rows_deleted=len(plan.deletes), row_count=row_count,
            columns_added=json.dumps([c.name for c in plan.new_columns]) if plan.new_columns else None,
            changed_at=ts, changed_by=user_id, changed_by_name=user_name, session_id=session_id,
            summary=summary, source_note=source_note))

        if self.s.create_display_views:
            self._refresh_display_view(conn, plan)
        return ApplyResult(plan.table_name, new_ver, len(plan.inserts), len(plan.updates),
                           len(plan.deletes), row_count, op)

    def _q(self, ident: str) -> str:
        return self.engine.dialect.identifier_preparer.quote(ident)

    def _add_column(self, conn: Connection, schema: str, table: str, c: ColumnSpec) -> None:
        typ = c.sa_type().compile(dialect=self.engine.dialect)
        tbl = f"{self._q(schema)}.{self._q(table)}"
        try:
            with conn.begin_nested():   # 標準語法 ADD COLUMN；部分資料庫只接受 ADD
                conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN {self._q(c.name)} {typ} NULL"))
        except DBAPIError:
            conn.execute(text(f"ALTER TABLE {tbl} ADD {self._q(c.name)} {typ} NULL"))
        self._comment(conn, schema, table, c.name, c.display_name, c.sa_type())

    def _comment(self, conn: Connection, schema: str, table: str, col: str, comment: str,
                 typ: Any = None) -> None:
        if not self.engine.dialect.supports_comments:
            return
        t = Table(table, MetaData(schema=schema), Column(col, typ if typ is not None else Unicode(), comment=comment))
        conn.execute(SetColumnComment(t.c[col]))

    def _refresh_display_view(self, conn: Connection, plan: Plan) -> None:
        sch, v = self._q(self.s.schema_ref), self._q(f"v_{plan.table_name}")
        sel = ", ".join(f"{self._q(c.name)} AS {self._q(c.display_name)}" for c in plan.columns)
        sel += (f", {self._q(SYS_UPDATED_AT)} AS {self._q('資料更新時間')}"
                f", {self._q(SYS_UPDATED_BY)} AS {self._q('更新者')}"
                f", {self._q(SYS_VERSION)} AS {self._q('版本')}")
        src = f"{sch}.{self._q(plan.table_name)}"
        conn.execute(text(f"DROP VIEW IF EXISTS {sch}.{v}"))
        conn.execute(text(f"CREATE VIEW {sch}.{v} AS SELECT {sel} FROM {src}"))

    # ------------------------------------------------------------------ rollback
    def plan_rollback(self, name: str, target_version: int | None = None,
                      conn: Connection | None = None) -> Plan:
        if conn is None:
            with self.engine.connect() as c:
                return self._plan_rollback(c, name, target_version)
        return self._plan_rollback(conn, name, target_version)

    def _plan_rollback(self, conn: Connection, name: str, target: int | None) -> Plan:
        info = self.get_table(name, conn)
        if not info:
            raise RepoError(f"找不到參照表 {name}")
        vl = self.meta.version_log
        if target is None:
            target = conn.execute(select(vl.c.parent_version).where(
                vl.c.table_name == info.table_name, vl.c.version == info.current_version)).scalar()
            if target is None:
                raise RepoError(f"{info.display_name} 目前為初始版本 v{info.current_version}，沒有上一版可退")
        if target >= info.current_version or target < 1:
            raise RepoError(f"可退回的版本為 v1 ~ v{info.current_version - 1}")
        h = self._sa_table(info.table_name, info.columns, info.key_columns, hist=True)
        snap = [dict(r._mapping) for r in conn.execute(select(h).where(h.c[HIST_VERSION] == target))]
        exists = conn.execute(select(vl.c.version).where(
            vl.c.table_name == info.table_name, vl.c.version == target)).scalar()
        if exists is None:
            raise RepoError(f"找不到 v{target} 的快照")
        rows = [{c.name: r.get(c.name) for c in info.columns} for r in snap]
        plan = Plan("rollback", info.table_name, info.display_name, info.current_version,
                    info.key_columns, info.columns, [], rollback_to=target)
        self._diff(plan, "full_replace", rows, [], self._load_rows(conn, info))
        return plan

    def apply_rollback(self, name: str, target_version: int | None, user_id: str,
                       user_name: str | None, session_id: str | None,
                       expected_current: int | None = None) -> ApplyResult:
        with self.engine.begin() as conn:
            info = self.get_table(name, conn, lock=True)
            if not info:
                raise RepoError(f"找不到參照表 {name}")
            if expected_current is not None and info.current_version != expected_current:
                raise StaleVersionError(
                    f"{name} 目前已是 v{info.current_version}（預覽時為 v{expected_current}），請重新發起退版")
            plan = self._plan_rollback(conn, name, target_version)
            return self._write(conn, plan, "rollback", user_id, user_name, session_id,
                               f"退版：還原為 v{plan.rollback_to} 的內容", None)

    # ------------------------------------------------------------------ history / export
    def history(self, name: str, limit: int = 10) -> list[dict]:
        vl = self.meta.version_log
        with self.engine.connect() as conn:
            rows = conn.execute(select(vl).where(vl.c.table_name == name.strip().lower())
                                .order_by(vl.c.version.desc()).limit(limit)).all()
        return [dict(r._mapping) for r in rows]

    def export_csv(self, name: str, out_dir: Path | None = None) -> Path:
        info = self.get_table(name)
        if not info:
            raise RepoError(f"找不到參照表 {name}")
        with self.engine.connect() as conn:
            rows = self._load_rows(conn, info)
        out_dir = Path(out_dir or self.s.export_dir) / info.table_name
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r'[\\/:*?"<>|\s]+', "_", info.display_name)
        path = out_dir / f"{safe}_{info.table_name}_v{info.current_version}.csv"
        names = [c.name for c in info.columns] + [SYS_UPDATED_AT, SYS_UPDATED_BY, SYS_VERSION]
        if self.s.csv_header == "display":
            header = [c.display_name for c in info.columns] + ["資料更新時間", "更新者", "版本"]
        else:
            header = names
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            for r in rows:
                w.writerow([fmt_value(r.get(n)) for n in names])
        return path


def fmt_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Y" if v else "N"
    if isinstance(v, Decimal):
        s = format(v.normalize(), "f")
        return s
    if hasattr(v, "isoformat"):
        return v.isoformat(sep=" ") if hasattr(v, "hour") else v.isoformat()
    return str(v)
