"""Parse callback XML into a channel-neutral dict."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass
class ChatMessage:
    to_user: str
    from_user: str
    create_time: str
    msg_type: str
    agent_id: str | None = None
    content: str | None = None
    msg_id: str | None = None
    pic_url: str | None = None
    media_id: str | None = None
    file_name: str | None = None
    event: str | None = None
    event_key: str | None = None
    task_id: str | None = None
    response_code: str | None = None

    @property
    def dedupe_key(self) -> str:
        if self.msg_id:
            return f"msg:{self.msg_id}"
        # 事件沒有 MsgId：用 使用者+時間+事件 去重 (平台逾時會重送)
        return f"evt:{self.from_user}:{self.create_time}:{self.event}:{self.event_key or ''}"


def _t(root: ET.Element, tag: str) -> str | None:
    el = root.find(tag)
    return el.text if el is not None and el.text is not None else None


def extract_encrypt(post_xml: str) -> str:
    root = _safe_parse(post_xml)
    enc = _t(root, "Encrypt")
    if not enc:
        raise ValueError("缺少 Encrypt 節點")
    return enc


def _safe_parse(xml: str) -> ET.Element:
    if "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise ValueError("不接受含 DTD/ENTITY 的 XML")
    return ET.fromstring(xml)


def parse_message(xml: str) -> ChatMessage:
    r = _safe_parse(xml)
    return ChatMessage(
        to_user=_t(r, "ToUserName") or "",
        from_user=_t(r, "FromUserName") or "",
        create_time=_t(r, "CreateTime") or "",
        msg_type=(_t(r, "MsgType") or "").lower(),
        agent_id=_t(r, "AgentID"),
        content=_t(r, "Content"),
        msg_id=_t(r, "MsgId"),
        pic_url=_t(r, "PicUrl"),
        media_id=_t(r, "MediaId"),
        file_name=_t(r, "FileName") or _t(r, "Title"),
        event=_t(r, "Event"),
        event_key=_t(r, "EventKey"),
        task_id=_t(r, "TaskId"),
        response_code=_t(r, "ResponseCode"),
    )
