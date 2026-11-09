"""Conversation orchestrator (channel-neutral).

Session state machine (one active session per user):
  drafting ──(proposal ok)──> awaiting_confirm ──(確認)──> applied  → 回傳最新 CSV
     ^                            │  (使用者繼續修改) → 新 revision，重新預覽
     └────────────────────────────┘
  任何狀態 ──(取消)──> cancelled ；逾時 ──> expired
  退版：建立 kind=rollback 的 session，同樣需按確認。
"""
from __future__ import annotations

import base64
import csv
import io
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from .agent import ReferenceTableAgent, plan_summary_text
from .config import Settings
from .db import MetaTables, now_local
from .models import Proposal
from .repository import Plan, RefRepository, RepoError, StaleVersionError, fmt_value

log = logging.getLogger(__name__)

ACTIVE = ("drafting", "awaiting_confirm")
CONFIRM_WORDS = {"確認", "确认", "是", "確定", "确定", "yes", "y", "ok", "confirm", "確認更新", "确认更新", "好", "同意"}
CANCEL_WORDS = {"取消", "cancel", "否", "不要", "放棄", "放弃", "算了"}
HELP_WORDS = {"help", "說明", "说明", "幫助", "帮助", "?", "？", "指令"}
LIST_WORDS = {"清單", "清单", "列表", "list", "參照表", "参照表", "所有表"}
ROLLBACK_RE = re.compile(r"^(?:緊急)?(?:退版|退回|回滾|回滚|rollback|紧急退版)\s*([a-z][a-z0-9_]*)?\s*(?:v?(\d+))?\s*$", re.I)
EXPORT_RE = re.compile(r"^(?:下載|下载|匯出|导出|csv|export)\s*([a-z][a-z0-9_]*)\s*$", re.I)
HISTORY_RE = re.compile(r"^(?:歷史|历史|版本|history)\s*([a-z][a-z0-9_]*)\s*$", re.I)

HELP_TEXT = """參照表助理使用方式：
• 直接貼上文字（有無格式皆可）、截圖或 Excel/CSV 檔，我會判斷要更新哪張參照表；沒有的話會推薦建表方式。
• 看到預覽後：按「確認更新」或回覆「確認」寫入；直接回覆修改內容可多輪調整；回覆「取消」放棄。
• 退版 <表名> [版本]：緊急退回上一版（或指定版本）
• 下載 <表名>：取得最新 CSV
• 歷史 <表名>：查看版本紀錄
• 清單：列出所有參照表"""


@dataclass
class Inbound:
    user_id: str
    user_name: str | None = None
    text: str | None = None
    images: list[tuple[bytes, str]] = field(default_factory=list)   # (bytes, media_type)
    files: list[tuple[str, bytes]] = field(default_factory=list)    # (file_name, bytes)
    event_key: str | None = None        # 卡片按鈕
    response_code: str | None = None    # 用於更新卡片狀態
    msg_key: str | None = None          # 去重


class Outbox(Protocol):
    def text(self, user_id: str, content: str) -> None: ...
    def markdown(self, user_id: str, content: str) -> None: ...
    def card(self, user_id: str, task_id: str, title: str, desc: str,
             fields: list[tuple[str, str]], buttons: list[dict]) -> None: ...
    def file(self, user_id: str, path: Path) -> None: ...
    def card_done(self, user_id: str, response_code: str | None, label: str) -> None: ...


class ConversationService:
    def __init__(self, repo: RefRepository, agent: ReferenceTableAgent, meta: MetaTables, settings: Settings):
        self.repo = repo
        self.agent = agent
        self.meta = meta
        self.s = settings
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ================================================================ entry
    def handle(self, msg: Inbound, out: Outbox) -> None:
        with self._user_lock(msg.user_id):
            try:
                if msg.msg_key and not self._record_inbound(msg):
                    log.info("duplicate message %s ignored", msg.msg_key)
                    return
                allowed = self.s.allowed_user_set
                if allowed and msg.user_id not in allowed:
                    out.text(msg.user_id, "您沒有參照表維護權限，請洽系統管理員。")
                    return
                self._dispatch(msg, out)
            except Exception as e:  # 不讓例外吞掉回覆
                log.exception("handle failed")
                out.text(msg.user_id, f"處理時發生錯誤：{e}")

    def _user_lock(self, uid: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(uid, threading.Lock())

    def _record_inbound(self, msg: Inbound) -> bool:
        content = msg.text or msg.event_key or (f"[image x{len(msg.images)}]" if msg.images else None) \
            or (f"[file {msg.files[0][0]}]" if msg.files else None)
        typ = "event" if msg.event_key else "image" if msg.images else "file" if msg.files else "text"
        try:
            with self.repo.engine.begin() as conn:
                conn.execute(insert(self.meta.inbound).values(
                    msg_key=msg.msg_key[:128], user_id=msg.user_id, msg_type=typ,
                    content=content, received_at=now_local(self.s)))
            return True
        except IntegrityError:
            return False

    def _dispatch(self, msg: Inbound, out: Outbox) -> None:
        uid = msg.user_id
        if msg.event_key:
            return self._on_button(msg, out)
        t = (msg.text or "").strip()
        if t and not msg.images and not msg.files:
            low = t.lower()
            if low in HELP_WORDS:
                return out.text(uid, HELP_TEXT)
            if low in LIST_WORDS:
                return self._list(uid, out)
            if low in CONFIRM_WORDS:
                sess = self._active_session(uid)
                if sess is not None and sess.status == "drafting":
                    return self._agent_turn(msg, out)   # 回答 Agent 的提問，例如「要建立嗎？」→「是」
                return self._confirm(uid, msg.user_name, None, None, out)
            if low in CANCEL_WORDS:
                return self._cancel(uid, None, out)
            if m := ROLLBACK_RE.match(t):
                return self._start_rollback(uid, msg.user_name, m.group(1), m.group(2), out)
            if m := EXPORT_RE.match(t):
                return self._export(uid, m.group(1), out)
            if m := HISTORY_RE.match(t):
                return self._history(uid, m.group(1), out)
        if not t and not msg.images and not msg.files:
            return out.text(uid, "目前支援文字、圖片與 Excel/CSV 檔。輸入「說明」查看用法。")
        self._agent_turn(msg, out)

    # ================================================================ sessions
    def _active_session(self, uid: str):
        ss = self.meta.sessions
        with self.repo.engine.connect() as conn:
            row = conn.execute(select(ss).where(ss.c.user_id == uid, ss.c.status.in_(ACTIVE))
                               .order_by(ss.c.updated_at.desc())).first()
        if row and now_local(self.s) - row.updated_at > timedelta(hours=self.s.session_ttl_hours):
            self._set_status(row.session_id, "expired")
            return None
        return row

    def _get_session(self, sid: str):
        ss = self.meta.sessions
        with self.repo.engine.connect() as conn:
            return conn.execute(select(ss).where(ss.c.session_id == sid)).first()

    def _set_status(self, sid: str, status: str, **extra) -> None:
        ss = self.meta.sessions
        with self.repo.engine.begin() as conn:
            conn.execute(update(ss).where(ss.c.session_id == sid)
                         .values(status=status, updated_at=now_local(self.s), **extra))

    def _new_session(self, uid: str, name: str | None, kind: str = "change") -> str:
        prev = self._active_session(uid)
        if prev:
            self._set_status(prev.session_id, "cancelled")
        sid = uuid.uuid4().hex
        ts = now_local(self.s)
        with self.repo.engine.begin() as conn:
            conn.execute(insert(self.meta.sessions).values(
                session_id=sid, user_id=uid, user_name=name, status="drafting", kind=kind,
                revision=0, messages_json="[]", created_at=ts, updated_at=ts))
        return sid

    # ================================================================ agent
    def _agent_turn(self, msg: Inbound, out: Outbox) -> None:
        uid = msg.user_id
        sess = self._active_session(uid)
        if sess is None or sess.kind != "change":
            sid = self._new_session(uid, msg.user_name)
            history: list[dict] = []
            revision = 0
        else:
            sid, history, revision = sess.session_id, json.loads(sess.messages_json or "[]"), sess.revision

        content = self._user_content(msg, out, has_draft=bool(sess and sess.proposal_json))
        if content is None:
            return
        turn = self.agent.run(history, content)
        ss = self.meta.sessions
        values: dict = {"messages_json": json.dumps(turn.messages, ensure_ascii=False),
                        "updated_at": now_local(self.s)}

        if turn.rollback and turn.plan:
            # Claude 判斷為退版需求 → 轉成退版 session
            with self.repo.engine.begin() as conn:
                conn.execute(update(ss).where(ss.c.session_id == sid).values(status="cancelled", **values))
            return self._present_rollback(uid, msg.user_name, turn.plan, out, intro=turn.reply_text)

        if turn.proposal and turn.plan:
            revision += 1
            values.update(status="awaiting_confirm", revision=revision, table_name=turn.plan.table_name,
                          base_version=turn.plan.base_version,
                          proposal_json=turn.proposal.model_dump_json())
            with self.repo.engine.begin() as conn:
                conn.execute(update(ss).where(ss.c.session_id == sid).values(**values))
            if turn.reply_text:
                out.text(uid, turn.reply_text)
            self._present_change(uid, sid, revision, turn.proposal, turn.plan, out)
            return

        with self.repo.engine.begin() as conn:
            conn.execute(update(ss).where(ss.c.session_id == sid).values(**values))
        out.text(uid, turn.reply_text or "我沒有理解需求，可以再說明一下嗎？（輸入「說明」查看用法）")

    def _user_content(self, msg: Inbound, out: Outbox, has_draft: bool) -> list[dict] | None:
        blocks: list[dict] = []
        for data, media_type in msg.images:
            if len(data) > 5 * 1024 * 1024:
                out.text(msg.user_id, "圖片超過 5MB，請壓縮或截較小範圍後再傳。")
                return None
            mt = media_type if media_type in ("image/png", "image/jpeg", "image/gif", "image/webp") \
                else _sniff_image(data)
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": mt, "data": base64.standard_b64encode(data).decode()}})
        for name, data in msg.files:
            try:
                blocks.append({"type": "text", "text": f"【附件 {name} 內容】\n{file_to_text(name, data)}"})
            except ValueError as e:
                out.text(msg.user_id, str(e))
                return None
        ts = now_local(self.s).strftime("%Y-%m-%d %H:%M")
        who = f"{msg.user_name}({msg.user_id})" if msg.user_name else msg.user_id
        header = f"[傳送者：{who}，時間：{ts}]"
        if has_draft:
            header += "\n[目前有一份待確認的提案；若這則訊息是修改意見，請送出修改後的完整提案]"
        blocks.append({"type": "text", "text": f"{header}\n{msg.text or '（請辨識附件內容）'}"})
        return blocks

    # ================================================================ previews
    def _present_change(self, uid: str, sid: str, rev: int, p: Proposal, plan: Plan, out: Outbox) -> None:
        out.markdown(uid, render_preview(plan, p, self.s.preview_rows))
        verb = "建立" if plan.action == "create_table" else "更新"
        out.card(uid, task_id=f"{sid}-{rev}", title=f"確認{verb}：{plan.display_name}",
                 desc=(p.summary or "")[:44],
                 fields=[("參照表", plan.table_name),
                         ("異動", f"+{len(plan.inserts)} ~{len(plan.updates)} -{len(plan.deletes)}"),
                         ("版本", f"v{plan.base_version or 0}→v{(plan.base_version or 0) + 1}")],
                 buttons=[{"text": "確認更新", "style": 1, "key": f"confirm|{sid}|{rev}"},
                          {"text": "取消", "style": 2, "key": f"cancel|{sid}|{rev}"}])

    def _present_rollback(self, uid: str, name: str | None, plan: Plan, out: Outbox, intro: str = "") -> None:
        sid = self._new_session(uid, name, kind="rollback")
        payload = {"table_name": plan.table_name, "target_version": plan.rollback_to}
        with self.repo.engine.begin() as conn:
            conn.execute(update(self.meta.sessions).where(self.meta.sessions.c.session_id == sid).values(
                status="awaiting_confirm", revision=1, table_name=plan.table_name,
                base_version=plan.base_version, proposal_json=json.dumps(payload)))
        if intro:
            out.text(uid, intro)
        out.markdown(uid, render_rollback_preview(plan, self.s.preview_rows))
        out.card(uid, task_id=f"{sid}-1", title=f"確認退版：{plan.display_name}",
                 desc=f"v{plan.base_version} 內容還原為 v{plan.rollback_to}",
                 fields=[("參照表", plan.table_name),
                         ("異動", f"+{len(plan.inserts)} ~{len(plan.updates)} -{len(plan.deletes)}")],
                 buttons=[{"text": "確認退版", "style": 1, "key": f"confirm|{sid}|1"},
                          {"text": "取消", "style": 2, "key": f"cancel|{sid}|1"}])

    # ================================================================ actions
    def _on_button(self, msg: Inbound, out: Outbox) -> None:
        parts = (msg.event_key or "").split("|")
        if len(parts) != 3 or parts[0] not in ("confirm", "cancel"):
            return out.text(msg.user_id, "無法辨識的按鈕。")
        action, sid, rev = parts[0], parts[1], int(parts[2]) if parts[2].isdigit() else -1
        if action == "confirm":
            self._confirm(msg.user_id, msg.user_name, sid, rev, out, msg.response_code)
        else:
            self._cancel(msg.user_id, sid, out, msg.response_code)

    def _confirm(self, uid: str, name: str | None, sid: str | None, rev: int | None, out: Outbox,
                 response_code: str | None = None) -> None:
        sess = self._get_session(sid) if sid else self._active_session(uid)
        if sess is None or sess.user_id != uid:
            return out.text(uid, "目前沒有待確認的變更。")
        if sess.status != "awaiting_confirm":
            state = {"applied": "已套用", "cancelled": "已取消", "expired": "已逾時",
                     "drafting": "尚未產生預覽", "failed": "已失敗"}.get(sess.status, sess.status)
            return out.text(uid, f"這份變更{state}，無法再確認。")
        if rev is not None and rev != sess.revision:
            return out.text(uid, f"這是舊的預覽（第 {rev} 版），請按最新一版（第 {sess.revision} 版）的確認按鈕。")
        if now_local(self.s) - sess.updated_at > timedelta(hours=self.s.session_ttl_hours):
            self._set_status(sess.session_id, "expired")
            return out.text(uid, "這份預覽已逾時，請重新傳送資料。")
        try:
            if sess.kind == "rollback":
                payload = json.loads(sess.proposal_json)
                res = self.repo.apply_rollback(payload["table_name"], payload["target_version"], uid, name,
                                               sess.session_id, expected_current=sess.base_version)
            else:
                p = Proposal.model_validate_json(sess.proposal_json)
                res = self.repo.apply(p, uid, name, sess.session_id, sess.base_version)
        except StaleVersionError as e:
            self._set_status(sess.session_id, "drafting")
            return out.text(uid, f"⚠️ {e}")
        except RepoError as e:
            self._set_status(sess.session_id, "failed")
            return out.text(uid, f"更新失敗，資料未變更：{e}")
        self._set_status(sess.session_id, "applied", applied_version=res.version)
        out.card_done(uid, response_code, "已確認")
        op = {"create": "已建立", "rollback": "已退版", "partial": "已更新", "full_replace": "已整批更新"}[res.operation]
        out.text(uid, f"✅ {op} {res.table_name} → v{res.version}\n"
                      f"新增 {res.inserted}、修改 {res.updated}、刪除 {res.deleted}，目前共 {res.row_count} 筆。\n"
                      f"如需緊急退回，回覆「退版 {res.table_name}」。")
        out.file(uid, self.repo.export_csv(res.table_name))

    def _cancel(self, uid: str, sid: str | None, out: Outbox, response_code: str | None = None) -> None:
        sess = self._get_session(sid) if sid else self._active_session(uid)
        if sess is None or sess.user_id != uid or sess.status not in ACTIVE:
            return out.text(uid, "目前沒有進行中的變更。")
        self._set_status(sess.session_id, "cancelled")
        out.card_done(uid, response_code, "已取消")
        out.text(uid, "已取消，資料未變更。")

    def _start_rollback(self, uid: str, name: str | None, table: str | None, ver: str | None,
                        out: Outbox) -> None:
        if not table:
            sess = self._active_session(uid)
            table = sess.table_name if sess and sess.table_name else None
        if not table:
            names = "、".join(t.table_name for t in self.repo.list_tables()) or "（無）"
            return out.text(uid, f"請指定要退版的表，例如「退版 material_group」。\n現有參照表：{names}")
        try:
            plan = self.repo.plan_rollback(table.lower(), int(ver) if ver else None)
        except RepoError as e:
            return out.text(uid, str(e))
        self._present_rollback(uid, name, plan, out)

    def _list(self, uid: str, out: Outbox) -> None:
        tables = self.repo.list_tables()
        if not tables:
            return out.text(uid, "目前沒有參照表。直接傳送資料，我會推薦建表方式。")
        lines = [f"• {t.display_name}（{t.table_name}）v{t.current_version}，{self.repo.row_count(t)} 筆"
                 for t in tables]
        out.text(uid, "參照表清單：\n" + "\n".join(lines))

    def _export(self, uid: str, table: str, out: Outbox) -> None:
        try:
            out.file(uid, self.repo.export_csv(table.lower()))
        except RepoError as e:
            out.text(uid, str(e))

    def _history(self, uid: str, table: str, out: Outbox) -> None:
        rows = self.repo.history(table.lower())
        if not rows:
            return out.text(uid, f"找不到 {table} 的版本紀錄。")
        op = {"create": "建立", "partial": "部分更新", "full_replace": "整批更新", "rollback": "退版"}
        lines = [f"v{r['version']} {r['changed_at']:%m/%d %H:%M} {r['changed_by_name'] or r['changed_by']} "
                 f"{op.get(r['operation'], r['operation'])} +{r['rows_inserted']}~{r['rows_updated']}"
                 f"-{r['rows_deleted']} {r['summary'] or ''}" for r in rows]
        out.text(uid, f"{table} 版本紀錄（新→舊）：\n" + "\n".join(lines))


# ==================================================================== rendering
def _fmt_row(row: dict, cols) -> str:
    return "｜".join(f"{c.display_name}:{fmt_value(row.get(c.name))}" for c in cols if row.get(c.name) is not None)


def render_preview(plan: Plan, p: Proposal, n: int) -> str:
    key_cols = [c for c in plan.columns if c.name in plan.key_columns]
    lines = [f"### 變更預覽：{plan.display_name}",
             f"> {p.summary}" if p.summary else "",
             f"表名：`{plan.table_name}`　模式：{'新建' if plan.action == 'create_table' else ('整批覆蓋' if p.mode == 'full_replace' else '部分更新')}"]
    if plan.action == "create_table":
        lines.append("**欄位**（★主鍵）：")
        for c in plan.columns:
            star = "★" if c.name in plan.key_columns else ""
            typ = c.type + (f"({c.length or 255})" if c.type == "text" else "")
            lines.append(f"- {star}{c.display_name} `{c.name}` {typ}")
        lines.append("（系統自動加上 sys_updated_at 更新時間、sys_updated_by 傳送者、sys_version 版本）")
    elif plan.new_columns:
        lines.append("**新增欄位**：" + "、".join(f"{c.display_name}`{c.name}`" for c in plan.new_columns))
    lines.append(f"<font color=\"info\">新增 {len(plan.inserts)}</font>　"
                 f"<font color=\"warning\">修改 {len(plan.updates)}</font>　"
                 f"<font color=\"comment\">刪除 {len(plan.deletes)}</font>　"
                 f"未變動 {plan.unchanged}　→ 共 {plan.row_count_after} 筆")
    if plan.inserts:
        lines.append("**新增**")
        lines += [f"+ {_fmt_row(r, plan.columns)}" for r in plan.inserts[:n]]
        if len(plan.inserts) > n:
            lines.append(f"…另 {len(plan.inserts) - n} 筆")
    if plan.updates:
        lines.append("**修改**")
        for key, changes, before in plan.updates[:n]:
            k = "/".join(str(v) for v in key.values())
            diff = "，".join(f"{_disp(plan, c)}：{fmt_value(before.get(c))} → {fmt_value(v)}" for c, v in changes.items())
            lines.append(f"~ {k}：{diff}")
        if len(plan.updates) > n:
            lines.append(f"…另 {len(plan.updates) - n} 筆")
    if plan.deletes:
        lines.append("**刪除**")
        lines += [f"- {_fmt_row(r, key_cols + [c for c in plan.columns if c not in key_cols][:1])}"
                  for r in plan.deletes[:n]]
        if len(plan.deletes) > n:
            lines.append(f"…另 {len(plan.deletes) - n} 筆")
    notes = list(p.assumptions) + plan.warnings
    if notes:
        lines.append("**請確認**")
        lines += [f"- {a}" for a in notes]
    if p.source_note:
        lines.append(f"來源：{p.source_note}")
    lines.append("回覆修改意見可繼續調整；按「確認更新」或回覆「確認」才會寫入。")
    return "\n".join(x for x in lines if x)


def render_rollback_preview(plan: Plan, n: int) -> str:
    lines = [f"### 退版預覽：{plan.display_name}",
             f"將 `{plan.table_name}` 由 v{plan.base_version} 還原為 v{plan.rollback_to} 的內容（會產生新版本 v{(plan.base_version or 0) + 1}）",
             plan_summary_text(plan)]
    for key, changes, before in plan.updates[:n]:
        k = "/".join(str(v) for v in key.values())
        lines.append(f"~ {k}：" + "，".join(f"{_disp(plan, c)}：{fmt_value(before.get(c))} → {fmt_value(v)}"
                                           for c, v in changes.items()))
    lines += [f"+ 還原 {_fmt_row(r, plan.columns)}" for r in plan.inserts[:n]]
    lines += [f"- 移除 {'/'.join(str(r[k]) for k in plan.key_columns)}" for r in plan.deletes[:n]]
    return "\n".join(lines)


def _disp(plan: Plan, col: str) -> str:
    c = next((c for c in plan.columns if c.name == col), None)
    return c.display_name if c else col


def _sniff_image(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def file_to_text(name: str, data: bytes, max_rows: int = 5000) -> str:
    """CSV / TXT / XLSX → CSV-ish text for Claude."""
    low = name.lower()
    if low.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        buf = io.StringIO()
        w = csv.writer(buf)
        for ws in wb.worksheets:
            buf.write(f"# 工作表：{ws.title}\n")
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= max_rows:
                    raise ValueError(f"檔案超過 {max_rows} 列，請分批上傳")
                if any(v is not None for v in row):
                    w.writerow(["" if v is None else fmt_value(v) for v in row])
        return buf.getvalue()
    if low.endswith((".csv", ".txt", ".tsv")):
        for enc in ("utf-8-sig", "gb18030", "big5"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("無法辨識檔案編碼，請存成 UTF-8")
        if text.count("\n") > max_rows:
            raise ValueError(f"檔案超過 {max_rows} 列，請分批上傳")
        return text
    raise ValueError(f"不支援的檔案格式：{name}（支援 xlsx / csv / txt）")
