# 參照表維護 Agent（Chat × Claude × 關聯式資料庫）

同仁在通訊軟體中傳**文字（有結構或無結構）、截圖、Excel/CSV**，Agent 會：

1. 判斷對應哪張**參照表**（查現有註冊表）；沒有的話**推薦一版建表方式**（英文表名/欄位 + 中文顯示名 + 主鍵 + 型別）
2. 產生**變更預覽**（新增/修改/刪除筆數、逐筆差異、待確認事項）＋「確認更新 / 取消」卡片
3. 使用者可**多輪對話修改**（改欄位、補資料、改成整批覆蓋…），每次都會重新預覽
4. 按下**確認**才寫入：每筆異動壓上 `sys_updated_at`（更新時間）、`sys_updated_by`（傳送者）、`sys_version`，並回傳**最新 CSV**
5. 隨時可**緊急退一版**（`退版 <表名>`），一樣先預覽再確認

BI 工具與 ETL 工具以唯讀帳號直接讀取 `ref.*` 的同一批表。

```
通訊軟體 ──callback──> FastAPI ──背景執行緒──> ConversationService (狀態機)
                                                    │
                      ┌─────────────────────────────┴───────────┐
                      ▼                                         ▼
        ReferenceTableAgent (Claude API)              RefRepository (確定性寫入)
        工具：list_tables / get_table /               plan → diff/驗證
        find_rows / submit_proposal /                 apply → DDL + DML + 快照 + 版本紀錄
        request_rollback（只讀 + 送提案）              rollback / export_csv
                                                       │
                                      ┌────────────────┼──────────────────┐
                                      ▼                ▼                  ▼
                                 ref.<表>         ref_hist.<表>       ref_meta.*
                            (BI/ETL 讀取)    (每版完整快照)   (註冊表/版本紀錄/session)
```

**設計重點：Claude 不直接寫資料庫。** Claude 只能查資料與送出結構化提案；所有寫入由 `RefRepository` 在單一交易中完成，並用樂觀鎖（預覽時的版本號）防止兩人同時改同一張表。

---

## 1. 資料庫設計

| Schema | 內容 | 誰讀 |
|---|---|---|
| `ref` | 現行參照表，例如 `ref.material_group`；若 `CREATE_DISPLAY_VIEWS=true` 另有 `ref.v_material_group`（中文欄位名） | BI / ETL（唯讀帳號） |
| `ref_hist` | 每一版的**完整快照**（`hist_version` + 原欄位），退版用 | Agent |
| `ref_meta` | `ref_registry` 註冊表、`ref_version_log` 版本紀錄、`ref_change_session` 對話、`ref_inbound_message` 原始訊息去重 | Agent（版本紀錄可開給唯讀帳號） |

每張參照表的系統欄位：

| 欄位 | 說明 |
|---|---|
| `sys_updated_at` | 該列最後一次被異動的時間（`TIMEZONE`，不帶時區） |
| `sys_updated_by` | 異動者的使用者 ID（姓名記在版本紀錄 `changed_by_name`） |
| `sys_version` | 該列最後一次被異動的版本號 |

- **部分更新**：只有實際有變的列會重壓時間/傳送者，沒變的列保留原本的值。
- **整批覆蓋**（full_replace）：以傳來的清單為準，計算差異後新增/修改/刪除。
- **版本紀錄** `ref_version_log`：版本號、操作（create / partial / full_replace / rollback）、增改刪筆數、操作人、時間、摘要、**原文來源**（例如原始通知的發文日期、發文者與原因，或申請單名稱）。
- **退一版**：回到「這一版之前」的狀態，並產生一個新版本（不會刪掉歷史）。連續退版會沿著版本鏈往回走；也可指定版本 `退版 material_group 3`。退版後被還原的列同樣壓上退版操作者與時間。
- 支援的結構變更：建表、**新增欄位**。改主鍵、刪欄位、改型別不會自動做（以免下游報表的欄位斷掉），Agent 會說明建議做法。
- 鍵值比對不分大小寫與前後空白，確保在不同資料庫之間行為一致。

### 建立資料庫與帳號

`sql/db_a`、`sql/db_b` 依資料庫分類提供建庫與帳號腳本，以 DBA 帳號執行對應的 `01_*.sql`（先改腳本中的密碼）；若該目錄有 `02_after_first_start.sql`，在第一次啟動 Agent（會自動建立 `ref_meta` 內部表）之後再執行。

`.env` 中以 `DB_URL`（完整 SQLAlchemy URL）設定連線，需要時以 `DB_ENGINE_OPTIONS` 傳入驅動參數；資料庫驅動請依使用的資料庫另外安裝。程式全部用 SQLAlchemy Core，沒有方言專屬 SQL。

| 帳號 | 權限 |
|---|---|
| `ref_agent` | 只在 `ref_agent_db` 內，擁有 `ref`/`ref_hist`/`ref_meta` 三個 schema（建表、加欄位、讀寫） |
| `ref_reader` | `ref` schema 唯讀（含之後新建的表）＋版本紀錄/註冊表唯讀；**看不到**對話內容 |

> 權限設計已在本機實測：唯讀帳號讀得到 Agent 新建的表與中文 view、不能刪資料、讀不到 `ref_change_session`。

---

## 2. 安裝與啟動

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      # 填入 DB、ANTHROPIC_API_KEY、通訊軟體應用參數
.venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8000
```

不接通訊軟體、先在本機試：
```bash
.venv\Scripts\python -m scripts.chat_cli --user <userid> --name "<姓名>"
```
（`/img 路徑` 傳圖、`/file 路徑` 傳 Excel、`/ok` 按確認；多行文字用 `<<` … `>>` 包起來）

### Claude API 設定
- 模型 `ANTHROPIC_MODEL=claude-opus-5-5`，adaptive thinking，`CLAUDE_EFFORT=high`（資料正確性優先；想省成本可試 `medium`）。
- 使用 streaming（大量資料列的工具輸入不會逾時）、prompt caching。
- `CLAUDE_FALLBACKS=true`：開啟 server-side refusal fallback（`fallbacks: "default"`），模型安全分類器誤擋時自動改由後備模型處理；不需要可設 `false`。
- 多輪修改時，整段 Claude 對話（含 thinking blocks）原樣保存在 `ref_change_session.messages_json`，只追加、不改寫。

---

## 3. 通訊軟體整合

服務提供 callback 端點接收訊息；收到訊息後會立刻回應平台，實際處理在背景執行，結果用主動推送（文字、markdown 預覽、按鈕卡片、CSV 檔）。重送的同一則訊息會依訊息 ID 去重。可用 `ALLOWED_USERS` 限定可維護參照表的使用者。

### 使用方式（對話指令）

| 輸入 | 動作 |
|---|---|
| 直接貼文字 / 截圖 / Excel、CSV | Agent 判斷參照表並產生預覽 |
| 預覽後回覆修改意見 | 多輪調整，產生新一版預覽（舊卡片的確認按鈕會失效） |
| 卡片「確認更新」或回覆 `確認`/`是` | 寫入並回傳最新 CSV |
| 卡片「取消」或回覆 `取消` | 放棄本次變更 |
| `退版 <表名> [版本]` | 緊急退版（預覽後確認） |
| `下載 <表名>` | 取得目前 CSV |
| `歷史 <表名>` | 最近的版本紀錄 |
| `清單` / `說明` | 列出參照表 / 使用說明 |

範例情境（見 `tests/test_conversation.py`）：
- **文字**：貼上一段新增代碼的通知 → 沒有表時推薦建立對應參照表與主鍵；已有表時以**部分新增**處理，原文的發文者與原因記入 `source_note`。
- **圖片**：申請單截圖「A 改為 B」→ Agent 用 `find_rows` 以舊說明反查代碼後只更新說明欄；若圖片可能被裁切會提醒確認。

---

## 4. BI / ETL 工具串接

1. 在 BI / ETL 工具中建立資料庫連線：資料庫 `ref_agent_db`，帳號用 **`ref_reader`**（唯讀），schema 選 `ref`。
2. 將參照表加入資料集：
   - 想直接用中文欄位名：選 `v_<表名>` 這種 view（需 `CREATE_DISPLAY_VIEWS=true`）；或選原表後套用欄位備註作為顯示名稱（欄位備註已寫入資料庫）。
3. 更新方式：
   - 參照表筆數小，建議用**直連**，Agent 更新後報表立即看到最新資料；
   - 若用**抽取**，請設定定時更新（例如每小時），或在 Agent 回傳 CSV 後手動更新。
4. ETL 任務可以 `ref.<表>` 做**關聯（Join）**補上對照欄位；需要只抓有異動的資料時，可用 `sys_updated_at` / `sys_version` 做增量條件，並以 `ref_meta.ref_version_log` 的最新版本判斷是否需要重跑下游。
5. 想做「參照表異動紀錄」報表：把 `ref_meta.ref_version_log` 也加入資料集（誰、何時、改了幾筆、原因）。

> 只新增欄位不會影響既有報表；新欄位需在資料集中重新選擇欄位後才會出現。

---

## 5. 開發與測試

```bash
# 需要一個可連線的測試資料庫（每個測試用隨機 schema，結束後清掉）
set TEST_DB_URL=<dialect+driver>://<user>:<pwd>@<host>:<port>/ref_agent_test
.venv\Scripts\python -m pytest -q
```
測試用腳本化的假 LLM（`tests/fakes.py`），涵蓋：文字範本建表＋多輪修改＋舊卡片失效＋他人不可代按、圖片範本依舊值改名＋確認＋退版、Agent 提問後回「是」、提案驗證失敗回饋給模型、訊息加解密/簽名/callback 端對端。

開發用 API（`DEV_API_ENABLED=true`，正式環境請關閉或設 `DEV_API_TOKEN`）：
- `POST /api/chat` `{"user_id","user_name","text","image_base64"}`：模擬一則訊息
- `GET /api/tables`、`GET /api/tables/{name}/csv`、`GET /api/tables/{name}/history`

## 6. 目錄

```
app/
  config.py        設定（.env）
  db.py            engine、ref_meta 內部表、系統欄位
  models.py        提案/欄位規格（pydantic）＋型別轉換
  repository.py    差異計算、交易寫入、快照、退版、CSV
  agent.py         Claude 工具迴圈與系統提示
  conversation.py  對話狀態機、指令、預覽渲染、檔案解析
  main.py          FastAPI：callback、開發 API
  messaging/       通訊軟體整合：加解密、API client、訊息解析
sql/               各資料庫的建庫與帳號腳本
scripts/chat_cli.py  本機對話測試
tests/
```
