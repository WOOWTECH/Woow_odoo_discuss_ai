# Woow Odoo Discuss AI

Odoo 18 模組：讓內部用戶與 portal 用戶在 Discuss 裡和「AI 聯絡人」一對一私訊。AI 聯絡人背後接 OpenAI 相容後端（Hermes、LiteLLM、任何 `/v1/chat/completions`）或 n8n webhook。

[English](README.md)

| 模組 | 狀態 | 內容 |
|---|---|---|
| `woow_ai_contact` | 可用 | AI 聯絡人、後端連線、回覆佇列、雙向檔案、推送 API、Discuss 側欄、portal `/my/ai` |
| `woow_ai_contact_livechat` | 可用（有 Live Chat 時自動安裝） | Live Chat 頻道交給 AI 聯絡人回答：轉真人、暫停、`/ai on` 立即恢復、從 `im_livechat_ai` 分段搬移（見 [docs/LIVECHAT.md](docs/LIVECHAT.md)） |

## 安裝

1. 把這個 repo（或 `woow_ai_contact` 資料夾）放進 addons path。
2. 安裝「AI Contacts for Discuss」（`woow_ai_contact`），只相依 `mail`、`portal`；內含繁體中文翻譯。

## 設定

設定 → AI 聯絡人 → AI 聯絡人 → 新增（限系統管理員）。

- **後端**：OpenAI 相容或 n8n webhook。
- **後端網址**：OpenAI 相容填以 `/v1` 結尾的基本網址；n8n 填完整 webhook 網址。必須是 https（`localhost`、`*.svc.cluster.local` 可用 http）。
- **API 金鑰／簽章金鑰**：只寫不讀，存檔後不再顯示。OpenAI 相容：Bearer 金鑰；n8n：HMAC 簽章金鑰（不會送出）。
- **模型**（選填）、**系統提示詞**（選填，留空交給後端）、**歷史訊息數**（預設 50）、**逾時**（整個回覆，預設 300 秒）。
- **串流回覆**（OpenAI 相容，預設開）：SSE 串流，連續 30 秒沒有資料才算失敗，避免 Cloudflare 約 100 秒的中斷。
- **重試次數**（0–2，預設 1）：只對連線失敗、HTTP 429、503；讀取逾時與 502／504／524 不重送（後端可能已在執行工具）。
- **測試連線**按鈕：OpenAI 相容送 `GET /models`、n8n 送簽章過的 `ping`，只顯示 HTTP 狀態，不會真的跑 AI。
- **招呼語**／**錯誤訊息**、**下載網域白名單**（每行一個，可用 `*.example.com`）與**檔案大小上限**（預設 20 MB）。
- **可使用的群組**：預設內部用戶＋portal；留空＝不能私訊（只給 Live Chat 用），機器人聯絡人保持封存。
- **推送 API**：按「產生推送金鑰」，金鑰只顯示一次（只存雜湊）。
- **除錯**：「記錄請求內容」平常請關閉。

### 伺服器需求（回覆在哪裡處理）
網頁請求只存訊息並喚醒排程工作；排程動作「AI 聯絡人：執行 AI 回覆」在 **cron 程序**裡等後端回覆、張貼後才結束，網頁程序回收不會再切斷長回覆。AI 聯絡人表單（後端分頁「回覆處理方式」）會顯示目前的模式。

- **排程模式**（正常）：要有 cron（本伺服器 `max_cron_threads` ≥ 1，或同一資料庫的 cron 在別的程序跑），而且 cron 時限夠長：`limit_time_real_cron`（設 `-1` 時看 `limit_time_real`）≥ 最長逾時＋60 秒，**`workers` > 0 時再加 70 秒**（prefork 的 cron worker 閒置等待也算在看門狗時間內）。預設逾時 300 秒 → `limit_time_real_cron = 430` 以上（例如 600）。`max_cron_threads = 1` 時回覆進行中其他排程要等，建議 2 以上。
- **備援（執行緒）模式**：沒有 cron，或 cron 時限不夠時，回覆在收到訊息的網頁程序背景執行緒處理（18.0.1.0.0 的做法）：長回覆途中該網頁程序重啟，回覆會被切斷並重跑。會記一次警告並顯示在表單上。
- 系統參數：`woow_ai_contact.job_runner` = `auto`（預設）｜`cron`｜`thread`；`woow_ai_contact.max_concurrency`（預設 4，最多 16；另受 cron 程序可用的資料庫連線數限制）。

### Hermes
後端網址 `https://<Hermes 主機>/v1`（或叢集內完整的 `http://<service>.<namespace>.svc.cluster.local:<port>/v1`，短服務名不接受），API 金鑰填 Hermes API server 金鑰，模型留空（Hermes 會忽略，用自己的設定），串流保持開啟。工具與記憶由 Hermes 自己處理。Hermes 不讀 `user`／`metadata`，使用者身分（姓名、語言、時區、內部或 portal，不含 email）放在 system 訊息裡讓模型看得到。

### LiteLLM
後端網址 `https://<LiteLLM 主機>/v1`，API 金鑰填 LiteLLM virtual key（建議限制可用模型），模型填 LiteLLM 模型名稱。

### n8n
建立「Webhook（開 Raw Body）→ Code（驗簽）→ AI Agent → Respond to Webhook」流程（見 [docs/PROTOCOL.md](docs/PROTOCOL.md#minimal-n8n-workflow)），把正式 webhook 網址填進後端網址、隨機產生的密鑰填進「簽章金鑰」。Odoo 不送 `Authorization`，只送 `X-Woow-Timestamp`＋`X-Woow-Signature`（對實際送出的 body 位元組算 HMAC-SHA256，時間窗 300 秒）；驗簽失敗請回非 200。經 Cloudflare 對外網址超過約 100 秒會 524：長流程請先回 `{"skip": true}`，再用推送 API 回覆。n8n 的 Executions 會存 webhook 內容（含對話），請設保留期限。

## 使用

- 後台：Discuss 側欄「AI 聯絡人」區塊，點一下就開啟（或建立）私訊。
- 回覆在背景由 cron 程序處理：同一段對話一次一則（連發只回最新、帶完整上下文），最多 4 段對話同時進行，處理中顯示「輸入中」，預設逾時 5 分鐘，重啟不掉單。
- portal：`/my` 首頁「AI 聯絡人」卡片 → `/my/ai` → 開始對話。
- 可傳圖片與檔案（OpenAI 相容後端每次最多內嵌 4 張、約 7 MB，其餘給 1 小時簽名網址）；AI 回覆支援表格、清單、程式碼，白名單網域的圖片／檔案、n8n base64 檔案、回覆中的 `data:image` 內嵌圖都會存成附件。

## 協定

兩種後端的請求／回應格式、回覆呈現規則、推送 API、簽名下載網址：[docs/PROTOCOL.md](docs/PROTOCOL.md)。設計說明：[docs/DESIGN.md](docs/DESIGN.md)。

## 授權

LGPL-3.0，見 [LICENSE](LICENSE)。
