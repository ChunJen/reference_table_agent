"""Runtime configuration (env vars / .env)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---- Database -------------------------------------------------------
    # 完整 SQLAlchemy URL：<dialect+driver>://<user>:<password>@<host>:<port>/<db>
    db_url: str = ""
    # 額外傳給 create_engine 的參數（JSON），依資料庫驅動需要設定
    db_engine_options: dict = {}

    schema_ref: str = "ref"          # 給 BI / ETL 讀取的現行參照表
    schema_hist: str = "ref_hist"    # 每一版完整快照（退版用）
    schema_meta: str = "ref_meta"    # Agent 內部：註冊表、版本紀錄、對話 session
    create_display_views: bool = False  # 另建 ref.v_<table>，欄位用中文顯示名
    timezone: str = "UTC"            # IANA 時區名稱

    # ---- Claude ---------------------------------------------------------
    anthropic_model: str = "claude-opus-5-5"
    claude_effort: str = "high"
    claude_max_tokens: int = 64000
    claude_fallbacks: bool = True     # server-side refusal fallback ("default")
    claude_max_tool_rounds: int = 12

    # ---- Messaging platform (通訊軟體應用) ------------------------------
    im_corp_id: str = ""
    im_agent_id: int = 0
    im_secret: str = ""
    im_token: str = ""             # callback Token
    im_aes_key: str = ""           # AES key (43 chars)
    im_api_base: str = ""         # 通訊軟體 server API base URL

    # ---- Behaviour ------------------------------------------------------
    allowed_users: str = ""           # 逗號分隔 userid，空白=不限制
    session_ttl_hours: int = 24
    max_rows_per_change: int = 5000
    preview_rows: int = 8
    csv_header: str = "display"       # display=中文顯示名, name=欄位名
    export_dir: Path = Path("exports")
    dev_api_enabled: bool = True      # /api/chat 模擬對話 (正式環境請關閉或加防護)
    dev_api_token: str = ""

    def sqlalchemy_url(self) -> str:
        if not self.db_url:
            raise ValueError("請在 .env 設定 DB_URL")
        return self.db_url

    @property
    def allowed_user_set(self) -> set[str]:
        return {u.strip() for u in self.allowed_users.split(",") if u.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
