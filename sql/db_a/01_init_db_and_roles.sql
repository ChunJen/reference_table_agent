-- =====================================================================
-- 參照表 Agent — 資料庫初始化（以 DBA 身分執行）
-- 建立：
--   ref_agent_db  獨立資料庫
--   ref_agent     Agent 專用讀寫帳號（只能動自己 DB 內的 ref / ref_hist / ref_meta）
--   ref_reader    BI / ETL 唯讀帳號（只看 ref 現行參照表 + 版本紀錄）
-- 請先把下面兩個密碼換掉
-- =====================================================================

CREATE ROLE ref_agent  LOGIN PASSWORD 'CHANGE_ME_agent'  NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE ref_reader LOGIN PASSWORD 'CHANGE_ME_reader' NOSUPERUSER NOCREATEDB NOCREATEROLE;

CREATE DATABASE ref_agent_db OWNER ref_agent ENCODING 'UTF8' TEMPLATE template0;
REVOKE ALL ON DATABASE ref_agent_db FROM PUBLIC;
GRANT CONNECT ON DATABASE ref_agent_db TO ref_agent, ref_reader;

\connect ref_agent_db

REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- schema 由 ref_agent 擁有：Agent 可在其中建表/加欄位，但碰不到其他資料庫
CREATE SCHEMA IF NOT EXISTS ref      AUTHORIZATION ref_agent;   -- 現行參照表（BI/ETL 讀這裡）
CREATE SCHEMA IF NOT EXISTS ref_hist AUTHORIZATION ref_agent;   -- 每版完整快照（退版用）
CREATE SCHEMA IF NOT EXISTS ref_meta AUTHORIZATION ref_agent;   -- 註冊表、版本紀錄、對話 session

-- 唯讀帳號：ref 全部表（含 Agent 之後新建的表）
GRANT USAGE ON SCHEMA ref TO ref_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA ref TO ref_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE ref_agent IN SCHEMA ref GRANT SELECT ON TABLES TO ref_reader;

-- 唯讀帳號：版本紀錄 / 註冊表（可做「參照表異動紀錄」報表）。不開放 session（含對話內容）
GRANT USAGE ON SCHEMA ref_meta TO ref_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE ref_agent IN SCHEMA ref_meta GRANT SELECT ON TABLES TO ref_reader;
-- ↑ 預設權限會讓 ref_meta 之後新建的表都可讀；Agent 第一次啟動建表後，執行 02_after_first_start.sql 收回 session/inbound 的讀取權
