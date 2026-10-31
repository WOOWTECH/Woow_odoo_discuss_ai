# Woow_odoo_discuss_ai 設計

Odoo 18。讓內部用戶與 portal 用戶在 Discuss 裡和「AI 聯絡人」一對一私訊；AI 聯絡人背後接 Hermes、公司 LiteLLM（OpenAI 相容）或 n8n webhook。同一套後端連接之後也接手 Live Chat AI（取代 `im_livechat_ai`）。

定案來源：`work/woow-ai-contact-20261031/COORD.md`（負責人 2026-10-31）。

## 模組

| 模組 | 相依 | 安裝 | 內容 |
|---|---|---|---|
| `woow_ai_contact` | `mail`, `portal` | 手動 | AI 聯絡人、後端連接、回覆佇列、多媒體、推送 API、Discuss 側欄、portal 入口、呼叫紀錄 |
| `woow_ai_contact_livechat` | `woow_ai_contact`, `im_livechat` | `auto_install` | Live Chat 頻道選 AI 聯絡人、轉真人／暫停、關鍵字轉真人、從 `im_livechat_ai` 搬移 |

不相依 `cs_portal_discuss`／`woow_odoo_portal_discuss`。有裝 `cs_portal_discuss` 的站，AI 私訊會自動出現在 `/my/discussions`（它是 `channel_type = 'chat'` 且使用者是成員）。

## 資料模型（woow_ai_contact）

### `woow.ai.contact`（AI 聯絡人）
| 欄位 | 說明 | 可見 |
|---|---|---|
| `name`、`image_1920`、`description` | 顯示用；`partner_id` 同步名稱與頭像 | 有權使用者 |
| `partner_id` | 機器人 partner（建立時自動產生，`active=True`，無 user，標籤「AI Contact」；AI 聯絡人封存時一併封存） | 有權使用者 |
| `active`、`sequence` | | |
| `allowed_group_ids` | 可使用的群組；預設 `base.group_user`＋`base.group_portal` | 系統管理員 |
| `backend_type` | `openai`（Hermes／LiteLLM）或 `n8n` | 系統管理員 |
| `endpoint_url` | openai：base URL（…/v1）；n8n：webhook 完整網址 | 系統管理員 |
| `api_key` | 只寫不讀：非儲存 compute／inverse 欄位，實際值存在只用 SQL 存取的欄 `woow_ai_contact.api_key_secret`（不是 ORM 欄位，任何 read／export／表單都拿不到）；另有 `api_key_set` 顯示是否已設定 | 系統管理員 |
| `model` | openai 用；可空（Hermes 用自己的預設） | 系統管理員 |
| `system_prompt` | 可空；空白＝完全交給後端角色 | 系統管理員 |
| `max_history` | 預設 50，1–200（同 livechat） | 系統管理員 |
| `timeout_seconds` | 預設 300 | 系統管理員 |
| `welcome_message`、`error_message` | 第一次開對話的招呼語、失敗時的回覆 | 系統管理員 |
| `download_domains` | AI 回覆中可下載成附件的網域白名單（每行一個，精確主機名或 `*.example.com`） | 系統管理員 |
| `max_download_mb` | 預設 20 | 系統管理員 |
| `push_key_hash`、`push_key_hint` | 推送 API 金鑰（只存 sha256；「產生推送金鑰」按鈕只顯示一次） | 系統管理員 |
| `log_payload` | 預設 False；開了才在呼叫紀錄存請求／回應內容（除錯用，會提示隱私） | 系統管理員 |

### `woow.ai.job`（回覆工作）
沿用 `im_livechat_ai` 1.1.0 C 包的設計並一般化：
- 使用者訊息與 job 在同一交易建立；commit 後（`cr.postcommit`）起 worker thread（非 daemon），debounce 1.5 秒。
- 每個 `discuss.channel` 一把 PostgreSQL advisory lock；同頻道只處理最新一筆（較舊的 skipped），最新那筆帶完整歷史。
- 復原 cron 每 2 分鐘接手 pending／running 卡住的 job；每筆最多 3 次；超過 30 分鐘放棄。
- 呼叫後端前與張貼前都再檢查一次（是否有更新的 job、livechat 是否暫停）。
- 欄位：`channel_id`、`contact_id`、`message_id`（觸發訊息）、`kind`（reply／handoff）、`state`、`run_count`、`run_after`、`error`、`reply_message_id`。

### `woow.ai.call.log`（呼叫紀錄，只有系統管理員可讀）
`contact_id`、`channel_id`、`job_id`、`backend_type`、`status`（ok／http_error／timeout／invalid_response／error）、`http_status`、`duration_ms`、`error`（截斷）、`request_payload`／`response_payload`（只有 `log_payload` 開時才存）。每日清除 30 天前的紀錄。

## 觸發

`discuss.channel` 覆寫張貼後的鉤子（與 `im_livechat_ai` 相同位置 `_notify_thread`，或 `message_post` 後）：
- `channel_type == 'chat'`，成員中有一位是 AI 聯絡人的 `partner_id`，作者不是該機器人 partner；
- 作者的 user 屬於 `allowed_group_ids`（否則回一句「您沒有使用這個 AI 聯絡人的權限」，不呼叫後端）；
- `message_type == 'comment'`，有文字或附件。
- 建 job（kind=reply）。

「輸入中」：job 開始呼叫後端時，機器人成員 `discuss.channel.member._notify_typing(True)`，結束（成功或失敗）時 `False`；長時間呼叫每 20 秒重送一次 True（前端約 60 秒會自動清除）。

## 後端協定

兩種後端都送同樣的上下文：系統提示詞（可空）＋最近 `max_history` 則（同 livechat：使用者→user，機器人→assistant），另附固定 `session_id = "woow-ai:<db uuid 前 8 碼>:<channel_id>"`，後端想自己做長期記憶可用。

身分（不代登入）：`user_id`、`partner_id`、`name`、`email`、`lang`、`tz`、`is_portal`、`company_id`。

### openai（Hermes、公司 LiteLLM、任何 /v1/chat/completions）
```
POST {endpoint_url}/chat/completions
Authorization: Bearer {api_key}
{
  "model": "{model}",            # 空白則不送
  "messages": [...],
  "user": "{session_id}",
  "metadata": {"session_id": ..., "odoo_user": {...}}   # 後端不認得就忽略
}
```
- 歷史中的使用者附件：圖片（≤5 MB，image/png|jpeg|gif|webp）只在**最近 3 則**使用者訊息內嵌為 `image_url` data URI；其餘附件與更舊的圖片改成文字行 `[附件] 檔名（類型，大小）：簽名網址`。
- 回覆取 `choices[0].message.content`（字串，或 content parts 中的 text）。
- 不送 `tools`；工具全部由後端自己處理（Hermes／n8n 用 Odoo MCP）。

### n8n（webhook，同步回應）
```
POST {endpoint_url}
Authorization: Bearer {api_key}
X-Woow-Timestamp: <unix>
X-Woow-Signature: sha256=<hex HMAC-SHA256(api_key, "<timestamp>.<raw body>")>
{
  "event": "message",
  "session_id": "...",
  "contact": {"id": 3, "name": "產品顧問"},
  "user": {...},
  "channel": {"id": 42, "type": "chat"},          # livechat 時 type=livechat，另附 livechat 欄位
  "system_prompt": "...",
  "message": {"id": 991, "text": "...", "attachments": [ATT, ...]},
  "history": [{"role": "user|assistant", "text": "...", "attachments": [ATT]}]
}
ATT = {"id": 12, "name": "報價.xlsx", "mimetype": "...", "size": 12345,
       "url": "https://<odoo>/woow_ai/attachment/12/<token>", "expires_at": "<ISO8601>"}
```
回應（HTTP 200，JSON）：
```
{"reply": {"text": "markdown 文字", "attachments": [{"name": "...", "mimetype": "...", "data_base64": "..."} | {"name": "...", "url": "https://..."}]}}
或 {"replies": [ {同上}, ... ]}      # 多則
或 {"skip": true}                   # 不回覆
```
`url` 型附件同樣受白名單與大小限制。

## 回覆呈現（兩種後端共用）

1. 文字當 markdown 子集轉成安全 HTML：段落、換行、粗體、斜體、刪除線、行內程式碼、程式碼區塊、標題（h3–h5）、清單（可巢狀）、引言與分隔線、連結（只允許 http/https/mailto，裸網址自動連結）、**GFM 表格**（轉 `<table class="table table-sm table-bordered">`）。先跳脫再組標籤，最後 `html_sanitize`。不用外部套件。`<think>…</think>` 先移除。引言／分隔線輸出成有樣式的 `div`：`<blockquote>`、`<hr>` 會被 Odoo 標成郵件引文（`data-o-mail-quote`），Discuss 會把它摺疊起來。
2. markdown 圖片 `![alt](url)` 與看起來是檔案的連結（副檔名 png/jpg/jpeg/gif/webp/mp4/webm/mov/pdf/xlsx/xls/csv/docx/pptx/zip）：網域在白名單內 → 下載（不跟隨轉址到白名單外、串流計量、超過 `max_download_mb` 就停）存成 `ir.attachment` 掛在回覆訊息上，文字中那段換成檔名；不在白名單 → 保留成連結。
3. 附件 mimetype 以實際內容判斷（`odoo.tools.mimetypes.guess_mimetype`），不信後端宣稱的類型；HTML／SVG／JS 一律改成 `application/octet-stream`。
4. 機器人以 `author_id = contact.partner_id`、`message_type='comment'`、`subtype mail.mt_comment` 張貼；`body_is_html` 由 `Markup` 處理。

## 使用者附件的簽名網址

`GET /woow_ai/attachment/<int:id>/<token>`，`auth='public'`：
- `token = <exp>.<hex HMAC-SHA256(database.secret, "woow_ai|<id>|<checksum>|<exp>")>`，有效 1 小時；
- 只簽發「該私訊頻道內的訊息附件」；驗章通過才 sudo 串流檔案（`ir.binary`），`Content-Disposition: attachment`，`X-Content-Type-Options: nosniff`。

## 推送 API（主動發訊）

```
POST /woow_ai/v1/push           auth='none', csrf=False, type='http'（JSON body）
Authorization: Bearer <push key>      # 對應到某個 AI 聯絡人
{"to": {"user_id": 7} | {"partner_id": 15} | {"email": "a@b.c"},
 "text": "markdown", "attachments": [同 n8n 回應格式]}
→ 200 {"ok": true, "channel_id": 42, "message_id": 1001}
  401 金鑰錯誤；404 找不到使用者；403 使用者不在 allowed_group_ids；400 格式錯誤
```
- 找或建該使用者與 AI 的私訊頻道，以機器人身分張貼（同上呈現規則）。
- 金鑰以常數時間比對 sha256。不限速（負責人定案：不限用量）。
- 實作補充：`auth='none'` 的路由在 Odoo 18 預設用唯讀 cursor，所以明確設 `readonly=False`；資料庫必須能由網域判斷（單一資料庫或 dbfilter，PaaS 租戶即是）。收件人必須是有效使用者（只有 partner、沒有 user → 404）；`email` 對到多個使用者 → 400 `ambiguous_recipient`。推送也寫一筆呼叫紀錄（`backend_type = push`，不存內容）。推送不會觸發 AI 回覆。

## 入口

- **後台 Discuss 側欄**：不 patch 既有元件，而是在 Odoo 18 的側欄登錄表 `mail.discuss_sidebar_items` 加一個元件（sequence 25：在信箱 20 之後、頻道／私訊分類 30 之前，也就是「私訊」上方），只載入 `web.assets_backend`（公開頁不顯示）。列出目前使用者可用的 AI 聯絡人（`/woow_ai/contacts` JSON-RPC）；點了呼叫 `/woow_ai/open`（找或建私訊，第一次建立時張貼 `welcome_message`），回傳 `Store` 資料並在 Discuss 開啟。
- **portal**：`/my` 首頁卡片「AI 聯絡人」（portal 計數器 `woow_ai_contact_count`，沒有可用的 AI 聯絡人就不顯示）→ `/my/ai`（卡片清單）→ `/my/ai/<contact_id>/open` 找或建私訊後導到 `/discuss/channel/<id>`（Odoo 原生公開 Discuss 畫面，附件上傳／圖片預覽皆為 Odoo 原生）；有裝 `cs_portal_discuss` 時才加 `?discussions=1`（沒裝時 Odoo 會對未知參數記 warning）。內部用戶改導到後台 `/odoo/discuss?active_id=discuss.channel_<id>`。
- AI 私訊一律設 `allow_public_upload = True`：Odoo 的 `/mail/attachment/upload` 對非內部用戶只允許在有開這個旗標的頻道上傳（沙盒實測：沒開時 portal 上傳回 403「本頻道不允許上載附件」）。私訊只有兩個成員，開了不擴大存取範圍。
- 一般「開始對話」搜尋也找得到（partner 有效即可）；權限仍以 `allowed_group_ids` 在觸發時把關。

## 權限

- `woow.ai.contact`：系統管理員全權；其他人只能讀（record rule：自己在 `allowed_group_ids` 內），且敏感欄位都有 `groups='base.group_system'`。
- `woow.ai.job`、`woow.ai.call.log`：只有系統管理員。
- 私訊內容：只有成員看得到（Odoo 原生）；呼叫紀錄預設不存內容。
- 後端網址只有系統管理員能改；`endpoint_url` 必須是 https（`localhost`／叢集內 `*.svc.cluster.local` 例外，給 PaaS 內網用）。

## woow_ai_contact_livechat

- `im_livechat.channel.ai_contact_id`（只能選既有 AI 聯絡人；Live Chat 管理員可選，網址金鑰仍只有系統管理員看得到）、`ai_enabled`。
- 訪客訊息觸發 job；真人客服張貼 → 暫停 AI（預設 30 分鐘，可設）；訪客說「轉真人」等關鍵字 → handoff job 張貼固定訊息並通知客服（沿用 1.1.0 D 包）。
- 角色：訪客→user、AI→assistant、真人客服→assistant 並加「[真人客服 名字]」前綴（沿用 1.1.0）。
- 不提供 Odoo 端商品工具（負責人定案：工具全部交給後端）。上線前要在後端（Hermes／n8n）用 Odoo MCP 補上查價，並以 L1 實測的價格案例回歸。
- 搬移：偵測到 `im_livechat_ai` 已安裝 → 以 1.1.0 的系統 LiteLLM 設定建立一個 openai 型 AI 聯絡人（每組不同的模型＋提示詞各一個），把頻道對應過去；細節依 L1 的 `L1-findings.md`（1.1.0 最終資料結構）定。

## 測試計畫（沙盒，不碰正式站）

- 單元：觸發條件、權限、markdown→HTML（含表格與惡意輸入）、白名單下載（轉址、超大、假 mimetype）、簽名網址（過期、竄改）、推送 API（401／403／404）、佇列（連發只回最新、重啟恢復、逾時）。
- 整合：沙盒 ns 跑真的 Hermes／n8n？—— n8n 用沙盒內的假 webhook（簡單 HTTP 伺服器）＋正式 n8n 的測試 workflow；openai 型用 LiteLLM 測試金鑰。
- 瀏覽器：內部用戶側欄開 AI、portal 用戶 `/my/ai` 開對話、上傳圖片／PDF、收到表格與圖片附件。

## 實作補充與差異（18.0.1.0.0）

- **Bridge 掛勾**（給 `woow_ai_contact_livechat`）：`discuss.channel._woow_ai_channel_contact()`（這個頻道由哪個 AI 聯絡人回答；核心＝含機器人成員的 chat）、`_woow_ai_check_author()`（ok／denied／ignore）、`_woow_ai_message_kind()`（reply／handoff／None）、`_woow_ai_is_paused()`、`_woow_ai_author_role()`（歷史角色與前綴，例如「[真人客服 名字]」）、`_woow_ai_requester()`（身分要送誰）、`_woow_ai_channel_info()`（n8n 的 `channel` 物件）。`woow.ai.job` 的 `_process_handoff()`、`_notify_failure()` 也是掛勾；核心版 handoff 直接 skipped、失敗只記 log（1 對 1 私訊裡使用者已看到錯誤訊息，沒有客服要通知）。
- **佇列**：與 1.1.0 相同（postcommit 非 daemon thread、debounce 1.5 秒、每頻道 advisory lock、READ COMMITTED cursor、張貼前鎖頻道列再檢查一次、復原 cron 每 2 分鐘、每筆最多 3 次、30 分鐘放棄）。檔案下載在鎖頻道列**之前**做（不寫資料庫），鎖住後才建附件並張貼，避免長時間擋住使用者發訊。HTTP 呼叫期間沒有開著的交易（逾時秒數等設定在 commit 前先讀好）。
- **輸入中**：開始呼叫時 `True`、張貼前 `False`；另起一個 daemon thread 用自己的 cursor 每 20 秒重送 `True`。沙盒實測 45 秒呼叫：True 於 0／20／40 秒、結束送 False。
- **重試**：連線失敗（請求沒送出）3 秒後重試一次；讀取逾時與 HTTP 錯誤不重試（n8n 不保證冪等）。
- **n8n 回應**寬鬆接受 `{"reply": "文字"}` 與 n8n 的 item 陣列 `[{...}]`；`replies` 最多 10 則。
- **openai `metadata`**：照規格送出。LiteLLM／Hermes 會忽略；但 OpenAI 官方 API 在沒有 `store` 時可能拒絕 `metadata`（且只收字串值），直接接 OpenAI 官方時要注意（目前沒有開關）。
- **附件 mimetype**：`ir.attachment` 會把 `application/octet-stream` 依內容重新判斷（HTML 會變回 `text/html`），所以建立後再單獨寫一次 mimetype（不帶內容時不會重判）。
- **呼叫紀錄**：`backend_type` 多一個 `push`；每日 cron 同時清除 30 天前的呼叫紀錄與已結束的 job。關閉「記錄請求內容」時清除該 AI 聯絡人已存的內容；記錄時 data URI 與 base64 附件只留長度。
- **推送金鑰**：格式 `wpk_` 開頭；精靈不存檔，金鑰只在產生按鈕的回應 context 裡出現一次；畫面只留前 8 碼＋末 4 碼提示。
- **預設訊息**（無權限、錯誤）以收訊者語言翻譯（`zh_TW.po`）。
- **沒有** `cs_portal_discuss` 的映像（paas-odoo-official:20261030.1113 沒有這個模組）一樣可用：portal 用 Odoo 原生 `/discuss/channel/<id>` 開啟與發訊（HttpCase），上傳圖片／PDF 並收到 AI 回覆（沙盒對真的 Odoo 伺服器實測）。
