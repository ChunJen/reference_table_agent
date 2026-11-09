"""Messaging platform server API: access_token、發送訊息/卡片/檔案、下載媒體、更新卡片."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

MAX_TEXT_BYTES = 2000   # text/markdown 上限 2048 bytes，留餘裕


def clip_utf8(s: str, limit: int = MAX_TEXT_BYTES) -> str:
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    suffix = "\n…（內容過長已截斷）"
    return b[: limit - len(suffix.encode())].decode("utf-8", "ignore") + suffix


class ChatAPIError(Exception):
    pass


class ChatClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.s = settings
        self.http = http or httpx.Client(base_url=settings.im_api_base, timeout=20.0)
        self._token: str | None = None
        self._token_exp = 0.0
        self._lock = threading.Lock()
        self._names: dict[str, str] = {}

    # ---------------------------------------------------------------- token
    def token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or time.time() > self._token_exp:
                r = self.http.get("/cgi-bin/gettoken",
                                  params={"corpid": self.s.im_corp_id, "corpsecret": self.s.im_secret})
                data = r.json()
                if data.get("errcode", 0) != 0:
                    raise ChatAPIError(f"gettoken: {data}")
                self._token = data["access_token"]
                self._token_exp = time.time() + int(data.get("expires_in", 7200)) - 300
            return self._token

    def _call(self, method: str, path: str, **kw: Any) -> dict:
        for attempt in (0, 1):
            params = dict(kw.pop("params", {}) or {})
            params["access_token"] = self.token(force=attempt == 1)
            r = self.http.request(method, path, params=params, **kw)
            data = r.json()
            if data.get("errcode", 0) in (40014, 42001, 40001) and attempt == 0:
                continue   # token 失效，重取一次
            if data.get("errcode", 0) != 0:
                raise ChatAPIError(f"{path}: {data}")
            return data
        raise ChatAPIError(f"{path}: token 重試失敗")

    # ---------------------------------------------------------------- send
    def _send(self, user_id: str, msgtype: str, body: dict) -> dict:
        payload = {"touser": user_id, "msgtype": msgtype, "agentid": self.s.im_agent_id, msgtype: body}
        return self._call("POST", "/cgi-bin/message/send", json=payload)

    def send_text(self, user_id: str, content: str) -> None:
        self._send(user_id, "text", {"content": clip_utf8(content)})

    def send_markdown(self, user_id: str, content: str) -> None:
        self._send(user_id, "markdown", {"content": clip_utf8(content)})

    def send_button_card(self, user_id: str, task_id: str, title: str, desc: str,
                         fields: list[tuple[str, str]], buttons: list[dict]) -> None:
        card = {
            "card_type": "button_interaction",
            "main_title": {"title": title[:36], "desc": desc[:44]},
            "horizontal_content_list": [{"keyname": k[:5], "value": v[:30]} for k, v in fields[:6]],
            "task_id": task_id,
            "button_list": buttons[:6],
        }
        self._send(user_id, "template_card", card)

    def update_card_button(self, user_id: str, response_code: str, replace_name: str) -> None:
        payload = {"userids": [user_id], "agentid": self.s.im_agent_id,
                   "response_code": response_code, "button": {"replace_name": replace_name}}
        try:
            self._call("POST", "/cgi-bin/message/update_template_card", json=payload)
        except Exception as e:   # 卡片更新失敗不影響主流程
            log.warning("update_template_card failed: %s", e)

    def send_file(self, user_id: str, path: Path) -> None:
        with path.open("rb") as f:
            data = self._call("POST", "/cgi-bin/media/upload", params={"type": "file"},
                              files={"media": (path.name, f, "text/csv")})
        self._send(user_id, "file", {"media_id": data["media_id"]})

    # ---------------------------------------------------------------- receive
    def download_media(self, media_id: str) -> tuple[bytes, str]:
        r = self.http.get("/cgi-bin/media/get", params={"access_token": self.token(), "media_id": media_id})
        ctype = r.headers.get("content-type", "")
        if "json" in ctype or "text/plain" in ctype:
            raise ChatAPIError(f"media/get: {r.text[:200]}")
        return r.content, ctype.split(";")[0]

    def user_name(self, user_id: str) -> str | None:
        if user_id in self._names:
            return self._names[user_id]
        try:
            data = self._call("GET", "/cgi-bin/user/get", params={"userid": user_id})
            name = data.get("name") or None
        except Exception:
            name = None   # 新版應用可能沒有通訊錄讀取權限
        if name:
            self._names[user_id] = name
        return name


class ChatOutbox:
    """Adapter: ConversationService -> messaging platform."""

    def __init__(self, client: ChatClient):
        self.c = client

    def text(self, user_id: str, content: str) -> None:
        self.c.send_text(user_id, content)

    def markdown(self, user_id: str, content: str) -> None:
        self.c.send_markdown(user_id, content)

    def card(self, user_id: str, task_id: str, title: str, desc: str,
             fields: list[tuple[str, str]], buttons: list[dict]) -> None:
        self.c.send_button_card(user_id, task_id, title, desc, fields, buttons)

    def file(self, user_id: str, path: Path) -> None:
        self.c.send_file(user_id, path)

    def card_done(self, user_id: str, response_code: str | None, label: str) -> None:
        if response_code:
            self.c.update_card_button(user_id, response_code, label)
