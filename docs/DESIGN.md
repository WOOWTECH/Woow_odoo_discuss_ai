# Woow_odoo_discuss_ai 設計

Odoo 18。讓內部用戶與 portal 用戶在 Discuss 裡和「AI 聯絡人」一對一私訊；AI 聯絡人背後接 Hermes、公司 LiteLLM（OpenAI 相容）或 n8n webhook。同一套後端連接之後也接手 Live Chat AI（取代 `im_livechat_ai`）。

定案來源：`work/woow-ai-contact-20261031/COORD.md`（負責人 2026-10-31，定案 1–28；第二批 21–28 見文末「負責人第二批定案」）。線路格式的完整說明在 [PROTOCOL.md](PROTOCOL.md)（英文，給 Hermes／n8n 串接者）。

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
| `partner_id` | 機器人 partner（建立時自動產生，無 user，標籤「AI Contact」）。`active`＝AI 聯絡人有效**且** `allowed_group_ids` 不是空的；AI 聯絡人封存，或群組清空（只給 Live Chat 用），partner 就保持封存，一般「開始對話」搜尋找不到 | 有權使用者 |
| `active`、`sequence` | | |
| `allowed_group_ids` | 可使用的群組；預設 `base.group_user`＋`base.group_portal`。**空集合＝不能私訊**（側欄、portal、推送都不列；只給 Live Chat 等橋接用） | 系統管理員 |
| `backend_type` | `openai`（Hermes／LiteLLM）或 `n8n` | 系統管理員 |
| `endpoint_url` | openai：base URL（…/v1）；n8n：webhook 完整網址 | 系統管理員 |
| `api_key` | 只寫不讀：非儲存 compute／inverse 欄位，實際值存在只用 SQL 存取的欄 `woow_ai_contact.api_key_secret`（不是 ORM 欄位，任何 read／export／表單都拿不到）；另有 `api_key_set` 顯示是否已設定。openai：Bearer 金鑰；**n8n：HMAC 簽章金鑰，表單標籤「簽章金鑰」，永遠不送出** | 系統管理員 |
| `model` | openai 用；可空（Hermes 用自己的預設） | 系統管理員 |
| `system_prompt` | 可空；空白＝完全交給後端角色 | 系統管理員 |
| `max_history` | 預設 50，1–200（同 livechat） | 系統管理員 |
| `timeout_seconds` | 預設 300（5–3600）；整個回覆的總時限（串流時另有 30 秒空閒逾時） | 系統管理員 |
| `openai_stream` | 預設 True；openai 型要求 SSE 串流（定案 21） | 系統管理員 |
| `max_retries` | 預設 1，0–2；只對連線失敗、HTTP 429、503（定案 26） | 系統管理員 |
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
`contact_id`、`channel_id`、`job_id`、`backend_type`、`status`（ok／http_error／timeout／invalid_response／error）、`http_status`、`duration_ms`、`prompt_tokens`／`completion_tokens`／`total_tokens`（openai 回應的 `usage`，沒有就是 0）、`error`（截斷；回覆中殘留 `MEDIA:` 路徑、內嵌圖被拒也記在這裡，狀態仍是 ok）、`request_payload`／`response_payload`（只有 `log_payload` 開時才存，data URI 與 base64 只留長度）。每日清除 30 天前的紀錄。

## 觸發

`discuss.channel` 覆寫張貼後的鉤子（與 `im_livechat_ai` 相同位置 `_notify_thread`，或 `message_post` 後）：
- `channel_type == 'chat'`，成員中有一位是 AI 聯絡人的 `partner_id`，作者不是該機器人 partner；
- 作者的 user 屬於 `allowed_group_ids`（否則回一句「您沒有使用這個 AI 聯絡人的權限」，不呼叫後端）；
- `message_type == 'comment'`，有文字或附件。
- 建 job（kind=reply）。

「輸入中」：job 開始呼叫後端時，機器人成員 `discuss.channel.member._notify_typing(True)`，結束（成功或失敗）時 `False`；長時間呼叫每 20 秒重送一次 True（前端約 60 秒會自動清除）。

## 後端協定

兩種後端都送同樣的上下文：系統提示詞（可空，`discuss.channel._woow_ai_system_prompt(contact, history)`，預設＝`contact.system_prompt`）＋最近 `max_history` 則（同 livechat：使用者→user，機器人→assistant；每則文字上限 16,000 字，超過截斷並加「…[truncated]」），另附固定 `session_id = "woow-ai-<db uuid 前 8 碼>-<channel_id>"`（定案 22，不用冒號），後端想自己做長期記憶可用。

身分（不代登入）：`discuss.channel._woow_ai_identity(contact)`，預設＝`contact._identity(_woow_ai_requester(contact))`：`user_id`、`partner_id`、`name`、`email`、`lang`、`tz`、`is_portal`、`company_id`（橋接可加 `is_guest`、`guest_id`、`visitor_key`）。n8n 的 `user` 照帶全部；openai 型在 system 訊息加一段模型看得到的身分區塊（姓名、語言、時區、帳號種類、user_id／partner_id，**不含 email**，定案 27），`metadata` 照帶（含 email）。預設訊息（錯誤、拒絕、回覆內註記）的語言也取自這裡的 `lang`。

逾時與重試（兩種後端共用）：連線 10 秒；openai 串流時 30 秒沒收到任何位元組就失敗，另外整個回覆不超過 `timeout_seconds`（requests 的 read timeout 只算單次讀取，所以總時限自己檢查）；非串流與 n8n 的讀取逾時＝`timeout_seconds`。`max_retries`（0–2，預設 1）只對連線失敗、HTTP 429、503，退避 3 秒、10 秒，總時間不超過 `timeout_seconds`；讀取逾時、502、504、524 與其他回應一律不重送（Hermes 的工具可能已經執行一半，定案 26）。

**Cloudflare 100 秒**：經對外網址的請求約 100 秒沒有位元組會被回 524。對策（定案 21）：openai 型預設串流（Hermes 每 10 秒送 keepalive、LiteLLM 持續送 token）；n8n 長流程先回 `{"skip": true}` 再用推送 API 回覆，或在 NetworkPolicy 允許時改走叢集內 `*.svc.cluster.local`。

### openai（Hermes、公司 LiteLLM、任何 /v1/chat/completions）
```
POST {endpoint_url}/chat/completions
Authorization: Bearer {api_key}
Accept: text/event-stream                 # 串流時
{
  "model": "{model}",            # 空白則不送；Hermes 會忽略
  "messages": [{"role": "system", "content": "<提示詞>\n\n<身分區塊>"}, ...],
  "user": "{session_id}",
  "metadata": {"session_id": "...", "odoo_user_id": "7", "odoo_email": "...", ...},   # 全部字串值、最多 16 鍵
  "stream": true                 # openai_stream 開時
}
```
- **messages 正規化**：一定以 user 開頭、以 user 結尾（Hermes 把最後一項當這次的輸入；Anthropic 類模型會把尾端 assistant 當預填續寫）。第一則 user 之前的 assistant（歡迎訊息）丟掉；最後一則 user 之後的 assistant（推送、拒絕、錯誤訊息）移到它前面；若因此變成 assistant 開頭（只有一則 user），把那幾則包成一則 user 前言「[Messages you (the assistant) sent before this user message]」。
- 歷史中的使用者附件：圖片（每張 ≤5 MB，**內嵌前用 `guess_mimetype(raw)` 再確認**是 image/png|jpeg|gif|webp，不信上傳時的 mimetype）只在**最近 3 則**使用者訊息內嵌為 `image_url` data URI，從最新的開始，**每次請求最多 4 張、data URI 總長約 7 MB**（Hermes 請求上限 10 MB；不然一則訊息傳 10 張，之後每次請求都會失敗直到滑出最近 3 則）。其餘附件、超出上限的圖片改成文字行 `[附件] 檔名（類型，大小）：簽名網址`。
- 回覆：串流（`text/event-stream`）時略過 `:` 開頭的行（keepalive）與 `event:` 不是預設 message 的事件（如 `hermes.tool.progress`），累積 `delta.content`、忽略 `reasoning_content`，讀到 `[DONE]` 結束；沒有 `[DONE]` 也沒有 `finish_reason` 就斷了 → invalid_response；chunk 帶 `error` → invalid_response。後端不理 `stream` 而回 JSON 也接受。非串流取 `choices[0].message.content`（字串，或 content parts 中的 text）。`usage` 記進呼叫紀錄（不送 `stream_options`，串流時只有後端自己附才有）。
- 不送 `tools`；工具全部由後端自己處理（Hermes／n8n 用 Odoo MCP）。Hermes 不讀 `user`、`metadata`，身分只能靠 system 區塊。

### n8n（webhook，同步回應）
```
POST {endpoint_url}
X-Woow-Timestamp: <unix>
X-Woow-Signature: sha256=<hex HMAC-SHA256(api_key, "<timestamp>.<raw body>")>
                                   # 不送 Authorization（P0：n8n Executions 會存 headers）
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

- 簽章是對**實際送出的原始 body 位元組**算的（Odoo 送出的就是簽的那份），n8n 要開 Webhook 的 Raw Body 驗原始位元組；時間窗 **300 秒**；驗簽失敗回非 200（Odoo 記 http_error 並貼出 error_message）。AI Agent 失敗也要回非 200，不要回 200 加道歉文字（呼叫紀錄才看得到失敗）。
- 「測試連線」送簽章過的 `{"event": "ping", "contact": {...}}`，回任何 200 都算成功；n8n 範例對非 message 事件回 `{"skip": true}`。
- 金鑰：`api_key`（Odoo→n8n 簽章）和推送金鑰（n8n→Odoo Bearer）是兩把不同的金鑰。

## 回覆呈現（兩種後端共用）

1. 文字當 markdown 子集轉成安全 HTML：段落、換行、粗體、斜體、刪除線、行內程式碼、程式碼區塊、標題（h3–h5）、清單（可巢狀）、引言與分隔線、連結（只允許 http/https/mailto，裸網址自動連結）、**GFM 表格**（轉 `<table class="table table-sm table-bordered">`）。先跳脫再組標籤，最後 `html_sanitize`。不用外部套件。先移除 `<think>…</think>`、`<thinking>…</thinking>`（含未閉合的開頭，以及只有結尾標籤的推理）與 `<|eot_id|>` 這類特殊 token（`<\|[a-z0-9_]{1,40}\|>`）。引言／分隔線輸出成有樣式的 `div`：`<blockquote>`、`<hr>` 會被 Odoo 標成郵件引文（`data-o-mail-quote`），Discuss 會把它摺疊起來。
   連結網域鉤子：`markdown_to_html(text, link_hosts=None)`；None＝維持現狀，有清單時只有主機在清單內的 http(s) 連結輸出 `<a>`，其餘只留文字。`discuss.channel._woow_ai_link_hosts(contact)` 預設 None（Live Chat 回傳官網自己的網域，防提示注入釣魚）。`_prepare_reply(reply, channel=None)`、`_post_text`、`_post_prepared` 都帶 channel；推送 API 在建好 chat 之前就整理回覆，走預設值。
2. markdown 圖片 `![alt](url)` 與看起來是檔案的連結（副檔名 png/jpg/jpeg/gif/webp/mp4/webm/mov/pdf/xlsx/xls/csv/docx/pptx/zip）：網域在白名單內 → 下載（https；純 http 只限白名單內的內網主機 localhost／`*.svc.cluster.local`；不跟隨轉址到白名單外、串流計量、超過 `max_download_mb` 就停）存成 `ir.attachment` 掛在回覆訊息上，文字中那段換成檔名；不在白名單 → 保留成連結。
   **data URI**（定案 24）：文字中的 `![alt](data:image/(png|jpeg|gif|webp);base64,…)` 與裸 `data:image/…` 不經網域白名單（不連網），解碼後受 `max_download_mb` 限制、用實際內容確認是那四種圖片，存成附件（檔名 `<alt>.png` 或 `image-N.png`）並從文字移除；被拒的或其他 `data:` 型別換成一句「（回覆中的一張圖片無法附加。）」並記在呼叫紀錄 error。Hermes 非串流時會把 ≤5 MB 的圖片換成 data URL。
   **MEDIA 標籤**：殘留的 `MEDIA:<path>`（Hermes 傳不出的檔案）從文字刪掉，換成「（AI 產生了檔案，但無法傳送。）」，呼叫紀錄 error 記一筆，伺服器路徑不外露。
   每則回覆最多 10 個檔案（以上各種合計）。
3. 附件 mimetype 以實際內容判斷（`odoo.tools.mimetypes.guess_mimetype`；沙盒映像有 python-magic），不信後端宣稱的類型。危險類型用**精確清單**改成 `application/octet-stream`：`text/html`、`application/xhtml+xml`、`image/svg+xml`、`text/xml`、`application/xml`、所有 `+xml` 結尾、javascript／ecmascript。不用子字串比對 `xml`（否則 xlsx／docx／pptx 的 OOXML 類型會被誤殺）。
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
  401 invalid_key；404 user_not_found；403 not_allowed；
  400 invalid_json／invalid_body／too_large／empty_message／invalid_recipient／ambiguous_recipient
```
- 找或建該使用者與 AI 的私訊頻道，以機器人身分張貼（同上呈現規則）。
- 金鑰以常數時間比對 sha256。不限速（負責人定案：不限用量）。
- 實作補充：`auth='none'` 的路由在 Odoo 18 預設用唯讀 cursor，所以明確設 `readonly=False`；資料庫必須能由網域判斷（單一資料庫或 dbfilter，PaaS 租戶即是）。收件人必須是有效使用者（只有 partner、沒有 user → 404）；`email` 對到多個使用者 → 400 `ambiguous_recipient`。推送也寫一筆呼叫紀錄（`backend_type = push`，不存內容）。推送不會觸發 AI 回覆。

## 入口

- **後台 Discuss 側欄**：不 patch 既有元件，而是在 Odoo 18 的側欄登錄表 `mail.discuss_sidebar_items` 加一個元件（sequence 25：在信箱 mailbox 20 之後、分類 channels 30 之前，也就是「頻道」區塊上方），只載入 `web.assets_backend`（公開頁不顯示）。列出目前使用者可用的 AI 聯絡人（`/woow_ai/contacts` JSON-RPC）；點了呼叫 `/woow_ai/open`（找或建私訊，第一次建立時張貼 `welcome_message`），回傳 `Store` 資料並在 Discuss 開啟。
- **portal**：不由核心自己做聊天頁（不採用 E2E 的建議）：Odoo 18 原生就有 `/discuss/channel/<id>`（`auth=public`，沙盒映像 `mail/controllers/discuss/public_page.py` 已確認）。`/my` 首頁卡片「AI 聯絡人」（portal 計數器 `woow_ai_contact_count`，沒有可用的 AI 聯絡人就不顯示）→ `/my/ai`（卡片清單）→ `/my/ai/<contact_id>/open` 找或建私訊後導到 `/discuss/channel/<id>`（Odoo 原生公開 Discuss 畫面，附件上傳／圖片預覽皆為 Odoo 原生）；有裝 `cs_portal_discuss` 時才加 `?discussions=1`（沒裝時 Odoo 會對未知參數記 warning）。內部用戶改導到後台 `/odoo/discuss?active_id=discuss.channel_<id>`。
- AI 私訊一律設 `allow_public_upload = True`：Odoo 的 `/mail/attachment/upload` 對非內部用戶只允許在有開這個旗標的頻道上傳（沙盒實測：沒開時 portal 上傳回 403「本頻道不允許上載附件」）。私訊只有兩個成員，開了不擴大存取範圍。
- 一般「開始對話」搜尋也找得到（partner 有效即可）；權限仍以 `allowed_group_ids` 在觸發時把關。

## 權限

- `woow.ai.contact`：系統管理員全權；其他人只能讀（record rule：自己在 `allowed_group_ids` 內），且敏感欄位都有 `groups='base.group_system'`。
- `woow.ai.job`、`woow.ai.call.log`：只有系統管理員。
- 私訊內容：只有成員看得到（Odoo 原生）；呼叫紀錄預設不存內容。
- 後端網址只有系統管理員能改；`endpoint_url` 必須是 https（`localhost`／叢集內完整寫出的 `*.svc.cluster.local` 例外，給 PaaS 內網用；短服務名不接受）。
- 「測試連線」按鈕只限系統管理員：openai `GET {endpoint}/models`（不送 chat completion，否則 Hermes 會真的跑一次 agent 和工具）、n8n 簽章過的 ping；畫面只顯示 HTTP 狀態和一句說明，不顯示上游回應內容。

## woow_ai_contact_livechat

實作（18.0.1.0.0）與操作細節：**docs/LIVECHAT.md**。以下是設計摘要。

- 欄位一律 `woow_ai_` 開頭（搬移期間 `im_livechat_ai` 1.1.0 同時安裝，它已有 `im_livechat.channel.ai_enabled`、`discuss.channel.ai_paused_until`，同名會共用資料欄而兩邊都回覆）：`im_livechat.channel.woow_ai_contact_id`、`woow_ai_enabled`、`woow_ai_pause_minutes`、`woow_ai_handoff_keywords`、`woow_ai_handoff_message`、搬移用的 `woow_ai_legacy_*`／`woow_ai_migration_state`；`discuss.channel.woow_ai_paused_until`。
- Live Chat 管理員可選**任一** AI 聯絡人（定案 25：**不加** `livechat_allowed` 開關；只給 Live Chat 用的聯絡人 `allowed_group_ids` 為空，橋接要給 Live Chat 管理員一條只讀 record rule 才選得到）；網址金鑰仍只有系統管理員看得到。後端要依 `channel.type == 'livechat'`／`user.is_guest` 只開放公開資料工具。
- 訪客訊息觸發 job；真人客服張貼 → 暫停 AI（預設 30 分鐘，可設）；訪客說「轉真人」等關鍵字 → handoff job 張貼固定訊息並通知客服（沿用 1.1.0 D 包）。
- 修 L1 實測的坑：預設關鍵字改成片語（「找真人」「轉人工」…，不再用單字「客服」「人工」「agent」）；暫停中訪客發訊息且最近 5 分鐘沒有客服發言 → 每次暫停張貼一次 `woow_ai_paused_message` 並再通知客服；客服 `/ai on`／`/ai off`／`/ai`（Live Chat 對話的指令）或對話表單按鈕立即恢復／暫停，恢復時回答沒人回的訪客訊息；暫停分鐘數一律檢查；Boolean 補 FALSE；搬移除了版本還要求舊金鑰欄已刪除。
- 身分語言只用已安裝的語言（guest 可能帶未安裝的語言碼，核心翻譯預設錯誤訊息時會丟 `Invalid language code`，工作卡在 running；沙盒實測發現）。
- 角色：訪客→user、AI→assistant、真人客服→assistant 並加「[真人客服 名字]」前綴（沿用 1.1.0）。
- 不提供 Odoo 端商品工具（負責人定案：工具全部交給後端）。上線前要在後端（Hermes／n8n）用 Odoo MCP 補上查價，並以 L1 實測的價格案例回歸。
- 使用核心鉤子：`_woow_ai_channel_contact`、`_woow_ai_check_author`、`_woow_ai_message_kind`、`_woow_ai_is_paused`、`_woow_ai_author_role`、`_woow_ai_requester`、`_woow_ai_identity`（guest 沒有 partner：放 guest 名稱、語言、時區）、`_woow_ai_system_prompt`（有真人客服訊息時加「同事回覆不要推翻」）、`_woow_ai_link_hosts`（官網網域）、`_woow_ai_channel_info`；`woow.ai.job._process_handoff`、`_notify_failure(reason, visitor_told)`。關掉 AI 或換聯絡人後還在排隊的工作，核心 `_skip_reply` 已會跳過。
- n8n：`channel` 物件 `{"id", "type": "livechat", "livechat": {"livechat_channel_id", "livechat_channel_name", "operator_name", "country_code", "visitor_kind"}}`；`user` 加 `is_guest`、`guest_id`、`visitor_key`（`guest:<id>`／`partner:<id>`／`public`）。session_id 是「這一段對話」，跨對話用 `visitor_key`。
- 搬移（分段，可回復；細節依 L1 的 `L1-findings.md` 與 `staging/livechat/BRIDGE-SPEC.md` e 節）：
  1. **準備**（`post_init_hook`／「重新準備」按鈕）：只讀 `im_livechat_ai` 18.0.1.1.0 以後的資料（SQL 讀，舊版的外洩金鑰欄永不讀），以 1.1.0 的系統 LiteLLM 設定建立 openai 型 AI 聯絡人（每組不同的模型＋提示詞各一個），頻道記下 `woow_ai_legacy_*`，狀態 `staged`，**不切換**（安裝不等於切換，避免商品工具立刻消失）。
  2. **切換**（每個頻道，系統管理員按，一筆交易）：先確認後端已做完價格回歸；關 1.1.0 的 `ai_enabled`、開 `woow_ai_enabled`，搬仍未到期的暫停與 30 分鐘內未跑的工作，狀態 `switched`；可切回。
  3. **完成**：全部頻道切換並觀察後 `finalized`；1.1.0 共用機器人與標籤的 xmlid 轉給橋接（noupdate），刪除 `im_livechat_ai.*` 系統參數（金鑰優先）。
  4. **解除安裝** `im_livechat_ai`（舊呼叫紀錄不搬，需要的話先匯出）。橋接自己解除安裝時，`uninstall_hook` 把舊機器人的 xmlid 移到 `__export__`。
  5. 所有租戶都解除安裝 1.1.0 後，平台映像才拿掉 `im_livechat_ai`。
- 共存保護：頻道的 1.1.0 `ai_enabled` 為 True（SQL 讀）時橋接不回覆；兩邊互相擋住「另一邊開著時打開」。

## 測試計畫（沙盒，不碰正式站）

- 單元：觸發條件、權限、markdown→HTML（含表格與惡意輸入）、白名單下載（轉址、超大、假 mimetype）、簽名網址（過期、竄改）、推送 API（401／403／404）、佇列（連發只回最新、重啟恢復、逾時）。
  審查後補：SSE 串流（keepalive、`hermes.tool.progress`、reasoning_content、跨 chunk 的多位元組字、空閒逾時、總時限、沒有 [DONE]）、重試範圍（429／503 重試，502／504／524／讀取逾時不重試、總時限內）、messages 正規化、內嵌張數與總量、假圖片不內嵌、身分／提示詞鉤子、data URI 與 MEDIA、`<thinking>`／特殊 token、連結網域鉤子、OOXML 類型、純 http 只限內網、測試連線、機器人 partner 封存規則、換聯絡人跳過、`visitor_told`。沙盒實測（sbx/live）：25 秒串流（每 2 秒 keepalive）、n8n 不帶 Authorization＋300 秒時間窗、測試連線。
- 整合：沙盒 ns 跑真的 Hermes／n8n？—— n8n 用沙盒內的假 webhook（簡單 HTTP 伺服器）＋正式 n8n 的測試 workflow；openai 型用 LiteLLM 測試金鑰。
- 瀏覽器：內部用戶側欄開 AI、portal 用戶 `/my/ai` 開對話、上傳圖片／PDF、收到表格與圖片附件。

## 實作補充與差異（18.0.1.0.0）

- **Bridge 掛勾**（給 `woow_ai_contact_livechat`）：`discuss.channel._woow_ai_channel_contact()`（這個頻道由哪個 AI 聯絡人回答；核心＝含機器人成員的 chat）、`_woow_ai_check_author()`（ok／denied／ignore）、`_woow_ai_message_kind()`（reply／handoff／None）、`_woow_ai_is_paused()`、`_woow_ai_author_role()`（歷史角色與前綴，例如「[真人客服 名字]」）、`_woow_ai_requester()`（身分要送誰）、`_woow_ai_identity(contact)`（身分 dict，預設 `contact._identity(requester)`）、`_woow_ai_system_prompt(contact, history)`（預設 `contact.system_prompt`）、`_woow_ai_link_hosts(contact)`（預設 None）、`_woow_ai_channel_info()`（n8n 的 `channel` 物件）。`_woow_ai_handle_new_message(message)` 的契約：回傳建立的 `woow.ai.job` 或 False；**先算 kind 再檢查暫停**（kind 鉤子看得到每一則允許的訊息，暫停中就不建 job；橋接要在 `super()` 回傳 handoff job 之後才暫停）。`woow.ai.job` 的 `_process_handoff()`、`_notify_failure(reason, visitor_told=True)` 也是掛勾（太舊放棄傳 `visitor_told=False`，`_give_up` 傳 True）；核心版 handoff 直接 skipped、失敗只記 log（1 對 1 私訊裡使用者已看到錯誤訊息，沒有客服要通知）。`_skip_reply` 與張貼前都檢查 `channel._woow_ai_channel_contact() == job.contact_id`，不同就 `skipped`「AI turned off or contact changed」。
- **佇列**：與 1.1.0 相同（postcommit 非 daemon thread、debounce 1.5 秒、每頻道 advisory lock、READ COMMITTED cursor、張貼前鎖頻道列再檢查一次、復原 cron 每 2 分鐘、每筆最多 3 次、30 分鐘放棄）。檔案下載在鎖頻道列**之前**做（不寫資料庫），鎖住後才建附件並張貼，避免長時間擋住使用者發訊。HTTP 呼叫期間沒有開著的交易（逾時秒數等設定在 commit 前先讀好）。
- **輸入中**：開始呼叫時 `True`、張貼前 `False`；另起一個 daemon thread 用自己的 cursor 每 20 秒重送 `True`。沙盒實測 45 秒呼叫：True 於 0／20／40 秒、結束送 False。
- **重試**：`max_retries`（預設 1）次，只對連線失敗（請求沒送出）、HTTP 429、503，3 秒、10 秒後，不超過總時限；讀取逾時與其他 HTTP 錯誤不重試（n8n 不保證冪等、Hermes 工具可能已執行）。
- **n8n 回應**寬鬆接受 `{"reply": "文字"}` 與 n8n 的 item 陣列 `[{...}]`；`replies` 最多 10 則。
- **openai `metadata`**：攤平成字串鍵、字串值（`odoo_user_id`、`odoo_email`…，最多 16 鍵、每值 512 字），符合 OpenAI 官方的格式要求；LiteLLM／Hermes 不會送給模型。OpenAI 官方 API 在沒有 `store` 時仍可能拒絕 `metadata`，直接接 OpenAI 官方時要注意（目前沒有開關）。
- **沒有做的**：Hermes header 會話模式（定案 23，維持定案 5 每次送最近 N 則）；`livechat_allowed` 開關（定案 25）；`openai_extra_params`（temperature 等參數交給後端／LiteLLM 設定）。
- **附件 mimetype**：`ir.attachment` 會把 `application/octet-stream` 依內容重新判斷（HTML 會變回 `text/html`），所以建立後再單獨寫一次 mimetype（不帶內容時不會重判）。
- **呼叫紀錄**：`backend_type` 多一個 `push`；每日 cron 同時清除 30 天前的呼叫紀錄與已結束的 job。關閉「記錄請求內容」時清除該 AI 聯絡人已存的內容；記錄時請求與回應（含 openai content 字串）裡的 data URI 與 base64 附件只留長度。串流回應記成 `{"stream": true, "content", "finish_reason", "usage"}`。
- **推送金鑰**：格式 `wpk_` 開頭；精靈不存檔，金鑰只在產生按鈕的回應 context 裡出現一次；畫面只留前 8 碼＋末 4 碼提示。
- **預設訊息**（無權限、錯誤）以收訊者語言翻譯（`zh_TW.po`）。
- **沒有** `cs_portal_discuss` 的映像（paas-odoo-official:20261030.1113 沒有這個模組）一樣可用：portal 用 Odoo 原生 `/discuss/channel/<id>` 開啟與發訊（HttpCase），上傳圖片／PDF 並收到 AI 回覆（沙盒對真的 Odoo 伺服器實測）。

## 負責人第二批定案（10-31，審查後 8 題）與落實

| # | 定案 | 核心落實 |
|---|---|---|
| 21 | Cloudflare 100 秒：openai 型預設串流（SSE、空閒逾時＋總時限）；n8n 長流程先回 skip 再用推送 API | `openai_stream` 預設 True；空閒 30 秒、總時限 `timeout_seconds`；PROTOCOL.md「Cloudflare」一節 |
| 22 | session_id 改 `woow-ai-<db8>-<channel_id>`（不用冒號） | `_session_id` |
| 23 | Hermes header 會話模式：不做 | 未實作，維持定案 5 |
| 24 | 回覆內 `data:image` 不經網域白名單直接存附件（仍受 `max_download_mb` 與實際類型檢查） | `_prepare_reply` |
| 25 | 不加「可用於 Live Chat」開關 | 未實作；Live Chat 管理員可選任一 AI 聯絡人 |
| 26 | 重試只對連線失敗、429、503 | `_http_post`、`max_retries` 0–2 預設 1 |
| 27 | 模型看得到的身分區塊不放 email（n8n JSON／metadata 照帶） | `_identity_system_text` |
| 28 | Hermes 實測用既有 Hermes 的專用測試對話、只做讀取類問答 | 實測線處理，核心無程式變更 |
