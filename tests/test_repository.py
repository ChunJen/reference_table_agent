import csv
from decimal import Decimal

import pytest

from app.models import Proposal
from app.repository import RepoError, StaleVersionError


def material_group_create():
    return Proposal(
        action="create_table", table_name="material_group", display_name="物料群組",
        description="價格折扣用物料群組",
        columns=[
            {"name": "group_code", "display_name": "物料群組代碼", "type": "text", "length": 20},
            {"name": "group_name", "display_name": "物料群組名稱", "type": "text", "length": 100},
            {"name": "discount_rate", "display_name": "折扣率", "type": "decimal", "precision": 9, "scale": 4},
        ],
        key_columns=["group_code"],
        rows=[
            {"group_code": "A01", "group_name": "測試品項A", "discount_rate": "0.95"},
            {"group_code": "A02", "group_name": "測試品項B", "discount_rate": 0.9},
        ],
        summary="建立物料群組表",
    )


def test_create_and_partial_add_from_text_sample(repo):
    p = material_group_create()
    plan = repo.plan(p)
    assert plan.base_version is None and len(plan.inserts) == 2
    r1 = repo.apply(p, "wang.xm", "王小明", "s1", None)
    assert (r1.version, r1.inserted, r1.row_count) == (1, 2, 2)

    # 使用者文字範本：新增 5 個物料群組（部分更新）
    add = Proposal(
        action="update_table", table_name="material_group", mode="partial",
        rows=[
            {"group_code": "G01", "group_name": "測試品項C"},
            {"group_code": "G02", "group_name": "成品-甲款-48"},
            {"group_code": "G02-1", "group_name": "成品-甲款-28"},
            {"group_code": "G03", "group_name": "成品-乙款-48"},
            {"group_code": "G03-1", "group_name": "成品-乙款-28"},
        ],
        summary="新增5個物料群組",
        source_note="2026.11.04 20:49 王小明：因應新產品上市調整成本價說明",
    )
    plan = repo.plan(add)
    assert len(plan.inserts) == 5 and not plan.updates and not plan.deletes
    r2 = repo.apply(add, "u_sender", "傳送者", "s2", 1)
    assert (r2.version, r2.row_count) == (2, 7)

    _, rows, total = repo.fetch_rows("material_group", limit=100)
    by = {r["group_code"]: r for r in rows}
    assert total == 7
    assert by["G02-1"]["sys_updated_by"] == "u_sender" and by["G02-1"]["sys_version"] == 2
    assert by["A01"]["sys_updated_by"] == "wang.xm" and by["A01"]["sys_version"] == 1  # 未變動不重壓
    assert by["A01"]["discount_rate"] == Decimal("0.9500")
    hist = repo.history("material_group")
    assert hist[0]["source_note"].startswith("2026.11.04") and hist[0]["parent_version"] == 1


def test_rename_by_value_then_rollback_chain(repo):
    # 圖片範本：「區域-北區專案一 改 區域-Z1區」— 依舊值找到代碼後只更新說明欄
    create = Proposal(
        action="create_table", table_name="project_subgroup", display_name="專案區小群組",
        columns=[{"name": "subgroup_code", "display_name": "小群組代碼", "length": 10},
                 {"name": "subgroup_desc", "display_name": "小群組說明", "length": 100}],
        key_columns=["subgroup_code"],
        rows=[{"subgroup_code": "P01", "subgroup_desc": "區域-北區專案一"},
              {"subgroup_code": "P02", "subgroup_desc": "區域-北區專案二"},
              {"subgroup_code": "P09", "subgroup_desc": "其他"}])
    repo.apply(create, "a", None, None, None)
    _, found, _ = repo.fetch_rows("project_subgroup", keyword="北區專案一")
    assert [r["subgroup_code"] for r in found] == ["P01"]

    upd = Proposal(action="update_table", table_name="project_subgroup", mode="partial",
                   rows=[{"subgroup_code": "p01", "subgroup_desc": "區域-Z1區"},   # 大小寫不同也能對到
                         {"subgroup_code": "P02", "subgroup_desc": "區域-Z2區"}],
                   delete_keys=[{"subgroup_code": "P09"}])
    plan = repo.plan(upd)
    assert len(plan.updates) == 2 and len(plan.deletes) == 1 and not plan.inserts
    assert plan.updates[0][0] == {"subgroup_code": "P01"}
    repo.apply(upd, "b", None, None, 1)                           # v2

    # 緊急退一版 → 回到 v1 的內容
    rb = repo.plan_rollback("project_subgroup")
    assert rb.rollback_to == 1 and len(rb.inserts) == 1 and len(rb.updates) == 2
    r = repo.apply_rollback("project_subgroup", None, "c", None, None, expected_current=2)
    assert r.version == 3 and r.operation == "rollback"
    _, rows, _ = repo.fetch_rows("project_subgroup")
    assert {x["subgroup_desc"] for x in rows} == {"區域-北區專案一", "區域-北區專案二", "其他"}
    assert {x["sys_updated_by"] for x in rows} == {"c"}          # 退版也壓上時間/操作者

    # 再退一版：v3(=v1 內容) 的上一版應不存在 (v1 為初始版)
    with pytest.raises(RepoError, match="初始版本|沒有上一版"):
        repo.plan_rollback("project_subgroup")
    # 但可指定退到 v2
    assert repo.apply_rollback("project_subgroup", 2, "c", None, None).version == 4


def test_full_replace_add_column_and_csv(repo, settings):
    repo.apply(material_group_create(), "a", None, None, None)
    full = Proposal(action="update_table", table_name="material_group", mode="full_replace",
                    columns=[{"name": "effective_date", "display_name": "生效日", "type": "date"}],
                    rows=[{"group_code": "A01", "group_name": "測試品項A", "discount_rate": "0.95",
                           "effective_date": "2026/11/04"},
                          {"group_code": "CX3", "group_name": "新的", "discount_rate": "1"}])
    plan = repo.plan(full)
    assert [c.name for c in plan.new_columns] == ["effective_date"]
    assert len(plan.inserts) == 1 and len(plan.deletes) == 1 and len(plan.updates) == 1
    repo.apply(full, "b", None, None, 1)
    path = repo.export_csv("material_group")
    with path.open(encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["物料群組代碼", "物料群組名稱", "折扣率", "生效日", "資料更新時間", "更新者", "版本"]
    assert rows[1][:4] == ["A01", "測試品項A", "0.95", "2026-11-04"] and rows[1][-1] == "2"
    # 退版後新增欄位保留但為空
    repo.apply_rollback("material_group", None, "c", None, None)
    _, rows2, _ = repo.fetch_rows("material_group")
    assert {r["group_code"] for r in rows2} == {"A01", "A02"}
    assert all(r["effective_date"] is None for r in rows2)


def test_validation_and_stale_version(repo):
    repo.apply(material_group_create(), "a", None, None, None)
    bad = Proposal(action="update_table", table_name="material_group",
                   rows=[{"group_code": "CX9", "discount_rate": "abc"}, {"group_code": "CX9", "foo": 1}])
    with pytest.raises(RepoError) as e:
        repo.plan(bad)
    assert "需為數字" in str(e.value) and "未知欄位" in str(e.value)
    ok = Proposal(action="update_table", table_name="material_group",
                  rows=[{"group_code": "A01", "group_name": "改名"}])
    with pytest.raises(StaleVersionError):
        repo.apply(ok, "a", None, None, expected_base_version=0)
    same = Proposal(action="update_table", table_name="material_group",
                    rows=[{"group_code": "A01", "group_name": "測試品項A"}])
    with pytest.raises(RepoError, match="沒有需要更新"):
        repo.apply(same, "a", None, None, 1)
