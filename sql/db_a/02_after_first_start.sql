-- Agent 第一次啟動（自動建立 ref_meta 內部表）後執行一次：
-- 唯讀帳號只保留版本紀錄與註冊表；對話內容與原始訊息不開放
REVOKE SELECT ON ref_meta.ref_change_session  FROM ref_reader;
REVOKE SELECT ON ref_meta.ref_inbound_message FROM ref_reader;
GRANT  SELECT ON ref_meta.ref_version_log, ref_meta.ref_registry TO ref_reader;
