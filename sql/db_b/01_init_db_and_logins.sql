-- =====================================================================
-- 參照表 Agent — 資料庫初始化（以 DBA 身分執行）
-- 請先把下面兩個密碼換掉
-- =====================================================================
USE master;
GO
IF DB_ID(N'ref_agent_db') IS NULL
    CREATE DATABASE ref_agent_db;  -- 需要時自行指定 collation（建議不分大小寫）
GO
IF SUSER_ID(N'ref_agent') IS NULL
    CREATE LOGIN ref_agent  WITH PASSWORD = N'CHANGE_ME_agent_Str0ng!',  DEFAULT_DATABASE = ref_agent_db, CHECK_POLICY = ON;
IF SUSER_ID(N'ref_reader') IS NULL
    CREATE LOGIN ref_reader WITH PASSWORD = N'CHANGE_ME_reader_Str0ng!', DEFAULT_DATABASE = ref_agent_db, CHECK_POLICY = ON;
GO

USE ref_agent_db;
GO
IF USER_ID(N'ref_agent')  IS NULL CREATE USER ref_agent  FOR LOGIN ref_agent;
IF USER_ID(N'ref_reader') IS NULL CREATE USER ref_reader FOR LOGIN ref_reader;
GO
IF SCHEMA_ID(N'ref')      IS NULL EXEC(N'CREATE SCHEMA ref      AUTHORIZATION ref_agent');
IF SCHEMA_ID(N'ref_hist') IS NULL EXEC(N'CREATE SCHEMA ref_hist AUTHORIZATION ref_agent');
IF SCHEMA_ID(N'ref_meta') IS NULL EXEC(N'CREATE SCHEMA ref_meta AUTHORIZATION ref_agent');
GO
-- Agent：只能在本 DB 建表/建 view；擁有三個 schema 因此可 ALTER / DML
GRANT CREATE TABLE, CREATE VIEW TO ref_agent;
GRANT VIEW DEFINITION TO ref_agent;
-- 寫欄位中文備註需要的權限已含在 schema owner 內
GO
-- BI / ETL 唯讀：schema 層級授權，之後新建的表自動可讀
GRANT SELECT ON SCHEMA::ref TO ref_reader;
GO
-- 版本紀錄/註冊表（Agent 第一次啟動建立表之後再執行這段）：
-- GRANT SELECT ON ref_meta.ref_version_log TO ref_reader;
-- GRANT SELECT ON ref_meta.ref_registry    TO ref_reader;
