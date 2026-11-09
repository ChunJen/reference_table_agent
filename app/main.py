"""FastAPI entry: messaging callback + dev/admin endpoints.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from .agent import AnthropicLLM, ReferenceTableAgent
from .config import Settings, get_settings
from .conversation import ConversationService, Inbound
from .db import init_db, make_engine
from .repository import RefRepository, RepoError
from .messaging.client import ChatClient, ChatOutbox
from .messaging.crypto import ChatCryptoError, MsgCrypt
from .messaging.messages import parse_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ref_agent")


class ChatIn(BaseModel):
    user_id: str
    user_name: str | None = None
    text: str | None = None
    image_base64: str | None = None
    image_media_type: str = "image/png"
    event_key: str | None = None


class Container:
    def __init__(self, settings: Settings, llm=None, im: ChatClient | None = None):
        self.s = settings
        self.engine = make_engine(settings)
        self.meta = init_db(self.engine, settings)
        self.repo = RefRepository(self.engine, settings, self.meta)
        self.agent = ReferenceTableAgent(self.repo, settings, llm or AnthropicLLM(settings))
        self.conv = ConversationService(self.repo, self.agent, self.meta, settings)
        self.im = im or (ChatClient(settings) if settings.im_corp_id else None)
        self.crypto = (MsgCrypt(settings.im_token, settings.im_aes_key, settings.im_corp_id)
                       if settings.im_aes_key else None)
        # 平台要求在時限內回應：訊息丟到背景執行緒處理，再用主動推送回覆
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="im")


def create_app(container: Container | None = None) -> FastAPI:
    holder: dict[str, Container] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        holder["c"] = container or Container(get_settings())
        yield
        holder["c"].pool.shutdown(wait=False)

    app = FastAPI(title="Reference Table Agent", lifespan=lifespan)

    def c() -> Container:
        return holder["c"]

    @app.get("/health")
    def health():
        return {"ok": True}

    # ------------------------------------------------------------ messaging callback
    @app.get("/im/callback", response_class=PlainTextResponse)
    def im_verify(msg_signature: str, timestamp: str, nonce: str, echostr: str):
        if not c().crypto:
            raise HTTPException(503, "messaging not configured")
        try:
            return c().crypto.verify_url(msg_signature, timestamp, nonce, echostr)
        except ChatCryptoError as e:
            raise HTTPException(403, str(e))

    @app.post("/im/callback", response_class=PlainTextResponse)
    async def im_receive(request: Request, msg_signature: str, timestamp: str, nonce: str):
        cc = c()
        if not cc.crypto or not cc.im:
            raise HTTPException(503, "messaging not configured")
        body = (await request.body()).decode("utf-8")
        try:
            xml = cc.crypto.decrypt_message(msg_signature, timestamp, nonce, body)
        except (ChatCryptoError, ValueError) as e:
            raise HTTPException(403, str(e))
        msg = parse_message(xml)
        cc.pool.submit(_process_im, cc, msg)
        return "success"

    # ------------------------------------------------------------ dev / admin
    def _auth(token: str | None):
        s = c().s
        if not s.dev_api_enabled:
            raise HTTPException(404)
        if s.dev_api_token and token != s.dev_api_token:
            raise HTTPException(401)

    @app.post("/api/chat")
    def chat(body: ChatIn, x_api_token: str | None = Header(default=None)):
        """模擬對話（開發/測試用），回傳 Agent 送出的所有訊息。"""
        _auth(x_api_token)
        import base64

        out = CollectingOutbox()
        imgs = [(base64.b64decode(body.image_base64), body.image_media_type)] if body.image_base64 else []
        c().conv.handle(Inbound(user_id=body.user_id, user_name=body.user_name, text=body.text,
                                images=imgs, event_key=body.event_key), out)
        return {"messages": out.items}

    @app.get("/api/tables")
    def tables(x_api_token: str | None = Header(default=None)):
        _auth(x_api_token)
        return [{"table_name": t.table_name, "display_name": t.display_name,
                 "version": t.current_version, "key_columns": t.key_columns,
                 "columns": [x.model_dump(exclude_none=True) for x in t.columns]}
                for t in c().repo.list_tables()]

    @app.get("/api/tables/{name}/csv")
    def table_csv(name: str, x_api_token: str | None = Header(default=None)):
        _auth(x_api_token)
        try:
            p = c().repo.export_csv(name)
        except RepoError as e:
            raise HTTPException(404, str(e))
        return FileResponse(p, filename=p.name, media_type="text/csv")

    @app.get("/api/tables/{name}/history")
    def table_history(name: str, limit: int = Query(20, le=200), x_api_token: str | None = Header(default=None)):
        _auth(x_api_token)
        return c().repo.history(name, limit)

    return app


def _process_im(cc: Container, msg) -> None:
    out = ChatOutbox(cc.im)
    try:
        name = cc.im.user_name(msg.from_user)
        inbound = Inbound(user_id=msg.from_user, user_name=name, msg_key=msg.dedupe_key)
        if msg.msg_type == "text":
            inbound.text = msg.content
        elif msg.msg_type == "image":
            inbound.images = [cc.im.download_media(msg.media_id)]
        elif msg.msg_type == "file" and msg.media_id:
            data, _ = cc.im.download_media(msg.media_id)
            inbound.files = [(msg.file_name or "upload.csv", data)]
        elif msg.msg_type == "event" and msg.event == "template_card_event":
            inbound.event_key, inbound.response_code = msg.event_key, msg.response_code
        elif msg.msg_type == "event":
            return   # enter_agent 等事件忽略
        else:
            out.text(msg.from_user, "目前支援文字、圖片與 Excel/CSV 檔。")
            return
        cc.conv.handle(inbound, out)
    except Exception:
        log.exception("process im message failed")
        try:
            out.text(msg.from_user, "系統處理失敗，請稍後再試或聯絡管理員。")
        except Exception:
            pass


class CollectingOutbox:
    """Outbox used by /api/chat and tests."""

    def __init__(self):
        self.items: list[dict] = []

    def text(self, user_id, content):
        self.items.append({"type": "text", "content": content})

    def markdown(self, user_id, content):
        self.items.append({"type": "markdown", "content": content})

    def card(self, user_id, task_id, title, desc, fields, buttons):
        self.items.append({"type": "card", "task_id": task_id, "title": title, "desc": desc,
                           "fields": fields, "buttons": buttons})

    def file(self, user_id, path: Path):
        self.items.append({"type": "file", "path": str(path), "name": Path(path).name})

    def card_done(self, user_id, response_code, label):
        if response_code:
            self.items.append({"type": "card_done", "label": label})


app = create_app()
