"""Claude agent: understands the user's message (structured text / free text / image /
file), looks up existing reference tables, and submits a validated change proposal.

Claude never writes to the database. It can only read (list/get/find) and submit a
proposal; the user's confirmation triggers the deterministic RefRepository.apply().
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from .config import Settings
from .models import Proposal
from .repository import Plan, RefRepository, RepoError, fmt_value

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是公司的「參照表維護助理」，透過通訊軟體接收同仁的訊息，維護資料庫中的參照表（對照表/主檔，例如物料群組、區域對照、客戶分類）。這些參照表會被 BI 報表與 ETL 流程直接引用，所以資料必須正確、結構穩定。

你能做的事：用工具查詢現有參照表，然後用 submit_proposal 送出一份「變更提案」。你不能直接寫入資料庫：提案會以預覽卡片給使用者，使用者按「確認更新」後系統才會寫入、記錄版本並回傳最新 CSV。所以絕不要說「已更新」。

處理流程：
1. 先呼叫 list_tables，依「意義」判斷訊息對應哪張現有參照表（例如「物料群組」對應 material_group）。有疑似相關的表就用 get_table 看欄位與現有資料。
2. 已有對應表 → action=update_table。
   - 預設 mode=partial：rows 只放要新增或修改的資料列，且一定要含主鍵欄位；partial 時只需給要改的欄位，未給的欄位維持原值。要刪除的列放 delete_keys（只放主鍵）。
   - 只有當使用者明確表示這是完整清單（「整批」「全部以此為準」「完整版」「覆蓋」）才用 mode=full_replace，此時 rows 要是整張表的完整內容，沒列到的資料會被刪除。
   - 需要新欄位時放在 columns（只放新增的欄位）。不支援改主鍵、刪欄位、改欄位型別；遇到這類需求請說明並建議做法。
   - 「A 改 B」「舊值→新值」這類只給名稱沒給代碼的修改，用 find_rows 以舊值查出主鍵再提案；查不到或查到多筆就問使用者，不要猜。
3. 沒有對應表 → action=create_table，主動推薦一版建表方式：
   - table_name 與欄位 name 用小寫英文 snake_case（如 material_group、group_code）；display_name 用中文。
   - 選定業務主鍵 key_columns（通常是代碼欄位）；型別在 text/long_text/integer/decimal/date/datetime/boolean 中選；代碼欄位用 text 並給足長度。
   - 不要自己加更新時間/更新者欄位，系統會自動加 sys_updated_at、sys_updated_by、sys_version。
   - 推論出來、使用者沒明說的部分寫進 assumptions，讓使用者一眼能確認或修改。
4. 圖片：逐字仔細辨識，不要腦補。看不清楚的字要寫進 assumptions。若圖片看起來被裁切（右側或下方被截斷、清單可能沒拍完），要在 assumptions 提醒使用者確認是否還有更多資料。
5. 原文裡的申請人、日期、單號、原因（如「因應…故新增…」「系統權限申請單」）摘要到 source_note。注意原文中的人名不一定是傳訊者；系統會另外記錄訊息的傳送者。
6. summary 用一句話描述這次變更。

多輪修改：使用者看過預覽後可能繼續補充或修改（改欄位名、加一筆、改型別、換成整批覆蓋…）。每次都重新送出「完整」的提案（不是只送差異），系統會以最新一版為準。

submit_proposal 回傳驗證錯誤時，修正後再送一次。驗證通過後只需回一兩句話（例如重點或需要使用者確認的事項），不要重複列出資料，系統會自動產生預覽卡片。

如果訊息跟參照表無關、或資訊不足以提出任何提案，就直接用文字回覆或提問，不要呼叫 submit_proposal。
若使用者要求「退版」「回到上一版」，呼叫 request_rollback。
一律使用繁體中文回覆，語氣簡潔。"""


COLUMN_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "小寫英文 snake_case 欄位名"},
        "display_name": {"type": "string", "description": "中文顯示名"},
        "type": {"type": "string", "enum": ["text", "long_text", "integer", "decimal", "date", "datetime", "boolean"]},
        "length": {"type": "integer", "description": "text 長度，預設 255"},
        "precision": {"type": "integer"},
        "scale": {"type": "integer"},
        "nullable": {"type": "boolean"},
        "description": {"type": "string"},
    },
    "required": ["name", "display_name", "type"],
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_tables",
        "description": "列出所有由本助理管理的參照表（表名、中文名、說明、欄位、主鍵、目前版本、筆數）。處理任何資料訊息前都要先呼叫。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_table",
        "description": "取得一張參照表的完整欄位定義與現有資料（最多 limit 筆）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string"},
                "limit": {"type": "integer", "description": "回傳資料筆數上限，預設 50，最大 500"},
            },
            "required": ["table_name"],
        },
    },
    {
        "name": "find_rows",
        "description": "在參照表中以關鍵字（部分比對、不分大小寫）搜尋資料列，用來由舊名稱/說明反查主鍵。",
        "input_schema": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string"},
                "keyword": {"type": "string"},
                "column": {"type": "string", "description": "只搜尋此欄位（可省略=全部欄位）"},
            },
            "required": ["table_name", "keyword"],
        },
    },
    {
        "name": "submit_proposal",
        "description": "送出完整的變更提案（建表或更新）。系統會驗證並計算與現有資料的差異；通過後產生預覽給使用者確認。每次修改都要送完整提案。",
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create_table", "update_table"]},
                "table_name": {"type": "string"},
                "display_name": {"type": "string", "description": "中文表名（create_table 必填）"},
                "description": {"type": "string", "description": "這張表的用途"},
                "columns": {"type": "array", "items": COLUMN_SCHEMA,
                            "description": "create_table: 全部欄位；update_table: 只放新增欄位"},
                "key_columns": {"type": "array", "items": {"type": "string"}, "description": "create_table 的業務主鍵"},
                "mode": {"type": "string", "enum": ["partial", "full_replace"]},
                "rows": {"type": "array", "items": {"type": "object"},
                         "description": "資料列，key 為欄位 name，值用字串或數字"},
                "delete_keys": {"type": "array", "items": {"type": "object"},
                                "description": "partial 模式要刪除的列，只放主鍵欄位"},
                "summary": {"type": "string"},
                "source_note": {"type": "string"},
                "assumptions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["action", "table_name", "mode", "rows", "summary"],
        },
    },
    {
        "name": "request_rollback",
        "description": "使用者要把某張參照表退回上一版（或指定版本）時呼叫。系統會產生退版預覽讓使用者確認。",
        "input_schema": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string"},
                "target_version": {"type": "integer", "description": "可省略=上一版"},
            },
            "required": ["table_name"],
        },
    },
]


class LLM(Protocol):
    def create(self, **params: Any) -> Any: ...


class AnthropicLLM:
    """Real client: streaming + get_final_message (long tool inputs, large max_tokens)."""

    def __init__(self, settings: Settings, client: Any | None = None):
        import anthropic

        self.client = client or anthropic.Anthropic()
        self.s = settings

    def create(self, **params: Any) -> Any:
        extra: dict[str, Any] = {}
        if self.s.claude_fallbacks:
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        with self.client.beta.messages.stream(**params, **extra) as stream:
            return stream.get_final_message()


@dataclass
class AgentTurn:
    reply_text: str
    proposal: Proposal | None = None
    plan: Plan | None = None
    rollback: dict | None = None
    messages: list[dict] = field(default_factory=list)   # full updated history
    error: str | None = None


def _dump_block(b: Any) -> dict:
    return b.model_dump(mode="json", exclude_none=True) if hasattr(b, "model_dump") else dict(b)


class ReferenceTableAgent:
    def __init__(self, repo: RefRepository, settings: Settings, llm: LLM):
        self.repo = repo
        self.s = settings
        self.llm = llm

    # -------------------------------------------------------------- tools
    def _tool(self, name: str, args: dict, turn: AgentTurn) -> tuple[str, bool]:
        try:
            if name == "list_tables":
                out = []
                for t in self.repo.list_tables():
                    out.append({
                        "table_name": t.table_name, "display_name": t.display_name,
                        "description": t.description, "key_columns": t.key_columns,
                        "columns": [f"{c.name}({c.display_name},{c.type})" for c in t.columns],
                        "current_version": t.current_version, "row_count": self.repo.row_count(t),
                    })
                return json.dumps(out or "目前沒有任何參照表", ensure_ascii=False), False
            if name == "get_table":
                limit = max(1, min(int(args.get("limit") or 50), 500))
                info, rows, total = self.repo.fetch_rows(args["table_name"], limit=limit)
                return json.dumps({
                    "table_name": info.table_name, "display_name": info.display_name,
                    "description": info.description, "key_columns": info.key_columns,
                    "columns": [c.model_dump(exclude_none=True) for c in info.columns],
                    "current_version": info.current_version, "total_rows": total,
                    "rows": [_jsonable(r, info) for r in rows],
                }, ensure_ascii=False), False
            if name == "find_rows":
                info, rows, total = self.repo.fetch_rows(
                    args["table_name"], limit=50, keyword=args["keyword"], column=args.get("column"))
                return json.dumps({"matches": [_jsonable(r, info) for r in rows],
                                   "match_count": len(rows), "total_rows": total}, ensure_ascii=False), False
            if name == "submit_proposal":
                p = Proposal.model_validate(args)
                plan = self.repo.plan(p)
                if not plan.has_changes:
                    return "提案與目前資料完全相同，沒有需要變更的內容。請向使用者說明。", True
                turn.proposal, turn.plan, turn.rollback = p, plan, None
                return "驗證通過，系統將顯示預覽給使用者確認：\n" + plan_summary_text(plan), False
            if name == "request_rollback":
                plan = self.repo.plan_rollback(args["table_name"], args.get("target_version"))
                turn.rollback = {"table_name": plan.table_name, "target_version": plan.rollback_to,
                                 "base_version": plan.base_version}
                turn.plan, turn.proposal = plan, None
                return f"已產生退版預覽（v{plan.base_version} → 還原 v{plan.rollback_to}），等待使用者確認。", False
            return f"未知工具 {name}", True
        except ValidationError as e:
            msgs = [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:20]]
            return "提案格式錯誤：\n" + "\n".join(msgs), True
        except RepoError as e:
            return str(e), True
        except (KeyError, TypeError, ValueError) as e:
            return f"參數錯誤：{e}", True

    # -------------------------------------------------------------- loop
    def run(self, history: list[dict], user_content: list[dict]) -> AgentTurn:
        messages = list(history) + [{"role": "user", "content": user_content}]
        turn = AgentTurn(reply_text="", messages=messages)
        for _ in range(self.s.claude_max_tool_rounds):
            resp = self.llm.create(
                model=self.s.anthropic_model,
                max_tokens=self.s.claude_max_tokens,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": self.s.claude_effort},
                cache_control={"type": "ephemeral"},
            )
            if resp.stop_reason == "refusal":
                turn.error = "模型拒絕處理此訊息，請調整內容後再試。"
                turn.reply_text = turn.error
                turn.messages = list(history)   # 不把被拒絕的訊息留在對話中
                return turn
            messages.append({"role": "assistant", "content": [_dump_block(b) for b in resp.content]})
            if resp.stop_reason == "max_tokens":
                turn.error = "內容太長，模型輸出被截斷。請把資料分批傳送，或改傳 Excel/CSV 檔。"
                turn.reply_text = turn.error
                turn.proposal = turn.plan = turn.rollback = None
                # 保持對話可接續：補上 tool_result
                pend = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
                if pend:
                    messages.append({"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                         "content": "輸出被截斷，未執行"} for b in pend]})
                return turn
            if resp.stop_reason == "pause_turn":
                continue
            tool_uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if not tool_uses:
                turn.reply_text = "\n".join(
                    b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
                return turn
            results = []
            for b in tool_uses:
                out, is_err = self._tool(b.name, b.input if isinstance(b.input, dict) else {}, turn)
                log.info("tool %s -> err=%s", b.name, is_err)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out,
                                **({"is_error": True} if is_err else {})})
            messages.append({"role": "user", "content": results})
        turn.reply_text = turn.reply_text or "處理步驟過多，請把需求說得更具體一些再試一次。"
        return turn


def _jsonable(row: dict, info) -> dict:
    return {c.name: fmt_value(row.get(c.name)) for c in info.columns} | {
        "sys_updated_at": fmt_value(row.get("sys_updated_at")),
        "sys_updated_by": row.get("sys_updated_by")}


def plan_summary_text(plan: Plan) -> str:
    parts = [f"表：{plan.display_name}（{plan.table_name}）",
             f"新增 {len(plan.inserts)} 筆、修改 {len(plan.updates)} 筆、刪除 {len(plan.deletes)} 筆、"
             f"未變動 {plan.unchanged} 筆；套用後共 {plan.row_count_after} 筆"]
    if plan.new_columns:
        parts.append("新增欄位：" + "、".join(f"{c.display_name}({c.name})" for c in plan.new_columns))
    parts += [f"注意：{w}" for w in plan.warnings]
    return "\n".join(parts)
