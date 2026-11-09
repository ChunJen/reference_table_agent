import base64

from fastapi.testclient import TestClient

from app.main import Container, create_app
from app.messaging.client import clip_utf8
from app.messaging.crypto import MsgCrypt
from app.messaging.messages import parse_message

from .fakes import ScriptedLLM, text, tool

AES_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode().rstrip("=")  # 43 chars
CORP = "corp123"


def test_crypto_roundtrip_and_signature():
    c = MsgCrypt("tok", AES_KEY, CORP)
    enc = c.encrypt("<xml><Content><![CDATA[你好]]></Content></xml>")
    sig = c.signature("1700000000", "n1", enc)
    assert c.verify_url(sig, "1700000000", "n1", enc).endswith("</xml>")


def test_parse_event_and_text():
    m = parse_message("""<xml><ToUserName><![CDATA[ww]]></ToUserName><FromUserName><![CDATA[zhangsan]]></FromUserName>
    <CreateTime>1</CreateTime><MsgType><![CDATA[event]]></MsgType><Event><![CDATA[template_card_event]]></Event>
    <EventKey><![CDATA[confirm|abc|2]]></EventKey><TaskId><![CDATA[abc-2]]></TaskId>
    <ResponseCode><![CDATA[rc]]></ResponseCode><AgentID>1000002</AgentID></xml>""")
    assert (m.msg_type, m.event_key, m.response_code) == ("event", "confirm|abc|2", "rc")
    assert m.dedupe_key.startswith("evt:zhangsan:1:")


def test_clip_utf8():
    s = clip_utf8("中" * 2000)
    assert len(s.encode()) <= 2000 and s.endswith("截斷）")


class FakeChat:
    def __init__(self):
        self.sent = []

    def user_name(self, uid):
        return "張三"

    def send_text(self, uid, c):
        self.sent.append(("text", c))

    def send_markdown(self, uid, c):
        self.sent.append(("markdown", c))

    def send_button_card(self, uid, task_id, title, desc, fields, buttons):
        self.sent.append(("card", buttons))

    def send_file(self, uid, path):
        self.sent.append(("file", path))

    def update_card_button(self, *a):
        self.sent.append(("card_done", a))

    def download_media(self, media_id):
        raise AssertionError


def test_callback_end_to_end(settings, engine):
    settings.im_token, settings.im_aes_key, settings.im_corp_id = "tok", AES_KEY, CORP
    llm = ScriptedLLM([tool("list_tables", {}), text("請問這是哪一張表的資料？")])
    im = FakeChat()
    container = Container(settings, llm=llm, im=im)
    crypt = container.crypto
    with TestClient(create_app(container)) as client:
        echo = crypt.encrypt("echo-123")
        r = client.get("/im/callback", params={
            "msg_signature": crypt.signature("1", "n", echo), "timestamp": "1", "nonce": "n", "echostr": echo})
        assert r.status_code == 200 and r.text == "echo-123"

        inner = ("<xml><ToUserName>ww</ToUserName><FromUserName>zhangsan</FromUserName><CreateTime>2</CreateTime>"
                 "<MsgType>text</MsgType><Content>G01：測試品項C</Content><MsgId>99</MsgId><AgentID>1</AgentID></xml>")
        enc = crypt.encrypt(inner)
        body = f"<xml><ToUserName>ww</ToUserName><Encrypt><![CDATA[{enc}]]></Encrypt></xml>"
        r = client.post("/im/callback", params={
            "msg_signature": crypt.signature("2", "n", enc), "timestamp": "2", "nonce": "n"}, content=body)
        assert r.text == "success"
        bad = client.post("/im/callback", params={"msg_signature": "x", "timestamp": "2", "nonce": "n"},
                          content=body)
        assert bad.status_code == 403
        container.pool.shutdown(wait=True)
    assert im.sent == [("text", "請問這是哪一張表的資料？")]
