# woow_ai_contact_livechat：Live Chat 橋接

`woow_ai_contact_livechat`（`depends: woow_ai_contact, im_livechat`，`auto_install: True`）讓 Live Chat 頻道「選一個 AI 聯絡人」來回覆訪客，並取代 `im_livechat_ai` 1.1.0。
重現 1.1.0 D 包的轉真人／暫停／關鍵字轉真人，修掉 L1 沙盒實測量到的坑（見「與 1.1.0 的差異」）。商品查詢工具不保留（負責人定案 10）：查價交給後端（Hermes／n8n＋Odoo MCP）。

## 設定

Live Chat ＞ 設定 ＞ 頻道 ＞「AI 聯絡人」分頁（Live Chat 管理員）：

| 欄位 | 預設 | 說明 |
|---|---|---|
| `woow_ai_enabled` AI 回覆 | 關 | 開啟時必須選 AI 聯絡人，且聯絡人有效 |
| `woow_ai_contact_id` AI 聯絡人 | — | 可選**任一**有效的 AI 聯絡人（定案 25，沒有「可用於 Live Chat」開關）。Live Chat 管理員只看得到名稱；網址、金鑰、提示詞只有系統管理員看得到 |
| `woow_ai_pause_minutes` 真人回覆後 AI 暫停（分鐘） | 30 | 1–1440，不論 AI 開關都檢查 |
| `woow_ai_handoff_keywords` 轉真人關鍵詞 | `找真人,真人客服,轉真人,真人服務,轉人工,人工客服,找客服,專人服務,talk to a human,human agent,real person,live agent` | 逗號／全形逗號／頓號／換行分隔；英文整字比對、不分大小寫；空白＝不做關鍵字轉真人 |
| `woow_ai_handoff_message` 轉真人訊息 | `已幫您通知客服，請稍候。` | |
| `woow_ai_paused_message` AI 暫停提示 | `真人客服會盡快回覆您，請稍候。在那之前 AI 助理暫停回覆。` | 空白＝不提示 |

只給 Live Chat 用的 AI 聯絡人，`allowed_group_ids` 留空：不能私訊、機器人 partner 保持封存。橋接加了一條 record rule，讓 Live Chat 管理員讀得到所有 AI 聯絡人（只讀）。
AI 聯絡人表單的「存取」分頁會列出用到它的 Live Chat 頻道。

**後端要注意**：Live Chat 訪客多半是匿名的。`channel.type == "livechat"` 或 `user.is_guest == true` 時，後端只能開放公開資料工具（商品、價格、營業資訊），不能查客戶、訂單、員工。Odoo 只負責送正確的旗標，權限把關在後端。`guest_id` 不是可信身分（cookie 可清除）。

## 誰說的話

| 作者 | 分類 | AI |
|---|---|---|
| 所有 AI 聯絡人的 partner、本頻道舊機器人（`woow_ai_legacy_bot_partner_id`）、1.1.0 共用機器人、標籤「AI 機器人」的 partner、仍安裝時 1.1.0 各頻道機器人、OdooBot | 機器人 | 不回、不暫停 |
| guest（`author_guest_id`）、沒有作者 | 訪客 | 回覆 |
| 有內部使用者（含封存的）且是指派客服或不是本對話的訪客成員 | 客服 | 不回；**comment 會暫停** |
| 有內部使用者、且是本對話的訪客成員（員工在官網上試用） | 訪客 | 回覆 |
| 指派客服但沒有內部使用者（Odoo chatbot） | 機器人 | 不回 |
| portal 使用者、public 使用者、沒有使用者的 partner | 訪客 | 回覆 |

訪客成員＝和對話同時建立、不是指派客服也不是機器人的第一個成員；之後邀請進來的客服不算。只有 comment 會觸發，評分、通知不會。頻道沒開 AI 時，客服發言也不暫停。

## 轉真人、暫停、提示、恢復

1. **客服發言**（comment）→ 這段對話暫停 `woow_ai_pause_minutes` 分鐘。只延長、不縮短；只影響這段對話；到期自動恢復（恢復後下一則訪客訊息的上下文會包含暫停期間沒回的問題）。只「加入」對話不會暫停。
2. **訪客說轉真人關鍵詞** → 建 handoff 工作並暫停：AI 張貼轉真人訊息（不呼叫後端，後端沒設定也能轉），客服收到 OdooBot 私訊（離線也看得到）＋黏著通知（在線者），內容含對話連結與「輸入 /ai on 可立即恢復 AI」。暫停中再說關鍵字不會再發。handoff 會取代較早的 reply 工作，補救排程不會重發。
3. **暫停中訪客又發訊息**（新增）→ 若最近 5 分鐘內沒有客服發言（客服不在線上聊），AI 張貼一次 `woow_ai_paused_message`，並再通知客服「訪客在等待回覆」。每次暫停最多一次；轉真人訊息本身就算提示。客服正在聊時不插話。
4. **立即恢復**（新增）→ 客服在對話中輸入 `/ai on`（`/ai off` 暫停、`/ai` 看狀態；只在 Live Chat 對話出現），或在「Live Chat ＞ 對話紀錄」的對話表單按「立即恢復 AI」。恢復時，如果最後一則真人訊息是訪客的（沒人回），馬上排一個回覆工作。只有內部使用者（Live Chat 客服或對話成員）能用。
5. 工作在呼叫後端前、張貼前（鎖住對話列之後）都再檢查暫停、是否換了聯絡人或關了 AI；後端回答途中客服插話，答案丟掉。

## 送給後端的內容

- 歷史角色：訪客→`user`；AI 與其他機器人→`assistant`；客服→`assistant` 並加前綴 `[真人客服 名字] `（名字用 Live Chat 使用者名稱，沒有才用 partner 名）。有客服訊息時系統提示後面加「同事回覆不要推翻」那段（1.1.0 原文）。
- `session_id` 是「這一段對話」（`woow-ai-<db8>-<channel_id>`）；跨對話認同一個訪客用 `user.visitor_key`。
- `user`（n8n）／`metadata`（openai）：

| 訪客 | user_id | partner_id | guest_id | name | email | is_portal | is_guest | visitor_key |
|---|---|---|---|---|---|---|---|---|
| guest | null | null | guest id | guest 名稱 | "" | false | true | `guest:<id>` |
| portal 使用者 | id | id | null | partner 名 | email | true | false | `partner:<id>` |
| 員工以訪客身分 | id | id | null | 同上 | 同上 | false | false | `partner:<id>` |
| 沒有使用者的 partner | null | id | null | partner 名 | email | false | false | `partner:<id>` |
| public 使用者 | null | null | null | "" | "" | false | true | `public` |

  `lang` 依序取訪客的語言、網站對話的語言、指派客服的語言，只用 Odoo 已安裝的語言（未安裝的語言碼會讓預設錯誤訊息翻譯失敗，沙盒實測發現）。模型看得到的身分區塊不含 email（定案 27）。**不送** IP、瀏覽頁面、`website.visitor` 追蹤資料、電話、地址。
- n8n 的 `channel`：

```json
{"id": 42, "type": "livechat",
 "livechat": {"livechat_channel_id": 3, "livechat_channel_name": "官網", "operator_name": "客服小明",
              "country_code": "TW", "visitor_kind": "guest|portal|employee|partner|public"}}
```

- 訪客附件照核心：openai 內嵌最近幾則的圖片、其他給 1 小時簽名網址；n8n 每個附件都有簽名網址。頻道關掉 AI 後簽名網址就失效。
- AI 回覆中的連結：只有 `web.base.url` 與各網站 `domain` 的主機會變成可點連結（核心 `_woow_ai_link_hosts` 鉤子），其他連結只留文字，防提示注入釣魚。
- 回覆失敗：訪客收到聯絡人的錯誤訊息，客服收到「AI 客服無法回覆…已向訪客送出錯誤訊息，請盡快接手」；工作太舊放棄時（沒告訴訪客）只說「請盡快接手」。

## 與 1.1.0 的差異（L1 實測的坑與處理）

| L1 觀察 | 處理 |
|---|---|
| 預設關鍵字有單字「客服」「人工」「agent」「operator」，「客服電話幾號」也轉真人、靜音 30 分鐘 | 預設改成片語（上表）。搬移時，頻道若還是 1.1.0 的預設值就換成新預設並寫進搬移報告；自訂值原樣保留 |
| 暫停期間完全沒有提示，沒人接手時訪客可能等 30 分鐘 | 暫停提示（每次暫停一次、客服在聊時不插話）＋再通知客服 |
| 沒有「立刻恢復 AI」 | `/ai on`、`/ai off`、對話表單按鈕；恢復時回答沒人回的訪客訊息 |
| 範圍檢查只在 AI 開啟且欄位被寫入時才跑 | 暫停分鐘數一律檢查 1–1440，執行時也夾住 |
| 新增 Boolean 欄位全新安裝後是 NULL | `post_init_hook` 補 FALSE |
| 遷移只靠版本號 | 搬移同時要求版本 ≥ 18.0.1.1.0 **且** 舊金鑰欄 `ai_api_key` 已不存在 |
| widget 標題是真人客服名字、模型會自稱別的身分 | 未處理（Odoo 原生 widget）；建議在 AI 聯絡人的提示詞固定身分與名稱、機器人放頭像 |
| 沒有客服在線時 widget 不出現 | 未處理（Odoo 核心行為，AI 不改變；要下班時間服務需另外設計） |
| 恢復延遲 60–98 秒、只靠 cron | 核心佇列相同（重啟後由 cron 補回），cron 程序要有監控 |

另外：
- 商品工具、`temperature`、`max_tokens`、模型下拉、每頻道機器人名稱／錯誤訊息都不在頻道上了（跟著 AI 聯絡人）；Live Chat 管理員不能改模型與提示詞（權限變嚴，刻意）。
- 重試照核心（定案 26）：只有連線失敗、429、503，讀取逾時與 502／504／524 不重送。搬移過來的聯絡人 `timeout_seconds` 120 秒、串流開著（Cloudflare 100 秒）。
- 沒有「輸入中」：機器人不是 Live Chat 對話的成員（與 1.1.0 相同）。
- 交接給 Odoo chatbot 的頻道：chatbot 腳本進行中訪客的回答也會觸發 AI（與 1.1.0 相同）；同時用 chatbot 與 AI 的頻道要自己評估。

## 與 im_livechat_ai 共存（搬移期間兩個都裝著）

- 欄位一律 `woow_ai_` 開頭，不和 1.1.0 的 `ai_enabled`、`ai_paused_until` 共用欄位。
- **一段對話絕不會被回兩次**：頻道的 1.1.0 `ai_enabled` 為 True（SQL 讀已提交值）時，橋接不回覆、不暫停、簽名網址也失效；在這種頻道開 `woow_ai_enabled` 會被擋（ValidationError），反過來在 AI 聯絡人回覆中的頻道開 1.1.0 也會被擋。
- 1.1.0 解除安裝後欄位消失，保護自動失效，不需改程式。

## 從 im_livechat_ai 1.1.0 搬移（分段、可回復）

| 步驟 | 動作 | 誰 | 可回復 |
|---|---|---|---|
| 0 | 平台映像同時帶 `im_livechat_ai` 18.0.1.1.0 與兩個新模組 | 平台發版 | — |
| 1 | 租戶升級 `im_livechat_ai` 到 18.0.1.1.0 | 平台 | — |
| 2 | 安裝 `woow_ai_contact` → 橋接自動安裝 → **準備** | 系統管理員 | 是（解除安裝橋接；1.1.0 不受影響） |
| 3 | 在 AI 聯絡人填好後端（建議 Hermes／n8n＋Odoo MCP 查價），以訪客身分跑**價格回歸** | 系統管理員＋後端負責人 | 是 |
| 4 | 逐頻道**切換**（先內部帳號試用的頻道） | 系統管理員 | 是（「切回 im_livechat_ai」） |
| 5 | 觀察至少一週：呼叫紀錄、客服通知、價格抽查 | 負責人 | 是 |
| 6 | **完成搬移** | 系統管理員 | 部分（1.1.0 的系統參數含金鑰已刪，要回 1.1.0 需重填） |
| 7 | 解除安裝 `im_livechat_ai` | 系統管理員 | 否（1.1.0 的工作與呼叫紀錄表會刪除，要留先匯出） |
| 8 | **所有租戶**都完成 7 後，平台映像才拿掉 `im_livechat_ai` | 平台發版 | — |

第 8 步：映像先拿掉而資料庫還裝著，該租戶模組會變成無法載入、升級檢查會失敗。平台要盤點「哪些租戶還裝著 im_livechat_ai」。

### 準備（安裝時自動；頻道表單「重新準備」）

- 條件：`im_livechat_ai` 已安裝、版本 ≥ 18.0.1.1.0、舊的每頻道金鑰欄 `ai_api_key` 已不存在（18.0.1.0.x 外洩過的金鑰永不讀）。不符合只記 warning，不做事。1.1.0 的資料一律用 SQL 讀，不依賴它的程式。
- 要搬的頻道：1.1.0 `ai_enabled` 為 True、或有提示詞、或有機器人 partner（1.1.0 升級時把所有頻道的 AI 關掉，只看開關會漏）。已經有 AI 聯絡人的頻道跳過（可重跑）。
- 依 `(ai_model, 提示詞, 機器人名稱, 錯誤訊息, ai_max_history, ai_max_retries)` 分組，每組建一個 openai 型 AI 聯絡人：網址＝`im_livechat_ai.base_url`（去掉結尾 `/` 與 `/chat/completions`，不是 https 就留空）、金鑰＝`im_livechat_ai.api_key`（只寫進 SQL 欄位、不記錄）、模型、提示詞＋1.1.0「沒工具」商品規則原文（換成能查價的後端後由管理員刪掉）、`max_history`、`max_retries`、錯誤訊息、逾時 120 秒、串流、`allowed_group_ids` 空、不記內容。組內第一個頻道的機器人 partner（沒有使用者、不是共用機器人、沒被別的聯絡人用）直接沿用，訪客看到的名字與頭像不變。
- 頻道寫入：AI 聯絡人、`woow_ai_enabled = False`、暫停分鐘數、關鍵字（1.1.0 預設值換新預設）、轉真人訊息、`woow_ai_legacy_bot_partner_id`、`woow_ai_legacy_enabled`、狀態 `staged`、搬移報告（是否靠商品工具、遺失的 temperature／max tokens、待填的網址或金鑰）。
- **1.1.0 完全不動、繼續回覆**。安裝不等於切換：切換前商品工具仍在。

### 切換（每個頻道，系統管理員，一筆交易）

頻道表單「從 im_livechat_ai 切換…」→ 精靈要勾「新後端已通過價格回歸測試」才有「切換」按鈕。前置：狀態 `staged`、AI 聯絡人有效且網址與金鑰都已填。

1. SQL 關掉這個頻道的 1.1.0 `ai_enabled`。
2. `woow_ai_enabled` ＝ 1.1.0 原本的開關（原本關著的頻道切過去仍是關的）。狀態 `switched`。
3. 仍未到期的 1.1.0 暫停搬到 `woow_ai_paused_until`（取較晚者）。
4. 1.1.0 還沒跑過（pending、0 次、30 分鐘內）的工作：每段對話先 `pg_try_advisory_xact_lock` 1.1.0 的鎖（`crc32('im_livechat_ai.job')`、對話 id）；拿到鎖就建同 kind、同訊息的 `woow.ai.job`，舊工作標 `skipped`；拿不到鎖表示 1.1.0 正在回這段對話，讓它回完（只會回一次），不搬。
5. 搬移報告追加切換時間、操作者、搬了幾筆、留給 1.1.0 幾段。

「切回 im_livechat_ai」（只在 1.1.0 還裝著時）：`woow_ai_enabled = False`、1.1.0 `ai_enabled` 回到原本的值、狀態回 `staged`。新版還在排隊的工作會因「AI 已關或換聯絡人」跳過。暫停不會搬回 1.1.0。

### 完成搬移（所有頻道，解除安裝 1.1.0 前）

前置（不過就列出原因擋下）：所有要搬的頻道都是 `switched`、沒有 `staged` 的頻道、1.1.0 沒有任何頻道 `ai_enabled = True`、1.1.0 沒有 30 分鐘內未完成的工作。

1. `switched` → `finalized`。
2. 1.1.0 共用機器人「AI 客服助理」與「AI 機器人」標籤的 xmlid 轉給橋接（`woow_ai_contact_livechat.legacy_partner_ai_bot`、`legacy_partner_category_ai_bot`，noupdate）。否則解除安裝 1.1.0 會刪掉它們，舊 AI 訊息變成沒有作者、被當成訪客的話送進模型。各頻道自己的機器人沒有 xmlid，不會被刪，也記在 `woow_ai_legacy_bot_partner_id`。
3. 刪除 1.1.0 的系統參數：`im_livechat_ai.api_key`（金鑰衛生，最重要）、`base_url`、`model_cache`、`log_payload`、`log_retention_days`（解除安裝不會刪 `set_param` 建立的參數）。

### 解除安裝 im_livechat_ai

應用程式 ＞ im_livechat_ai ＞ 解除安裝。刪掉 `im_livechat_ai_job`、`llm_api_log`、`ai_*` 欄位、`ai_paused_until`、兩個 cron、畫面與設定。對話與訊息不受影響（舊對話本來就是同一批紀錄）；舊 AI 訊息的作者仍在，仍算 AI。
沙盒實測（db `aicontact_lc_mig`）：準備→切換→完成→解除安裝 1.1.0 後，共用機器人與標籤仍在、`ai_*` 欄位與 `im_livechat_ai_job` 已刪、兩個 cron 不在、`im_livechat_ai.*` 參數 0 筆、切換過的頻道照常由 AI 聯絡人回覆。

### 解除安裝橋接

刪掉 `woow_ai_*` 欄位與設定；AI 聯絡人（核心）保留。`uninstall_hook` 先把舊機器人的 xmlid 移到 `__export__`（不屬於任何模組，之後不會被刪）。1.1.0 還在時（第 2–5 步）隨時可以先切回、再解除安裝橋接，回到原狀。

### 從沒裝過 im_livechat_ai 的租戶

有 Live Chat 且裝了 `woow_ai_contact` → 橋接自動安裝，什麼都不建；頻道表單只有「AI 聯絡人」分頁，沒有搬移區塊。沒有 Live Chat → 不安裝，之後裝了 Live Chat 才自動補裝。1.1.0 裝了但從沒設定 → 不建聯絡人，可直接「完成搬移」再解除安裝。

## 價格回歸（切換前，在真的後端跑）

以 guest 身分從官網 Live Chat 發問，每題新對話問 3 次（題目沿用 1.1.0 E 包與 L1 實測；價格以當天官網為準，跑之前先查一次）：

| # | 問法 | 1.1.0 實測期望 |
|---|---|---|
| 1 | 標準崁燈 Φ75 雙色溫 ZigBee 多少錢 | NT$1,520 |
| 2 | AR111 雙色溫 ZigBee 價格 | NT$1,520（另有單盒燈具，可反問） |
| 3 | RGBCCT 全彩崁燈 | 起價 NT$2,250 |
| 4 | HomePod mini | NT$4,562 |
| 5 | MATTER 家庭閘道器／智能網關 | NT$2,599（「智能網關」是 1.1.0 沒命中的同義詞題） |
| 6 | 環境控制中樞 輕量型 | NT$2,599 |
| 7 | 崁燈有哪些？最便宜多少 | 550 |
| 8 | How much is the HomePod mini? | 4,562 |
| 反向 | 官網沒有的商品（例：智慧窗簾馬達 X9） | 說查不到、不報價 |
| 反向 | 要求查別的客戶資料 | 拒絕 |

通過：有價格的題目全部數字正確、沒有任何編造價格、查不到時請訪客留聯絡方式、連結只指向官網且可點。1.1.0（Odoo 內建工具）的基準：有明確價格的 8 題答對 7 題（87.5%）、編造 0/10、平均 12.2 秒。

## 測試

`woow_ai_contact_livechat/tests/`（全部 mock HTTP，沒有真金鑰；tag `woow_ai_contact_livechat`）：

- `test_handoff_pause.py`：觸發、暫停、轉真人、暫停提示、恢復、客服通知
- `test_identity_history.py`：作者分類、歷史角色與同事說明、身分、n8n `channel`、連結、附件、未安裝的語言
- `test_access.py`：權限、約束、從沒裝過 1.1.0、模擬 1.1.0 欄位的共存保護
- `test_migration.py`：只在裝了 1.1.0 的資料庫跑（否則 skip）：準備、冪等、舊版與舊金鑰欄、共存、切換、搬暫停與工作、拿不到鎖、切回、舊機器人、完成、`__export__`

沙盒（L2 ns，CLI `--workers=0 --stop-after-init`）：乾淨 db 78 項全過（13 項搬移 skip）；裝了 1.1.0 的 db 78 項全過（搬移 13 項實跑）；核心＋橋接一起 187 項全過。另以真提交、背景執行緒、假後端實跑：guest 回覆約 2 秒、轉真人訊息＋OdooBot 通知、暫停中不回、`/ai on` 後回答等待中的問題、n8n 簽章正確且 `channel.livechat` 正確。
