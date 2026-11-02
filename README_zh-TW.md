# Woow Odoo Discuss AI

Odoo 18 模組：讓內部用戶與 portal 用戶在 Discuss 裡和「AI 聯絡人」一對一私訊。AI 聯絡人背後接 OpenAI 相容後端（Hermes、LiteLLM、任何 `/v1/chat/completions`）或 n8n webhook。

[English](README.md)

| 模組 | 狀態 | 內容 |
|---|---|---|
| `woow_ai_contact` | 可用 | AI 聯絡人、後端連線、回覆佇列、雙向檔案、推送 API、像 OdooBot 一樣自動出現在聊天清單、portal `/my/ai` |
| `woow_ai_contact_livechat` | 可用（有 Live Chat 時自動安裝） | Live Chat 頻道交給 AI 聯絡人回答：轉真人、暫停、`/ai on` 立即恢復、從 `im_livechat_ai` 分段搬移（見 [docs/LIVECHAT.md](docs/LIVECHAT.md)） |
| `woow_ai_contact_portal_discuss` | 可用（有 [woow_odoo_portal_discuss](https://github.com/WOOWTECH/woow_odoo_portal_discuss) 時自動安裝） | portal 單一入口：AI 聯絡人變成 `/my/discussions` 的「AI 助理」分頁，`/my/ai` 轉到那裡 |

## 安裝

1. 把這個 repo（或 `woow_ai_contact` 資料夾）放進 addons path。
2. 安裝「AI Contacts for Discuss」（`woow_ai_contact`），只相依 `mail`、`portal`；內含繁體中文翻譯。

## 設定

討論 → 配置 → AI 聯絡人 → 新增（限系統管理員；同一個選單下還有「回覆工作」「呼叫紀錄」）。

- **後端**：OpenAI 相容或 n8n webhook。
- **後端網址**：OpenAI 相容填以 `/v1` 結尾的基本網址；n8n 填完整的正式 webhook 網址（Chat Trigger 是 `…/webhook/<id>/chat`）。必須是 https（`localhost`、`*.svc.cluster.local` 可用 http）。
- **API 金鑰／簽章金鑰**：只寫不讀，存檔後不再顯示。OpenAI 相容（必填）：Bearer 金鑰；n8n（選填）：HMAC 簽章金鑰（不會送出），留空＝請求不簽章，例如公開的 n8n Chat Trigger。
- **模型**（選填）、**系統提示詞**（選填，留空交給後端）、**歷史訊息數**（預設 50）、**逾時**（整個回覆，預設 300 秒；n8n 每次最多 90 秒）。
- **串流回覆**（OpenAI 相容，預設開）：SSE 串流，連續 30 秒沒有資料才算失敗，避免 Cloudflare 約 100 秒的中斷。
- **重試次數**（0–2，預設 1）：只對連線失敗、HTTP 429、503；讀取逾時與 502／504／524 不重送（後端可能已在執行工具）。
- **測試連線**按鈕：OpenAI 相容送 `GET /models`；n8n 送 `{"event": "ping", ..., "action": "loadPreviousSession", "sessionId": "woow-ai-ping-<id>"}`（有密鑰才簽章），Chat Trigger 不會因此執行 agent。只顯示 HTTP 狀態，不會真的跑 AI。
- **使用者傳送的文件**（OpenAI 相容，「檔案」分頁）：`把檔案送給模型`（預設，LiteLLM／OpenAI：PDF、Word、Excel、csv 等原檔以 file 送出，Odoo 不轉換）｜`只給下載連結`（Hermes 用：它對 file 一律回 HTTP 400，agent 可以自己抓連結）。圖片兩種都內嵌。上限見 [docs/PROTOCOL.md](docs/PROTOCOL.md#documents)。
- **招呼語**／**錯誤訊息**、**下載網域白名單**（每行一個，可用 `*.example.com`）與**檔案大小上限**（預設 20 MB）。
- **可使用的群組**：預設只有內部用戶；留空（且沒有分享給任何人）＝不能私訊（只給 Live Chat 用），機器人聯絡人保持封存，不會自動建私訊、也搜尋不到。加入 Portal 群組＝所有 portal 用戶都能用，表單會顯示警告。
- **分享給**（存取分頁）：逐一挑選可以使用的使用者，通常是 portal 客戶。加入後對方立刻有這個私訊（附招呼語、算未讀），在後台討論或 portal 討論區直接看得到；移除後（且不在允許的群組裡）私訊從對方清單移除（保留紀錄），對方再傳訊息 AI 只回「您沒有使用這個 AI 聯絡人的權限」、不呼叫後端，portal 也不再列出。封存 AI 聯絡人不算取消分享（對話保留、回封存通知）。
- **推送 API**：按「產生推送金鑰」，金鑰只顯示一次（只存雜湊）。
- **除錯**：「記錄請求內容」平常請關閉。

### 伺服器需求（回覆在哪裡處理）
網頁請求只存訊息並喚醒排程工作；排程動作「AI 聯絡人：執行 AI 回覆」在 **cron 程序**裡等後端回覆、張貼後才結束，網頁程序回收不會再切斷長回覆。AI 聯絡人表單（後端分頁「回覆處理方式」）會顯示目前的模式。

- **排程模式**（正常）：要有 cron（本伺服器 `max_cron_threads` ≥ 1，或同一資料庫的 cron 在別的程序跑），而且 cron 時限夠長：`limit_time_real_cron`（設 `-1` 時看 `limit_time_real`）≥ 最長逾時＋60 秒，**`workers` > 0 時再加 70 秒**（prefork 的 cron worker 閒置等待也算在看門狗時間內）。預設逾時 300 秒 → `limit_time_real_cron = 430` 以上（例如 600）。cron 在別的程序跑時（網頁伺服器 `max_cron_threads = 0`），模式依 cron 程序回報的伺服器型態與時限判斷，不看網頁伺服器自己的設定。
- 一趟處理期間會占住那個 cron worker：同一個 worker 的其他排程（以及它服務的其他資料庫）要等。`max_cron_threads = 1`、或一個 cron 程序服務多個資料庫時，每趟最多只花 60 秒接新的回覆（表單會顯示警告）；建議 `max_cron_threads` ≥ 2，並且每個有 AI 聯絡人的資料庫各有自己的 cron 程序。cron 程序的 `db_maxconn` 要 ≥ 同時處理數＋5。
- **備援（執行緒）模式**：沒有 cron，或 cron 時限不夠時，回覆在收到訊息的網頁程序背景執行緒處理（18.0.1.0.0 的做法）：長回覆途中該網頁程序重啟，回覆會被切斷並重跑。會記一次警告並顯示在表單上。
- 系統參數：`woow_ai_contact.job_runner` = `auto`（預設）｜`cron`｜`thread`；`woow_ai_contact.max_concurrency`（預設 4，最多 16；另受 cron 程序可用的資料庫連線數限制）。

### Hermes
後端網址 `https://<Hermes 主機>/v1`（或叢集內完整的 `http://<service>.<namespace>.svc.cluster.local:<port>/v1`，短服務名不接受），API 金鑰填 Hermes API server 金鑰，模型留空（Hermes 會忽略，用自己的設定），串流保持開啟。工具與記憶由 Hermes 自己處理。Hermes 不讀 `user`／`metadata`，使用者身分（姓名、語言、時區、內部或 portal，不含 email）放在 system 訊息裡讓模型看得到。

### LiteLLM
後端網址 `https://<LiteLLM 主機>/v1`，API 金鑰填 LiteLLM virtual key（建議限制可用模型），模型填 LiteLLM 模型名稱。

### n8n
n8n 的請求與回覆格式和 `im_livechat_ai` 18.0.1.2.0 相同，同一個工作流程兩個模組都能用。

**既有的 Chat Trigger（託管聊天）工作流程**（公開聊天，例如智居顧問）：後端網址填 `https://<n8n>/webhook/<webhookId>/chat`、簽章金鑰留空，按「測試連線」（HTTP 200）即可。Odoo 會帶 Chat Trigger 讀的 `action`、`sessionId`、`chatInput`，agent 回的 `{"output": "..."}` 發成一則訊息（markdown 連結與粗體會顯示）。限制：這種流程用 `sessionId` 自己記憶、不看 Odoo 送的歷史、提示詞與事件（轉真人期間客服說的話它看不到），也無法驗證請求來自 Odoo。使用者的圖片／檔案在 `message.attachments`（1 小時簽名網址），流程要用的話自己加 HTTP Request 節點下載。細節見 [docs/PROTOCOL.md](docs/PROTOCOL.md#n8n-chat-trigger-hosted-chat-workflows)。

**新建工作流程**：建立「Webhook（開 Raw Body）→ Code（驗簽）→ AI Agent → Respond to Webhook」流程（見 [docs/PROTOCOL.md](docs/PROTOCOL.md#minimal-n8n-workflow)），把正式 webhook 網址填進後端網址、隨機產生的密鑰填進「簽章金鑰」。Odoo 不送 `Authorization`；有密鑰時只送 `X-Woow-Timestamp`＋`X-Woow-Signature`（對實際送出的 body 位元組算 HMAC-SHA256，時間窗 300 秒）；驗簽失敗請回非 200。經 Cloudflare 對外網址超過約 100 秒會 524：長流程請先回 `{"skip": true}`，再用推送 API 回覆。n8n 的 Executions 會存 webhook 內容（含對話），請設保留期限。

## 使用

- 像 OdooBot：使用者第一次打開後台（討論／訊息選單，手機版也一樣）時，自動建好與每個他可用的 AI 聯絡人的私訊、置頂在聊天清單，附招呼語（算未讀，訊息圖示有數字）。之後新增或新開放的 AI 聯絡人，下次打開時補上。
- 每人每個 AI 聯絡人只自動建一次：取消置頂／關掉後不會自己回來；AI 主動推送或使用者自己再開（「開始對話」搜尋、portal）時才會再出現。
- 「開始對話」搜尋得到 AI 聯絡人（只列出自己有權限用的）；狀態顯示「機器人」（綠色愛心），跟 OdooBot 一樣。
- 回覆在背景由 cron 程序處理：同一段對話一次一則（連發只回最新、帶完整上下文），最多 4 段對話同時進行，處理中顯示「輸入中」，預設逾時 5 分鐘，重啟不掉單。
- portal：第一次進任何 portal 頁面（`/my`、`/my/ai`，或 woow_odoo_portal_discuss 這類 portal 討論模組的對話清單）就自動建好私訊（同樣只建一次）；`/my` 首頁「AI 聯絡人」卡片 → `/my/ai` → 開始對話。有裝 woow_odoo_portal_discuss 時，自動安裝的 `woow_ai_contact_portal_discuss` 把兩者合成單一入口：首頁只留「討論區」卡片，`/my/discussions` 上方分頁「全部｜AI 助理｜私訊｜群組｜頻道」（`?filterby=ai|chat|group|channel`，沒內容的分頁不顯示）。AI 助理分頁列出與 AI 的私訊（私訊分頁不再列 AI），並列出還沒開對話的 AI 助理（「其他 AI 助理」）；`/my/ai` 轉到 AI 助理分頁，聊天頁的返回列回到進來時的分頁。
- AI 私訊不顯示「開始通話」「開始視訊通話」（AI 不會接），跟 OdooBot 一樣；人跟人的通話不受影響。
- 可傳圖片與檔案（OpenAI 相容後端每次最多內嵌 4 張、約 7 MB；PDF、xlsx、xls、docx、csv、txt、md、json 等文件依「使用者傳送的文件」設定：預設原檔直接送給模型（Odoo 不轉換），Hermes 請選「只給下載連結」；其餘給 1 小時簽名網址，`web.base.url` 必須是 Odoo 的對外網址）；AI 回覆支援表格、清單、程式碼，白名單網域的圖片／檔案、n8n base64 檔案、回覆中的 `data:image` 內嵌圖都會存成附件。

## 協定

兩種後端的請求／回應格式、回覆呈現規則、推送 API、簽名下載網址：[docs/PROTOCOL.md](docs/PROTOCOL.md)。設計說明：[docs/DESIGN.md](docs/DESIGN.md)。

## 授權

LGPL-3.0，見 [LICENSE](LICENSE)。
