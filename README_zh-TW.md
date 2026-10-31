# Woow Odoo Discuss AI

Odoo 18 模組：讓內部用戶與 portal 用戶在 Discuss 裡和「AI 聯絡人」一對一私訊。AI 聯絡人背後接 OpenAI 相容後端（Hermes、LiteLLM、任何 `/v1/chat/completions`）或 n8n webhook。

[English](README.md)

| 模組 | 狀態 | 內容 |
|---|---|---|
| `woow_ai_contact` | 可用 | AI 聯絡人、後端連線、回覆佇列、雙向檔案、推送 API、Discuss 側欄、portal `/my/ai` |
| `woow_ai_contact_livechat` | 規劃中 | Live Chat 頻道交給 AI 聯絡人回答、轉真人（取代 `im_livechat_ai`） |

## 安裝

1. 把這個 repo（或 `woow_ai_contact` 資料夾）放進 addons path。
2. 安裝「AI Contacts for Discuss」（`woow_ai_contact`），只相依 `mail`、`portal`；內含繁體中文翻譯。

## 設定

設定 → AI 聯絡人 → AI 聯絡人 → 新增（限系統管理員）。

- **後端**：OpenAI 相容或 n8n webhook。
- **後端網址**：OpenAI 相容填以 `/v1` 結尾的基本網址；n8n 填完整 webhook 網址。必須是 https（`localhost`、`*.svc.cluster.local` 可用 http）。
- **API 金鑰**：只寫不讀，存檔後不再顯示。
- **模型**（選填）、**系統提示詞**（選填，留空交給後端）、**歷史訊息數**（預設 50）、**逾時**（預設 300 秒）。
- **招呼語**／**錯誤訊息**、**下載網域白名單**（每行一個，可用 `*.example.com`）與**檔案大小上限**（預設 20 MB）。
- **可使用的群組**：預設內部用戶＋portal。
- **推送 API**：按「產生推送金鑰」，金鑰只顯示一次（只存雜湊）。
- **除錯**：「記錄請求內容」平常請關閉。

### Hermes
後端網址 `https://<Hermes 主機>/v1`（或叢集內 `http://<service>.<namespace>.svc.cluster.local:<port>/v1`），API 金鑰填 Hermes API server 金鑰，模型留空用 Hermes 預設。工具與記憶由 Hermes 自己處理。

### LiteLLM
後端網址 `https://<LiteLLM 主機>/v1`，API 金鑰填 LiteLLM virtual key（建議限制可用模型），模型填 LiteLLM 模型名稱。

### n8n
建立「Webhook → AI Agent → Respond to Webhook」流程（見 [docs/PROTOCOL.md](docs/PROTOCOL.md#minimal-n8n-workflow)），Webhook 用 Header Auth（`Authorization: Bearer <密鑰>`），把正式 webhook 網址填進後端網址、同一個密鑰填進 API 金鑰。每次呼叫另有 `X-Woow-Signature` 簽章可驗證。

## 使用

- 後台：Discuss 側欄「AI 聯絡人」區塊，點一下就開啟（或建立）私訊。
- portal：`/my` 首頁「AI 聯絡人」卡片 → `/my/ai` → 開始對話。
- 可傳圖片與檔案；AI 回覆支援表格、清單、程式碼，白名單網域的圖片／檔案會存成附件。

## 協定

兩種後端的請求／回應格式、回覆呈現規則、推送 API、簽名下載網址：[docs/PROTOCOL.md](docs/PROTOCOL.md)。設計說明：[docs/DESIGN.md](docs/DESIGN.md)。

## 授權

LGPL-3.0，見 [LICENSE](LICENSE)。
