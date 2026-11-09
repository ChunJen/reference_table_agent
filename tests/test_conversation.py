import csv
import json

from app.agent import ReferenceTableAgent
from app.conversation import ConversationService, Inbound
from app.main import CollectingOutbox
from app.models import Proposal

from .fakes import PNG_BYTES, TEXT_SAMPLE, ScriptedLLM, text, tool

MG_ROWS = [
    {"group_code": "G01", "group_name": "測試品項C"},
    {"group_code": "G02", "group_name": "成品-甲款-48"},
    {"group_code": "G02-1", "group_name": "成品-甲款-28"},
    {"group_code": "G03", "group_name": "成品-乙款-48"},
    {"group_code": "G03-1", "group_name": "成品-乙款-28"},
]


def mg_proposal(code_len=20, extra_cols=()):
    return {
        "action": "create_table", "table_name": "material_group", "display_name": "物料群組",
        "description": "價格折扣用物料群組",
        "columns": [{"name": "group_code", "display_name": "物料群組代碼", "type": "text", "length": code_len},
                    {"name": "group_name", "display_name": "物料群組名稱", "type": "text", "length": 100},
                    *extra_cols],
        "key_columns": ["group_code"], "mode": "full_replace", "rows": MG_ROWS,
        "summary": "新增物料群組參照表並加入 5 個群組",
        "source_note": "2026.11.04 20:49 王小明：因應新產品上市調整成本價說明",
        "assumptions": ["以物料群組代碼為主鍵"],
    }


def make(repo, settings, steps):
    llm = ScriptedLLM(steps)
    agent = ReferenceTableAgent(repo, settings, llm)
    return ConversationService(repo, agent, repo.meta, settings), llm


def of(out, typ):
    return [m for m in out.items if m["type"] == typ]


def test_text_sample_create_multiround_confirm(repo, settings):
    conv, llm = make(repo, settings, [
        tool("list_tables", {}),
        lambda p: (_assert_no_tables(p), tool("submit_proposal", mg_proposal()))[1],
        text("目前沒有物料群組表，建議如上建立，請確認。"),
        # 第二輪：使用者要求修改
        lambda p: (_assert_revision_context(p), tool("submit_proposal", mg_proposal(
            code_len=10, extra_cols=[{"name": "remark", "display_name": "備註", "type": "text"}])))[1],
        text("已把代碼長度改為 10，並新增備註欄。"),
    ])
    out = CollectingOutbox()
    conv.handle(Inbound(user_id="zhangsan", user_name="張三", text=TEXT_SAMPLE, msg_key="m1"), out)
    first_msg = llm.calls[0]["messages"][0]["content"][-1]["text"]
    assert "傳送者：張三(zhangsan)" in first_msg and "G03-1" in first_msg
    assert llm.calls[0]["model"] == "claude-opus-5-5" and llm.calls[0]["thinking"] == {"type": "adaptive"}
    preview = of(out, "markdown")[0]["content"]
    assert "★物料群組代碼" in preview and "新增 5" in preview and "王小明" in preview
    card1 = of(out, "card")[0]
    sid = card1["buttons"][0]["key"].split("|")[1]
    assert card1["buttons"][0]["key"] == f"confirm|{sid}|1"

    # 重送同一則（平台重試）→ 去重
    out_dup = CollectingOutbox()
    conv.handle(Inbound(user_id="zhangsan", text=TEXT_SAMPLE, msg_key="m1"), out_dup)
    assert out_dup.items == []

    out2 = CollectingOutbox()
    conv.handle(Inbound(user_id="zhangsan", user_name="張三", text="代碼長度改 10，另外加一個備註欄", msg_key="m2"), out2)
    card2 = of(out2, "card")[0]
    assert card2["buttons"][0]["key"] == f"confirm|{sid}|2"
    assert "備註" in of(out2, "markdown")[0]["content"]

    # 按舊卡片的確認 → 拒絕
    out3 = CollectingOutbox()
    conv.handle(Inbound(user_id="zhangsan", event_key=f"confirm|{sid}|1", msg_key="e1"), out3)
    assert "舊的預覽" in out3.items[0]["content"]
    assert repo.get_table("material_group") is None

    # 別人不能按我的卡片
    out4 = CollectingOutbox()
    conv.handle(Inbound(user_id="other", event_key=f"confirm|{sid}|2", msg_key="e2"), out4)
    assert "沒有待確認" in out4.items[0]["content"]

    out5 = CollectingOutbox()
    conv.handle(Inbound(user_id="zhangsan", user_name="張三", event_key=f"confirm|{sid}|2",
                        response_code="rc1", msg_key="e3"), out5)
    assert of(out5, "card_done")[0]["label"] == "已確認"
    assert "已建立 material_group → v1" in of(out5, "text")[0]["content"]
    path = of(out5, "file")[0]["path"]
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    assert rows[0][:3] == ["物料群組代碼", "物料群組名稱", "備註"]
    assert len(rows) == 6 and all(r[-2] == "zhangsan" for r in rows[1:])
    info = repo.get_table("material_group")
    assert [c.length for c in info.columns if c.name == "group_code"] == [10]
    h = repo.history("material_group")[0]
    assert h["changed_by_name"] == "張三" and "王小明" in h["source_note"]

    # 已套用的卡片再按一次
    out6 = CollectingOutbox()
    conv.handle(Inbound(user_id="zhangsan", event_key=f"confirm|{sid}|2", msg_key="e4"), out6)
    assert "已套用" in out6.items[0]["content"]


def _assert_no_tables(params):
    res = params["messages"][-1]["content"][0]
    assert res["type"] == "tool_result" and "沒有任何參照表" in res["content"]


def _assert_revision_context(params):
    msgs = params["messages"]
    # 多輪：完整歷史 (第一輪 user/assistant/tool_result...) + 新的修改訊息，且只追加不改寫
    assert len(msgs) == 7 and msgs[-1]["role"] == "user"
    assert "待確認的提案" in msgs[-1]["content"][-1]["text"]


def test_image_sample_rename_by_value_confirm_and_rollback(repo, settings):
    repo.apply(Proposal(
        action="create_table", table_name="project_subgroup", display_name="專案區小群組",
        columns=[{"name": "subgroup_code", "display_name": "小群組代碼", "length": 10},
                 {"name": "subgroup_desc", "display_name": "小群組說明", "length": 100}],
        key_columns=["subgroup_code"],
        rows=[{"subgroup_code": f"Z0{i}", "subgroup_desc": d} for i, d in enumerate(
            ["區域-北區專案一", "區域-北區專案二", "區域-北區專案三", "區域-中區專案一",
             "區域-中區專案二", "區域-南區專案一", "區域-南區專案二"], 1)]),
        "admin", None, None, None)

    new = ["區域-Z1區", "區域-Z2區", "區域-Z3區", "區域-Z4區", "區域-Z5區", "區域-Z6區", "區域-Z7區"]

    def check_image(params):
        blocks = params["messages"][0]["content"]
        assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/png"
        return tool("find_rows", {"table_name": "project_subgroup", "keyword": "北區專案一"})

    def check_found(params):
        found = json.loads(params["messages"][-1]["content"][0]["content"])
        assert [m["subgroup_code"] for m in found["matches"]] == ["Z01"]
        return tool("submit_proposal", {
            "action": "update_table", "table_name": "project_subgroup", "mode": "partial",
            "rows": [{"subgroup_code": f"Z0{i}", "subgroup_desc": d} for i, d in enumerate(new, 1)],
            "summary": "專案區小群組說明改名（7 筆）",
            "source_note": "系統權限申請單：專案區修改小群組說明",
            "assumptions": ["圖片下方可能被裁切，請確認是否只有這 7 筆"]})

    conv, llm = make(repo, settings, [check_image, check_found, text("已依舊說明找到對應代碼。")])
    out = CollectingOutbox()
    conv.handle(Inbound(user_id="lisi", images=[(PNG_BYTES, "application/octet-stream")], msg_key="i1"), out)
    md = of(out, "markdown")[0]["content"]
    assert "修改 7" in md and "區域-北區專案一 → 區域-Z1區" in md and "裁切" in md

    out2 = CollectingOutbox()
    conv.handle(Inbound(user_id="lisi", text="確認", msg_key="c1"), out2)
    assert "已更新 project_subgroup → v2" in of(out2, "text")[0]["content"]
    _, rows, _ = repo.fetch_rows("project_subgroup")
    assert sorted(r["subgroup_desc"] for r in rows) == new
    assert {r["sys_updated_by"] for r in rows} == {"lisi"}

    # 緊急退版（指令，不經 LLM）
    out3 = CollectingOutbox()
    conv.handle(Inbound(user_id="lisi", text="退版 project_subgroup", msg_key="r1"), out3)
    assert "退版預覽" in of(out3, "markdown")[0]["content"]
    key = of(out3, "card")[0]["buttons"][0]["key"]
    out4 = CollectingOutbox()
    conv.handle(Inbound(user_id="lisi", event_key=key, msg_key="r2"), out4)
    assert "已退版 project_subgroup → v3" in of(out4, "text")[0]["content"]
    _, rows, _ = repo.fetch_rows("project_subgroup")
    assert "區域-北區專案一" in {r["subgroup_desc"] for r in rows}
    assert len(of(out4, "file")) == 1
    assert len(llm.steps) == 0


def test_agent_question_then_yes_goes_to_agent_and_cancel(repo, settings):
    conv, llm = make(repo, settings, [
        tool("list_tables", {}),
        text("沒有找到相關參照表。這些是「客戶等級」嗎？要幫您建立新表嗎？"),
        lambda p: (_assert_yes_forwarded(p), text("好的，請提供完整的等級清單。"))[1],
    ])
    out = CollectingOutbox()
    conv.handle(Inbound(user_id="u", text="A級 VIP、B級 一般", msg_key="q1"), out)
    assert "要幫您建立" in out.items[0]["content"]
    out2 = CollectingOutbox()
    conv.handle(Inbound(user_id="u", text="是", msg_key="q2"), out2)
    assert "完整的等級清單" in out2.items[0]["content"]
    out3 = CollectingOutbox()
    conv.handle(Inbound(user_id="u", text="取消", msg_key="q3"), out3)
    assert "已取消" in out3.items[0]["content"]


def _assert_yes_forwarded(params):
    assert params["messages"][-1]["content"][-1]["text"].endswith("是")


def test_invalid_proposal_returns_error_to_model(repo, settings):
    bad = mg_proposal()
    bad["key_columns"] = ["nope"]
    conv, llm = make(repo, settings, [
        tool("submit_proposal", bad),
        lambda p: (_assert_error(p), tool("submit_proposal", mg_proposal()))[1],
        text("已修正。"),
    ])
    out = CollectingOutbox()
    conv.handle(Inbound(user_id="u", text=TEXT_SAMPLE), out)
    assert of(out, "card")


def _assert_error(params):
    res = params["messages"][-1]["content"][0]
    assert res["is_error"] is True and "key_columns" in res["content"]
