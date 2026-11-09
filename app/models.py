"""Proposal / column specs + value coercion. Claude only ever produces these;
the deterministic repository is what touches the database."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import BigInteger, Boolean, Date, DateTime, Numeric, Unicode, UnicodeText

from .db import HIST_VERSION, SYSTEM_COLUMNS

IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
RESERVED = set(SYSTEM_COLUMNS) | {HIST_VERSION}

ColumnType = Literal["text", "long_text", "integer", "decimal", "date", "datetime", "boolean"]


class ColumnSpec(BaseModel):
    name: str
    display_name: str
    type: ColumnType = "text"
    length: int | None = None          # text only (default 255)
    precision: int | None = None       # decimal only (default 18)
    scale: int | None = None           # decimal only (default 4)
    nullable: bool = True
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _ident(cls, v: str) -> str:
        v = v.strip().lower()
        if not IDENT_RE.match(v):
            raise ValueError(f"欄位名稱 '{v}' 不合法：需小寫英文開頭，只含 a-z0-9_，最長 48 字元")
        if v in RESERVED:
            raise ValueError(f"欄位名稱 '{v}' 為系統保留欄位")
        return v

    def sa_type(self):
        if self.type == "text":
            return Unicode(self.length or 255)
        if self.type == "long_text":
            return UnicodeText()
        if self.type == "integer":
            return BigInteger()
        if self.type == "decimal":
            return Numeric(self.precision or 18, self.scale if self.scale is not None else 4)
        if self.type == "date":
            return Date()
        if self.type == "datetime":
            return DateTime()
        if self.type == "boolean":
            return Boolean()
        raise ValueError(self.type)

    def coerce(self, value: Any) -> Any:
        """Coerce a JSON value from Claude into the python value for this column."""
        if value is None or (isinstance(value, str) and value.strip() == ""):
            if not self.nullable:
                raise ValueError(f"欄位 {self.display_name}({self.name}) 不可為空")
            return None
        t = self.type
        if t in ("text", "long_text"):
            s = str(value).strip()
            if t == "text" and len(s) > (self.length or 255):
                raise ValueError(f"欄位 {self.name} 長度 {len(s)} 超過 {self.length or 255}")
            return s
        if t == "integer":
            if isinstance(value, bool):
                raise ValueError(f"欄位 {self.name} 需為整數：{value!r}")
            d = _to_decimal(value, self.name)
            if d != d.to_integral_value():
                raise ValueError(f"欄位 {self.name} 需為整數：{value!r}")
            return int(d)
        if t == "decimal":
            d = _to_decimal(value, self.name)
            scale = self.scale if self.scale is not None else 4
            return d.quantize(Decimal(1).scaleb(-scale)) if scale >= 0 else d
        if t == "date":
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            return _parse_date(str(value), self.name)
        if t == "datetime":
            if isinstance(value, datetime):
                return value
            return _parse_datetime(str(value), self.name)
        if t == "boolean":
            if isinstance(value, bool):
                return value
            s = str(value).strip().lower()
            if s in {"1", "true", "t", "y", "yes", "是", "啟用", "有效", "v"}:
                return True
            if s in {"0", "false", "f", "n", "no", "否", "停用", "無效", "x"}:
                return False
            raise ValueError(f"欄位 {self.name} 需為是/否：{value!r}")
        raise ValueError(t)


def _to_decimal(value: Any, name: str) -> Decimal:
    s = str(value).strip().replace(",", "").replace("，", "")
    try:
        return Decimal(s)
    except InvalidOperation as e:
        raise ValueError(f"欄位 {name} 需為數字：{value!r}") from e


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d", "%Y年%m月%d日")


def _parse_date(s: str, name: str) -> date:
    s = s.strip()
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    raise ValueError(f"欄位 {name} 日期格式無法辨識：{s!r}")


def _parse_datetime(s: str, name: str) -> datetime:
    s = s.strip().replace("T", " ")
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    return datetime.combine(_parse_date(s, name), datetime.min.time())


class Proposal(BaseModel):
    """A complete change plan. Revisions always replace the whole draft."""

    action: Literal["create_table", "update_table"]
    table_name: str
    display_name: str | None = None
    description: str | None = None
    # create_table: full column list. update_table: only NEW columns to add (may be empty).
    columns: list[ColumnSpec] = Field(default_factory=list)
    key_columns: list[str] = Field(default_factory=list)   # create_table only
    # full_replace = rows 是整張表的完整內容；partial = rows 為新增/修改，delete_keys 為刪除
    mode: Literal["full_replace", "partial"] = "partial"
    rows: list[dict[str, Any]] = Field(default_factory=list)
    delete_keys: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""            # 給使用者看的一句話摘要
    source_note: str | None = None   # 原文中的申請人、單號、原因
    assumptions: list[str] = Field(default_factory=list)  # Agent 的推斷/待確認事項

    @field_validator("table_name")
    @classmethod
    def _tname(cls, v: str) -> str:
        v = v.strip().lower()
        if not IDENT_RE.match(v):
            raise ValueError(f"表名 '{v}' 不合法：需小寫英文開頭，只含 a-z0-9_，最長 48 字元")
        return v

    @model_validator(mode="after")
    def _check(self) -> "Proposal":
        names = [c.name for c in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("欄位名稱重複")
        if self.action == "create_table":
            if not self.columns:
                raise ValueError("create_table 需要 columns")
            if not self.display_name:
                raise ValueError("create_table 需要 display_name（中文表名）")
            if not self.key_columns:
                raise ValueError("create_table 需要 key_columns（業務主鍵）")
            missing = [k for k in self.key_columns if k not in names]
            if missing:
                raise ValueError(f"key_columns {missing} 不在 columns 中")
            if self.mode != "full_replace":
                self.mode = "full_replace"   # 新表的初始資料即完整內容
            if self.delete_keys:
                raise ValueError("create_table 不可有 delete_keys")
        if self.mode == "full_replace" and self.delete_keys:
            raise ValueError("full_replace 不需要 delete_keys（未列出的資料列即會刪除）")
        return self
