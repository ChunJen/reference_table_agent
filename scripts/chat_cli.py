"""本機對話測試（不需接通訊軟體，會呼叫真的 Claude API 與 .env 設定的資料庫）。

  python -m scripts.chat_cli --user <userid> --name "<姓名>"
指令：
  /img <路徑>    傳送圖片          /file <路徑>   傳送 xlsx/csv
  /ok            按下最新卡片的確認  /cancel        按下取消
  多行文字：單獨一行輸入 << 開始，再一行 >> 結束
"""
from __future__ import annotations

import argparse
import mimetypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.conversation import Inbound  # noqa: E402
from app.main import CollectingOutbox, Container  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="local_tester")
    ap.add_argument("--name", default=None)
    a = ap.parse_args()
    c = Container(get_settings())
    last_buttons: list[dict] = []
    print("參照表助理（本機）。輸入「說明」看用法，Ctrl+C 離開。多行輸入：<< 開始、>> 結束。")
    while True:
        try:
            first = input("\n你> ")
        except (EOFError, KeyboardInterrupt):
            break
        msg = Inbound(user_id=a.user, user_name=a.name)
        if first.startswith("/img "):
            p = Path(first[5:].strip().strip('"'))
            msg.images = [(p.read_bytes(), mimetypes.guess_type(p.name)[0] or "image/png")]
            msg.text = input("（附加說明，可空白）> ") or None
        elif first.startswith("/file "):
            p = Path(first[6:].strip().strip('"'))
            msg.files = [(p.name, p.read_bytes())]
        elif first.strip() in ("/ok", "/cancel"):
            want = "confirm" if first.strip() == "/ok" else "cancel"
            key = next((b["key"] for b in last_buttons if b["key"].startswith(want)), None)
            if not key:
                print("（沒有可按的卡片）")
                continue
            msg.event_key = key
        elif first.strip() == "<<":
            lines = []
            while (line := input("... ")).strip() != ">>":
                lines.append(line)
            msg.text = "\n".join(lines)
        else:
            msg.text = first
        out = CollectingOutbox()
        c.conv.handle(msg, out)
        for m in out.items:
            if m["type"] in ("text", "markdown"):
                print(f"\n助理> {m['content']}")
            elif m["type"] == "card":
                last_buttons = m["buttons"]
                print(f"\n[卡片] {m['title']} — {m['desc']}  " + "  ".join(f"[{b['text']}]" for b in m["buttons"])
                      + "   (輸入 /ok 或 /cancel)")
            elif m["type"] == "file":
                print(f"\n[檔案] {m['path']}")


if __name__ == "__main__":
    main()
