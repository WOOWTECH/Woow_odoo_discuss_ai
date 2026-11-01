# Woow_odoo_discuss_ai 設計

Odoo 18。讓內部用戶與 portal 用戶在 Discuss 裡和「AI 聯絡人」一對一私訊；AI 聯絡人背後接 Hermes、公司 LiteLLM（OpenAI 相容）或 n8n webhook。同一套後端連接之後也接手 Live Chat AI（取代 `im_livechat_ai`）。

定案來源：`work/woow-ai-contact-20261031/COORD.md`（負責人 2026-10-31，定案 1–28；第二批 21–28 見文末「負責人第二批定案」；2026-11-01 定案 30–37 見「入口」與「18.0.1.3.0」，定案 38（n8n 對齊 im_livechat_ai 18.0.1.2.0）見「n8n」，定案 39（文件原檔直送）見「document_mode」與「文件」，皆 18.0.1.4.0）。線路格式的完整說明在 [PROTOCOL.md](PROTOCOL.md)（英文，給 Hermes／n8n 串接者）。

## 模組

| 模組 | 相依 | 安裝 | 內容 |
|---|---|---|---|
| `woow_ai_contact` | `mail`, `portal` | 手動 | AI 聯絡人、後端連接、回覆佇列、多媒體、推送 API、像 OdooBot 的自動私訊與搜尋、portal 入口、呼叫紀錄 |
| `woow_ai_contact_livechat` | `woow_ai_contact`, `im_livechat` | `auto_install` | Live Chat 頻道選 AI 聯絡人、轉真人／暫停、關鍵字轉真人、從 `im_livechat_ai` 搬移 |

不相依 `cs_portal_discuss`／`woow_odoo_portal_discuss`。有裝 `cs_portal_discuss` 的站，AI 私訊會自動出現在 `/my/discussions`（它是 `channel_type = 'chat'` 且使用者是成員）。

## 資料模型（woow_ai_contact）

### `woow.ai.contact`（AI 聯絡人）
| 欄位 | 說明 | 可見 |
|---|---|---|
| `name`、`image_1920`、`description` | 顯示用；`partner_id` 同步名稱與頭像 | 有權使用者 |
| `partner_id` | 機器人 partner（建立時自動產生，無 user，標籤「AI Contact」）。`active`＝AI 聯絡人有效**且** `allowed_group_ids` 不是空的；AI 聯絡人封存，或群組清空（只給 Live Chat 用），partner 就保持封存，一般「開始對話」搜尋找不到 | 有權使用者 |
| `active`、`sequence` | | |
| `allowed_group_ids` | 可使用的群組；預設 `base.group_user`（18.0.1.5.0 起，QA C；portal 使用者要由管理員明確加 `base.group_portal`，之前建立的聯絡人不變）。**空集合＝不能私訊**（不自動建私訊、「開始對話」搜不到、portal、推送都不列；只給 Live Chat 等橋接用） | 系統管理員 |
| `backend_type` | `openai`（Hermes／LiteLLM）或 `n8n` | 系統管理員 |
| `endpoint_url` | openai：base URL（…/v1）；n8n：webhook 完整網址 | 系統管理員 |
| `api_key` | 只寫不讀：非儲存 compute／inverse 欄位，實際值存在只用 SQL 存取的欄 `woow_ai_contact.api_key_secret`（不是 ORM 欄位，任何 read／export／表單都拿不到）；另有 `api_key_set` 顯示是否已設定。**端點換到另一台伺服器（scheme／主機／埠不同，或清空）或換後端類型、同一次寫入又沒給新金鑰 → 已存的金鑰自動移除**（不會把公司 LiteLLM 金鑰送到新主機，也不會把 n8n 簽章金鑰當 Bearer 送出；原本沒網址、之後才填的不算，給搬移用）；表單沒有金鑰時顯示提示。openai：Bearer 金鑰；**n8n：HMAC 簽章金鑰，表單標籤「簽章金鑰」，永遠不送出** | 系統管理員 |
| `model` | openai 用；可空（Hermes 用自己的預設） | 系統管理員 |
| `system_prompt` | 可空；空白＝完全交給後端角色 | 系統管理員 |
| `max_history` | 預設 50，1–200（同 livechat） | 系統管理員 |
| `timeout_seconds` | 預設 300（5–3600）；整個回覆的總時限（串流時另有 30 秒空閒逾時） | 系統管理員 |
| `openai_stream` | 預設 True；openai 型要求 SSE 串流（定案 21） | 系統管理員 |
| `max_retries` | 預設 1，0–2；只對連線失敗、HTTP 429、503（定案 26） | 系統管理員 |
| `welcome_message`、`error_message` | 第一次開對話的招呼語、失敗時的回覆 | 系統管理員 |
| `document_mode` | 18.0.1.2.0 起；openai 型的使用者文件怎麼給模型：`file`（預設，每個文件原檔以 OpenAI `file` 內容送出、帶自己的類型，Odoo 不讀也不轉換）、`link`（只給簽名網址，Hermes 用：它對 file 內容回 400）。18.0.1.4.0（定案 39）拿掉 `text`（Odoo 擷取文字）與 `tools/documents.py`，升級時 `text`→`link`；細節與上限見 PROTOCOL.md「Documents」 | 系統管理員 |
| `download_domains` | AI 回覆中可下載成附件的網域白名單（每行一個，精確主機名或 `*.example.com`） | 系統管理員 |
| `max_download_mb` | 預設 20 | 系統管理員 |
| `push_key_hash`、`push_key_hint` | 推送 API 金鑰（只存 sha256；「產生推送金鑰」按鈕只顯示一次） | 系統管理員 |
| `onboarded_user_ids` | 18.0.1.3.0 起；已自動建過（或找到過）私訊的使用者（表 `woow_ai_contact_onboarded_user_rel`，主鍵 contact＋user）。不在清單內、又有權使用的使用者，下次打開討論／portal 首頁時才自動建私訊 | 系統管理員 |
| `log_payload` | 預設 False；開了才在呼叫紀錄存請求／回應內容（除錯用，會提示隱私） | 系統管理員 |

### `woow.ai.job`（回覆工作）
沿用 `im_livechat_ai` 1.1.0 C 包的設計並一般化；**定案 29（18.0.1.1.0）起，等 AI 回覆的工作移出 HTTP 網頁程序**：
- 使用者訊息與 job 在同一交易建立（pending，`run_after`＝debounce 1.5 秒後）；HTTP 請求只做 `ir.cron._trigger()`（呼叫時間＝現在：Odoo 在 commit 後送 `NOTIFY cron_trigger`，閒置的 cron worker／cron thread 立刻醒來；未來時間的 trigger 不送 NOTIFY，最慢要等 cron 的 60 秒輪詢，所以 debounce 由 dispatcher 依 `run_after` 自己等）。**HTTP 程序不再起任何執行緒**。
- 排程動作「AI Contacts: Run AI Replies」（`ir_cron_run_ai_jobs`，每 1 分鐘，priority 1；原本 2 分鐘的復原 cron 併入，18.0.1.0.0 的 `ir_cron_recover_ai_jobs` 在升級時刪除）的回呼 `_cron_run_jobs` 是 dispatcher（`tools/dispatcher.py`，不含 Odoo 的純 Python，單元測試）：
  - 抓到期的對話（`_dispatch_candidates`：每個頻道取最新一筆 pending 或「running 超過 60 秒」的 job；到期＝`run_after` 已到；channel 的 advisory lock 有人握著＝主人還活著，不碰），每段對話一條執行緒、各自的 READ COMMITTED cursor，**在 cron 程序內**跑（`_channel_worker`：最新那筆先跑，舊的被 supersede；跑完再看同頻道有沒有新到期的，**但只在接單時窗還開著、伺服器沒在關閉時才接下一筆**，否則留給下一趟，由這趟結束時的 retrigger 接手）。
  - **被別的程序鎖住的對話不算「等一下就到期」**：只有 `run_after` 還沒到的 job（debounce、`_defer`）會讓 dispatcher 留在這一趟等；鎖住的對話由鎖的主人自己接續（備援模式的執行緒、另一個 dispatcher 都會 chain），每分鐘的 cron 是保底。以前鎖住會回報「5 秒後到期」，整趟預算期間每 2 秒空查一次。
  - **公平分配**：同時處理數依 AI 聯絡人輪流分配（目前執行中最少的聯絡人先、同聯絡人內最舊的先）；有兩個以上啟用中的 AI 聯絡人、且同時處理數 ≥ 2 時，同一個聯絡人不會占走最後一個空位（慢的後端不會把其他聯絡人全部擋住）。只有一個 AI 聯絡人時不保留。
  - 同時最多 `woow_ai_contact.max_concurrency`（預設 4，上限 16）段對話；另依本程序連線池剩餘連線限制：每段回覆只握一條 cursor，「輸入中」改由全程序共用的一條執行緒（`_TypingHub`，每 20 秒一條短 cursor 刷新所有進行中的回覆），所以上限是「剩餘連線 − 2」（一條給 dispatcher 查詢、一條給輸入中）。cron pod 的 `db_maxconn` 至少要「同時處理數＋cron 自己用的 3 條（LISTEN、cron_cr、回呼的 cursor）＋2」。
  - 回覆進行中每 2 秒查一次有沒有新的到期對話（一個查詢；實測 150 秒回覆 79 次＝約 0.5 次／秒）；沒有執行中、但 20 秒內有 job 會到期（debounce、延後的 job）就等它，不然結束。不用 LISTEN（多佔一條連線，平台 cron pod 連線很少）。查詢失敗（連線池滿、DB failover）只記 log、當作沒有工作，不讓 cron 失敗（失敗會讓 `failure_count` 增加、這個排程被排到所有排程後面）。
  - 單段對話的 unit 丟例外（或剛跑完的 job 仍到期＝它當掉了）時，那段對話退避 5 秒、連續失敗加倍（最多 60 秒），不會忙迴圈（審查實測舊版 3 秒 17321 次）。
  - 時間預算（接單時窗）：`limit_time_real_cron`（prefork：`-1`＝`limit_time_real`、`0`＝不限；threaded：>0 才用，否則 `limit_time_real`）減去本趟已用的時間、減最長逾時與 60 秒餘裕，最多 300 秒。已用時間：threaded 用 cron thread 的 `start_time`（同一 db 前面跑的排程也算進去）；prefork 量「WorkerCron 上次回報看門狗到現在」：模組載入時把 `PreforkServer.pipe_ping` 包一層只記時間（原樣轉呼叫），所以閒置等待和同一輪排在前面的排程（例如 Email Queue）都算進去；這個 worker 還沒在載入後回報過（registry 剛載入的第一趟）時，用 70 秒＋載入以來的時間。預算用完就不再接新的（unit 內也不再接續）、**等執行中的全部 join 完**、有剩餘工作就再 `_trigger()` 自己後返回。所以一趟最長＝接單時窗＋最長逾時＋60 秒，cron worker 只會在兩趟之間回收（`limit_request`、記憶體、`limit_time_worker_cron` 都是在 `process_work` 之後才檢查），不會帶著進行中的 AI 呼叫被回收。
  - **共用的 cron worker**（cron 程序的 `max_cron_threads` ≤ 1，或同一個 cron 程序服務多個 db：`--database` 有多個、threaded 已載入多個 registry、prefork 沒指定 db 時 `list_dbs` 有多個）：一趟期間這個 worker 不能跑其他排程、也不能換別的 db（Odoo 18：prefork 一次 `process_work` 只處理一個 db、threaded 的 cron thread 依序跑所有 registry），所以接單時窗降到 60 秒、閒置等待降到 5 秒（`_defer` 15 秒的 job 改由到期時間的 trigger 接手，最慢要等 cron 的 60 秒輪詢）。一趟上限變成 60＋最長逾時＋60 秒。表單會顯示警告；建議 `max_cron_threads` ≥ 2，並且每個有 AI 聯絡人的 db 各有自己的 cron 程序。
  - **伺服器關閉**：每次醒來檢查 SIGINT handler 綁定的物件——threaded 的 `ThreadedServer.quit_signals_received > 0`、prefork worker 的 `alive == False`（master 送 SIGINT）——為真就不再接新工作，也不再接續，只等執行中的。prefork 的 master 優雅關閉最多等 `limit_time_real` 秒才送 SIGTERM；threaded 的 `stop()` 只等非 daemon 執行緒 1 秒就關閉連線池，所以 threaded 關閉時進行中的回覆仍會被切斷（job 留在 running，重啟後 60 秒被復原、後端會再被呼叫一次），只是不會再多接新的。
  - 鎖忙的對話（別的程序在處理）不碰；unit 剛好搶輸鎖（BUSY）時 5 秒後再試；job 可以 `_defer(秒數, 原因)` 延後自己（橋接在 1.1.0 還握著對話鎖時用，15 秒），dispatcher 在閒置等待內就同一趟等它，並在到期時間 `_trigger` 一次當備援（以前要等 2 分鐘的復原 cron）。
  - 同一個 cron 不會兩個 worker 同時跑（ir.cron 的 `FOR NO KEY UPDATE SKIP LOCKED`）；跨程序（備援模式的執行緒、另一個 dispatcher）靠每個頻道的 advisory lock：拿不到就不跑，拿到後重讀狀態（READ COMMITTED），已 done 的不會再呼叫後端。「Run Manually」從網頁按下時只 `_trigger`，不在網頁程序等回覆。
  - 回呼開始時先 commit 回呼自己的交易（job_cr），等回覆期間它不留 snapshot。**Odoo 的 cron_cr 沒辦法提早 commit**：整趟都是 idle in transaction，保留 REPEATABLE READ snapshot（VACUUM 清不掉這段期間的 dead tuple），並用 `FOR NO KEY UPDATE` 鎖住這個排程的 ir_cron 列；所以一趟期間在表單修改／停用這個排程、按「Run Manually」、解除安裝本模組都會得到「This cron task is currently being executed」，要等這趟結束。這是 Odoo 長時間 cron 本來的行為，只能靠縮短一趟（時窗後不接續、共用 worker 的短時窗）控制。
- **模式**（`_job_runner`，每個資料庫快取 60 秒，改變時記一次 log，AI 聯絡人表單「回覆處理方式」顯示）：系統參數 `woow_ai_contact.job_runner` = `auto`（預設）／`cron`／`thread`。auto：有 cron 執行者**且** cron 時限 ≥ 最長逾時＋60 秒（prefork 再＋70 秒）→ `cron`；否則 `thread`（備援：commit 後在建立 job 的程序起非 daemon 執行緒，就是 18.0.1.0.0 的做法，cron 只做復原、交給執行緒後立刻返回）。強制 `cron` 但時限不夠時表單警告（長回覆會在 cron worker 被看門狗砍掉時中斷並重跑）。
  - 本程序 `max_cron_threads` > 0（且不是 `--stop-after-init`／測試）：用本程序的設定判斷。
  - 本程序沒有 cron thread（例如平台 web pod `max_cron_threads = 0`、cron 在獨立 pod）：**用 cron 程序回報的設定判斷，不用本程序的**。cron 程序每趟開始、以及一趟進行中每 60 秒（在 dispatcher 的查詢裡），把自己的伺服器型態（prefork／threaded）、實際套用的 cron 時限、cron thread 數、服務的 db 數寫進系統參數 `woow_ai_contact.dispatcher_state`（JSON，用 SQL upsert，不清 ormcache；解除安裝時刪除）。10 分鐘內有回報＝有 cron 執行者，依它回報的型態與時限算 `required` 與是否夠用；沒有回報（全新安裝第一趟之前、cron 停了）→ 備援。以前用 `ir_cron.lastcall`，它要等一趟結束才更新，一趟超過 10 分鐘就會在中途被判斷成沒有 cron；而且 web pod 用自己的 prefork／`limit_time_real_cron` 算，同一份設定兩邊結論不同（審查實測 cron pod 判 cron、web pod 判 thread）。
- **部署注意**：PaaS 租戶 chart 預設 `workers = 2`、`limit_time_real = 120`、`limit_time_real_cron = -1`（＝120 秒），不夠 300 秒逾時 → auto 會留在備援模式（行為與 18.0.1.0.0 相同，不會更差）。要用排程模式須把 `limitTimeRealCron` 設到至少 430（建議 600）；平台 cron pod（threaded、`limit_time_real_cron = 1800`）已足夠。
- 每個 `discuss.channel` 一把 PostgreSQL advisory lock；同頻道只處理最新一筆（較舊的 skipped），最新那筆帶完整歷史。
- 每筆最多 3 次；超過 30 分鐘放棄；處理中當掉（還沒 claim 就例外）的 job 60 秒後才再試，不會被同一趟一直撿起來。
- 呼叫後端前與張貼前都再檢查一次（是否有更新的 job、livechat 是否暫停）。
- 欄位：`channel_id`、`contact_id`、`message_id`（觸發訊息）、`kind`（reply／handoff）、`state`、`run_count`、`run_after`、`error`、`reply_message_id`。
- 預設訊息（錯誤、回覆內註記、無權限）的語言：身分語言沒安裝時退回目前語言或 en_US（`woow.ai.contact._installed_lang`；以前會丟「Invalid language code」讓 `_give_up` 中斷、job 卡在 running）。
- 查詢數（沙盒實測，`sbx/live/measure_queries.py`）：建歷史＋組請求與歷史長度無關（約 12 個查詢）；整筆 job（openai，每則 2 個附件）4 則 83→76、20 則 122→75（附件的 prefetch 在 commit 清快取後失效，改成整段上下文共用一組 id）；張貼回覆 3 個檔案 32→30（附件一次 create）。系統參數（`database.uuid`／`secret`、`web.base.url`）本來就是 ormcache，每個 job 0 查詢。

### `woow.ai.call.log`（呼叫紀錄，只有系統管理員可讀）
`contact_id`、`channel_id`、`job_id`、`backend_type`、`status`（ok／http_error／timeout／invalid_response／error）、`http_status`、`duration_ms`、`prompt_tokens`／`completion_tokens`／`total_tokens`（openai 回應的 `usage`，沒有就是 0）、`error`（截斷；回覆中殘留 `MEDIA:` 路徑、內嵌圖被拒也記在這裡，狀態仍是 ok）、`request_payload`／`response_payload`（只有 `log_payload` 開時才存，data URI 與 base64 只留長度）。每日清除 30 天前的紀錄。

## 觸發

`discuss.channel` 覆寫張貼後的鉤子（與 `im_livechat_ai` 相同位置 `_notify_thread`，或 `message_post` 後）：
- `channel_type == 'chat'`，成員中有一位是 AI 聯絡人的 `partner_id`，作者不是該機器人 partner；
- 作者的 user 屬於 `allowed_group_ids`（否則回一句「您沒有使用這個 AI 聯絡人的權限」，不呼叫後端）；
- AI 聯絡人已封存時（18.0.1.5.0 起，QA C）：私訊裡有人寫訊息，機器人每段對話張貼一次固定訊息「這個 AI 聯絡人已停用。」（依作者語言翻譯，不呼叫後端，`discuss.channel.woow_ai_inactive_notice_sent`；聯絡人恢復有效時清掉，再封存會再提示一次）；Live Chat 不提示（橋接自己處理）；
- `message_type == 'comment'`，有文字或附件。
- 建 job（kind=reply）。

「輸入中」：job 開始呼叫後端時，機器人成員 `discuss.channel.member._notify_typing(True)`，結束（成功或失敗）時 `False`；長時間呼叫每 20 秒重送一次 True（前端約 60 秒會自動清除）。

## 後端協定

兩種後端都送同樣的上下文：系統提示詞（可空，`discuss.channel._woow_ai_system_prompt(contact, history)`，預設＝`contact.system_prompt`）＋最近 `max_history` 則（同 livechat：使用者→user，機器人→assistant；每則文字上限 16,000 字，超過截斷並加「…[truncated]」），另附固定 `session_id = "woow-ai-<db uuid 前 8 碼>-<channel_id>"`（定案 22，不用冒號），後端想自己做長期記憶可用。

身分（不代登入）：`discuss.channel._woow_ai_identity(contact)`，預設＝`contact._identity(_woow_ai_requester(contact))`：`user_id`、`partner_id`、`name`、`email`、`lang`、`tz`、`is_portal`、`company_id`（橋接可加 `is_guest`、`guest_id`、`visitor_key`）。n8n 的 `user` 照帶全部；openai 型在 system 訊息加一段模型看得到的身分區塊（姓名、語言、時區、帳號種類、user_id／partner_id，**不含 email**，定案 27），`metadata` 照帶（含 email）。預設訊息（錯誤、拒絕、回覆內註記）的語言也取自這裡的 `lang`。

逾時與重試（兩種後端共用）：連線 10 秒；openai 串流時 30 秒沒收到任何位元組就失敗，另外整個回覆不超過 `timeout_seconds`（requests 的 read timeout 只算單次讀取，所以總時限自己檢查）；非串流的讀取逾時＝`timeout_seconds`；n8n＝`min(timeout_seconds, 90)`（同 im_livechat_ai 18.0.1.2.0，Cloudflare 約 100 秒會切）。`max_retries`（0–2，預設 1）只對連線失敗、HTTP 429、503，退避 3 秒、10 秒，總時間不超過 `timeout_seconds`；讀取逾時、502、504、524 與其他回應一律不重送（Hermes 的工具可能已經執行一半，定案 26）。

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
- 文件（18.0.1.2.0 真 LLM 驗收發現模型不會開簽名網址；18.0.1.4.0 定案 39 改成原檔直送）：`file` 模式在最近 5 則使用者訊息內，從最新的開始，最多 5 個文件（PDF、docx、xlsx／xlsm、xls、csv、tsv、txt、md、json），每個 ≤4 MB、和圖片共用約 7 MB 的 data URI 額度（圖片先），原檔以 `{"type": "file", "file": {"filename", "file_data": "data:<類型>;base64,…"}}` 送出；類型看內容（`media.document_mimetype`，檔名只用來分辨 zip／純文字的同類），HTML／SVG／XML／JS 永遠不當檔案送。放不下、太大、太多或太舊時加一行 `[附件說明]` 並保留簽名網址。公司 LiteLLM 實測 PDF／xlsx／docx 都讀得到；woowtech Hermes 對 file 回 400 `unsupported_content_type`，所以 Hermes 用 `link`。只讀附件時用單筆 prefetch 讀 `raw`（`raw` 是 compute 欄位，經整段上下文共用的 prefetch 讀會把所有附件的檔案都讀進來）。
- 回覆：串流（`text/event-stream`）時略過 `:` 開頭的行（keepalive）與 `event:` 不是預設 message 的事件（如 `hermes.tool.progress`），累積 `delta.content`、忽略 `reasoning_content`，讀到 `[DONE]` 結束；沒有 `[DONE]` 也沒有 `finish_reason` 就斷了 → invalid_response；chunk 帶 `error` → invalid_response。後端不理 `stream` 而回 JSON 也接受。非串流取 `choices[0].message.content`（字串，或 content parts 中的 text）。`usage` 記進呼叫紀錄（不送 `stream_options`，串流時只有後端自己附才有）。
- 不送 `tools`；工具全部由後端自己處理（Hermes／n8n 用 Odoo MCP）。Hermes 不讀 `user`、`metadata`，身分只能靠 system 區塊。

### n8n（webhook，同步回應）
定案 38（11-01）：格式與 L1 `im_livechat_ai` 18.0.1.2.0 完全相同，同一個 n8n 工作流程兩邊都能用（含既有的公開 Chat Trigger，例如智居顧問）。
```
POST {endpoint_url}
X-Woow-Timestamp: <unix>           # 只有設了簽章金鑰才送這兩個 header；
X-Woow-Signature: sha256=<hex HMAC-SHA256(api_key, "<timestamp>.<raw body>")>   # 金鑰留空＝不簽章
                                   # 不送 Authorization（P0：n8n Executions 會存 headers）
{
  "event": "message",
  "session_id": "...",
  "action": "sendMessage",          # n8n Chat Trigger 讀的三個欄位（值同 session_id、message.text）
  "sessionId": "...",
  "chatInput": "...",
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
或 {"output": "markdown 文字"}       # n8n AI Agent／Chat Trigger 的預設回覆，一則（只有文字）
```
也接受 n8n 的 item 陣列 `[{...}]`（取第一個）。同時有多個時依序取 `skip`、`replies`、`reply`、`output`。
`url` 型附件同樣受白名單與大小限制。

- 簽章是對**實際送出的原始 body 位元組**算的（Odoo 送出的就是簽的那份），n8n 要開 Webhook 的 Raw Body 驗原始位元組；時間窗 **300 秒**；驗簽失敗回非 200（Odoo 記 http_error 並貼出 error_message）。AI Agent 失敗也要回非 200，不要回 200 加道歉文字（呼叫紀錄才看得到失敗）。
- 「測試連線」送 `{"event": "ping", "contact": {...}, "action": "loadPreviousSession", "sessionId": "woow-ai-ping-<id>"}`（有密鑰才簽章），回任何 200 都算成功；Chat Trigger 自己回 loadPreviousSession、不會跑 agent；n8n 範例對非 message 事件回 `{"skip": true}`。
- 金鑰：`api_key`（Odoo→n8n 簽章，選填）和推送金鑰（n8n→Odoo Bearer）是兩把不同的金鑰。n8n 聯絡人沒有簽章金鑰也能用（表單顯示提示、測試連線照常可按）；openai 型仍必須有 API 金鑰。
- Chat Trigger 的限制：流程用 `sessionId` 自己記憶，不看 Odoo 的 `history`／`system_prompt`／`events`，也驗不了來源；使用者的圖片／檔案只在 `message.attachments`（簽名網址），流程要用得自己下載（PROTOCOL.md「n8n Chat Trigger」一節）。

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
- `email` 是精確、不分大小寫的比對（`=ilike` 前先跳脫 `\`、`%`、`_`，地址裡的底線不是萬用字元）。
- 實作補充：`auth='none'` 的路由在 Odoo 18 預設用唯讀 cursor，所以明確設 `readonly=False`；資料庫必須能由網域判斷（單一資料庫或 dbfilter，PaaS 租戶即是）。收件人必須是有效使用者（只有 partner、沒有 user → 404）；`email` 對到多個使用者 → 400 `ambiguous_recipient`。推送也寫一筆呼叫紀錄（`backend_type = push`，不存內容）。推送不會觸發 AI 回覆。

## 入口

- **後台：像 OdooBot（定案 30–35，18.0.1.3.0）**。手機版 Discuss 是底部分頁（郵箱／聊天／群組／線上客服），沒有側欄，原本的側欄區塊「AI 聯絡人」在手機上看不到，所以拿掉（連同 `/woow_ai/contacts`、`/woow_ai/open` 兩條只給它用的 JSON 路由），入口一律是聊天清單：
  - **自動建私訊**：覆寫 `res.users._init_messaging(store)`（網頁前端每次載入時 `/mail/data` 的 `init_messaging`；OdooBot 是在 `_on_webclient_bootstrap` 建，同一個時機），在 `super()` **之前**呼叫 `woow.ai.contact._onboard_user(user)`：找出「有效、有機器人 partner、`allowed_group_ids` 與使用者群組有交集、使用者不在 `onboarded_user_ids`」的 AI 聯絡人；對每個先 `INSERT … ON CONFLICT DO NOTHING RETURNING` 記成已建（同一使用者兩個分頁同時載入時只有一個請求拿到，另一個等前者 commit 後拿不到或得到序列化錯誤由 Odoo 重試，不會建兩個私訊或兩則招呼語），已有私訊（之前開過、可能已取消置頂）就只記錄、不動它，沒有才 `_get_or_create_chat`（置頂、`allow_public_upload`、張貼招呼語）。因為在 `super()` 之前，同一次回應的 `initChannelsUnreadCounter` 就算進去（定案 32：招呼語算未讀，訊息圖示有數字；新成員的 `new_message_separator` 是 0，機器人的訊息就是未讀）。
  - `/mail/data` 是唯讀路由：沒有待建時只多一個讀取查詢；有待建而 cursor 是唯讀（有 replica 時）就主動丟 `ReadOnlySqlTransaction`，Odoo 用讀寫 cursor 重跑這個請求（不留「bad query」錯誤 log）。其他錯誤在 savepoint 裡記 log 後吞掉（不影響討論載入；整批回復，下次再試）。
  - **只自動建一次**（定案 34）：`onboarded_user_ids`（每人每個 AI 聯絡人一筆）。使用者取消置頂／關掉後不會自己回來；`_get_or_create_chat`（推送、portal、搜尋開啟都經過它）會重新置頂並送 `Store` 給使用者，也會記成已建。升級到 18.0.1.3.0 的搬移把「已經有私訊」的使用者都記成已建（不會有第二則招呼語）。
  - **搜尋**（定案 31）：Odoo 18 的「開始對話」（`ChannelSelector`，手機版聊天分頁的按鈕也是它）與指令面板都呼叫 `res.partner.im_search`，原生只搜內部使用者；擴充後另外加上「名稱符合、目前使用者有權使用（`_user_allowed`）」的 AI 聯絡人機器人 partner（`Store` 結果合併）。群組為空或沒權限的永遠搜不到。選了之後前端呼叫 `discuss.channel.channel_get(partners_to=[機器人])`：覆寫後只要對象是 AI 聯絡人的機器人，就先檢查權限（沒權限丟 `AccessError`「您沒有權限使用這個 AI 聯絡人」，不建頻道），再走 `_get_or_create_chat`（招呼語、上傳旗標、已建記錄、`force_open` 設 `fold_state = open`）；其他對象照原生。`_get_or_create_chat` 自己建頻道時帶 `woow_ai_channel_get` context 略過這段。
  - **狀態**（定案 35）：`res.partner._compute_im_status` 對 AI 聯絡人的機器人（含只給 Live Chat 用的封存機器人）回 `bot`，前端顯示綠色愛心（同 OdooBot，`mail` 的 `res.partner` 對 `base.partner_root` 的做法）。
- **設定選單**（定案 37）：AI 聯絡人、回覆工作、呼叫紀錄從「設定」（`base.menu_administration`）搬到「討論 › 配置」（`mail.menu_configuration`，與通知、語音及視訊、預設回應同一層，sequence 30–32），各自 `groups = base.group_system`（權限照舊）；舊的上層選單 `menu_woow_ai_root` 升級時刪除。18.0.1.5.0 起（QA C：內部使用者用網址 `/odoo/action-woow_ai_contact.action_woow_ai_contact` 看得到名稱與說明）視窗動作也設 `groups_id = base.group_system`，且 `woow.ai.contact` 的 `web_search_read`／`web_read`／`web_read_group`（網頁清單與表單的讀取）只給系統管理員（`_woow_ai_ui_allowed`；橋接另外開放 Live Chat 管理員，他們要幫頻道選 AI 聯絡人）。私訊、portal、推送都走 ORM，不受影響；讀取權限（ACL＋record rule）不變。
- **portal**：第一次進 `/my`（`CustomerPortal.home` 覆寫）就呼叫同一個 `_woow_ai_onboard`（定案 36，同樣只建一次）；卡片與 `/my/ai` 不變。不由核心自己做聊天頁（不採用 E2E 的建議）：Odoo 18 原生就有 `/discuss/channel/<id>`（`auth=public`，沙盒映像 `mail/controllers/discuss/public_page.py` 已確認）。`/my` 首頁卡片「AI 聯絡人」（portal 計數器 `woow_ai_contact_count`，沒有可用的 AI 聯絡人就不顯示）→ `/my/ai`（卡片清單）→ `/my/ai/<contact_id>/open` 找或建私訊後導到 `/discuss/channel/<id>`（Odoo 原生公開 Discuss 畫面，附件上傳／圖片預覽皆為 Odoo 原生）；有裝 `cs_portal_discuss` 時才加 `?discussions=1`（沒裝時 Odoo 會對未知參數記 warning）。內部用戶改導到後台 `/odoo/discuss?active_id=discuss.channel_<id>`。
- AI 私訊一律設 `allow_public_upload = True`：Odoo 的 `/mail/attachment/upload` 對非內部用戶只允許在有開這個旗標的頻道上傳（沙盒實測：沒開時 portal 上傳回 403「本頻道不允許上載附件」）。私訊只有兩個成員，開了不擴大存取範圍。
- 「開始對話」搜尋只列出自己有權使用的 AI 聯絡人（見上）；權限另外仍以 `allowed_group_ids` 在開啟與觸發回覆時把關。

## 權限

- `woow.ai.contact`：系統管理員全權；其他人只能讀（record rule：自己在 `allowed_group_ids` 內），且敏感欄位都有 `groups='base.group_system'`。
- `woow.ai.job`、`woow.ai.call.log`：只有系統管理員。
- 私訊內容：只有成員看得到（Odoo 原生）；呼叫紀錄預設不存內容。
- 後端網址只有系統管理員能改；`endpoint_url` 必須是 https（`localhost`／叢集內完整寫出的 `*.svc.cluster.local` 例外，給 PaaS 內網用；短服務名不接受）。
- 「測試連線」按鈕只限系統管理員：openai `GET {endpoint}/models`（不送 chat completion，否則 Hermes 會真的跑一次 agent 和工具）、n8n 的 ping（有密鑰才簽章）；畫面只顯示 HTTP 狀態和一句說明，不顯示上游回應內容。

## woow_ai_contact_livechat

實作（18.0.1.0.0）與操作細節：**docs/LIVECHAT.md**。以下是設計摘要。

- 欄位一律 `woow_ai_` 開頭（搬移期間 `im_livechat_ai` 1.1.0 同時安裝，它已有 `im_livechat.channel.ai_enabled`、`discuss.channel.ai_paused_until`，同名會共用資料欄而兩邊都回覆）：`im_livechat.channel.woow_ai_contact_id`、`woow_ai_enabled`、`woow_ai_pause_minutes`、`woow_ai_handoff_keywords`、`woow_ai_handoff_message`、搬移用的 `woow_ai_legacy_*`／`woow_ai_migration_state`；`discuss.channel.woow_ai_paused_until`。
- Live Chat 管理員可選**任一** AI 聯絡人（定案 25：**不加** `livechat_allowed` 開關；只給 Live Chat 用的聯絡人 `allowed_group_ids` 為空，橋接要給 Live Chat 管理員一條只讀 record rule 才選得到）；網址金鑰仍只有系統管理員看得到。後端要依 `channel.type == 'livechat'`／`user.is_guest` 只開放公開資料工具。
- 訪客訊息觸發 job；真人客服張貼 → 暫停 AI（預設 30 分鐘，可設）；訪客說「轉真人」等關鍵字 → handoff job 張貼固定訊息並通知客服（沿用 1.1.0 D 包）。
- 修 L1 實測的坑：預設關鍵字改成片語（「找真人」「轉人工」…，不再用單字「客服」「人工」「agent」）；暫停中訪客發訊息且最近 5 分鐘沒有客服發言 → 每次暫停張貼一次 `woow_ai_paused_message` 並再通知客服；客服 `/ai on`／`/ai off`／`/ai`（Live Chat 對話的指令）或對話表單按鈕立即恢復／暫停，恢復時回答沒人回的訪客訊息；暫停分鐘數一律檢查；Boolean 補 FALSE；搬移除了版本還要求舊金鑰欄已刪除。
- 身分語言只用已安裝的語言（guest 可能帶未安裝的語言碼，核心翻譯預設錯誤訊息時會丟 `Invalid language code`，工作卡在 running；沙盒實測發現）。
- 角色：訪客→user、AI→assistant、真人客服→assistant 並加「[真人客服 名字]」前綴（沿用 1.1.0）。
- 不提供 Odoo 端商品工具（負責人定案：工具全部交給後端）。上線前要在後端（Hermes／n8n）用 Odoo MCP 補上查價，並以 L1 實測的價格案例回歸。
- 使用核心鉤子：`_woow_ai_channel_contact`、`_woow_ai_check_author`、`_woow_ai_message_kind`、`_woow_ai_is_paused`、`_woow_ai_author_role`、`_woow_ai_requester`、`_woow_ai_identity`（guest 沒有 partner：放 guest 名稱、語言、時區）、`_woow_ai_system_prompt`（有真人客服訊息時加「同事回覆不要推翻」）、`_woow_ai_link_hosts`（官網網域）、`_woow_ai_channel_info`、`_woow_ai_conversation_events`（轉真人／暫停提示／暫停／恢復事件，`woow.ai.livechat.event`）；`woow.ai.job._process_handoff`、`_notify_failure(reason, visitor_told)`。關掉 AI 或換聯絡人後還在排隊的工作，核心 `_skip_reply` 已會跳過。
- n8n：`channel` 物件 `{"id", "type": "livechat", "livechat": {"livechat_channel_id", "livechat_channel_name", "operator_name", "country_code", "visitor_kind"}}`；`user` 加 `is_guest`、`guest_id`、`visitor_key`（`guest:<id>`／`partner:<id>`／`public`）。session_id 是「這一段對話」，跨對話用 `visitor_key`。
- 搬移（分段，可回復；細節依 L1 的 `L1-findings.md` 與 `staging/livechat/BRIDGE-SPEC.md` e 節）：
  1. **準備**（`post_init_hook`／「重新準備」按鈕）：只讀 `im_livechat_ai` 18.0.1.1.0 以後的資料（SQL 讀，舊版的外洩金鑰欄永不讀），以 1.1.0 的系統 LiteLLM 設定建立 openai 型 AI 聯絡人（每組不同的模型＋提示詞各一個），頻道記下 `woow_ai_legacy_*`，狀態 `staged`，**不切換**（安裝不等於切換，避免商品工具立刻消失）。
  2. **切換**（每個頻道，系統管理員按，一筆交易）：先確認後端已做完價格回歸；關 1.1.0 的 `ai_enabled`、開 `woow_ai_enabled`，搬仍未到期的暫停與 30 分鐘內未跑的工作，狀態 `switched`；可切回。
  3. **完成**：全部頻道切換並觀察後 `finalized`；1.1.0 共用機器人與標籤的 xmlid 轉給橋接（noupdate），刪除 `im_livechat_ai.*` 系統參數（金鑰優先）。
  4. **解除安裝** `im_livechat_ai`（舊呼叫紀錄不搬，需要的話先匯出）。橋接自己解除安裝時，`uninstall_hook` 把舊機器人的 xmlid 移到 `__export__`。
  5. 所有租戶都解除安裝 1.1.0 後，平台映像才拿掉 `im_livechat_ai`。
- 解除安裝核心 `woow_ai_contact`：`uninstall_hook` 封存所有 AI 聯絡人的機器人 partner（保留，舊訊息的作者）、把 AI 私訊對其他成員取消釘選並關掉 `allow_public_upload`，免得還能私訊一個不會回答的聯絡人。
- 共存保護：頻道的 1.1.0 `ai_enabled` 為 True（SQL 讀）時橋接不回覆；兩邊互相擋住「另一邊開著時打開」。

## 測試計畫（沙盒，不碰正式站）

- 單元：觸發條件、權限、markdown→HTML（含表格與惡意輸入）、白名單下載（轉址、超大、假 mimetype）、簽名網址（過期、竄改）、推送 API（401／403／404）、佇列（連發只回最新、重啟恢復、逾時）。
  審查後補：SSE 串流（keepalive、`hermes.tool.progress`、reasoning_content、跨 chunk 的多位元組字、空閒逾時、總時限、沒有 [DONE]）、重試範圍（429／503 重試，502／504／524／讀取逾時不重試、總時限內）、messages 正規化、內嵌張數與總量、假圖片不內嵌、身分／提示詞鉤子、data URI 與 MEDIA、`<thinking>`／特殊 token、連結網域鉤子、OOXML 類型、純 http 只限內網、測試連線、機器人 partner 封存規則、換聯絡人跳過、`visitor_told`。沙盒實測（sbx/live）：25 秒串流（每 2 秒 keepalive）、n8n 不帶 Authorization＋300 秒時間窗、測試連線。
- 整合：沙盒 ns 跑真的 Hermes／n8n？—— n8n 用沙盒內的假 webhook（簡單 HTTP 伺服器）＋正式 n8n 的測試 workflow；openai 型用 LiteLLM 測試金鑰。
- 瀏覽器：內部用戶聊天清單（含手機版）看到自動建好的 AI 私訊、「開始對話」搜尋開 AI、portal 用戶 `/my/ai` 開對話、上傳圖片／PDF、收到表格與圖片附件。

## 實作補充與差異（18.0.1.0.0）

- **Bridge 掛勾**（給 `woow_ai_contact_livechat`）：`discuss.channel._woow_ai_channel_contact()`（這個頻道由哪個 AI 聯絡人回答；核心＝含機器人成員的 chat）、`_woow_ai_check_author()`（ok／denied／ignore）、`_woow_ai_message_kind()`（reply／handoff／None）、`_woow_ai_is_paused()`、`_woow_ai_author_role()`（歷史角色與前綴，例如「[真人客服 名字]」）、`_woow_ai_requester()`（身分要送誰）、`_woow_ai_identity(contact)`（身分 dict，預設 `contact._identity(requester)`）、`_woow_ai_system_prompt(contact, history)`（預設 `contact.system_prompt`）、`_woow_ai_link_hosts(contact)`（預設 None）、`_woow_ai_channel_info()`（n8n 的 `channel` 物件）、`_woow_ai_conversation_events(contact, history)`（18.0.1.2.0；模型要知道、但不是它說的事實，例如轉真人、恢復 AI；預設空。openai 放在 system 訊息最後一段、n8n 是 `events`；只宣告事件的通知訊息由橋接在 `_woow_ai_author_role` 回 None 拿掉）。`_woow_ai_handle_new_message(message)` 的契約：回傳建立的 `woow.ai.job` 或 False；**先算 kind 再檢查暫停**（kind 鉤子看得到每一則允許的訊息，暫停中就不建 job；橋接要在 `super()` 回傳 handoff job 之後才暫停）。`woow.ai.job` 的 `_process_handoff()`、`_notify_failure(reason, visitor_told=True)` 也是掛勾（太舊放棄傳 `visitor_told=False`，`_give_up` 傳 True）；核心版 handoff 直接 skipped、失敗只記 log（1 對 1 私訊裡使用者已看到錯誤訊息，沒有客服要通知）。`_skip_reply` 與張貼前都檢查 `channel._woow_ai_channel_contact() == job.contact_id`，不同就 `skipped`「AI turned off or contact changed」。
- **觸發的保護與併發**：`_notify_thread` 在 savepoint 裡建 job，其他錯誤記 log 後吞掉（不影響使用者的訊息）；但 PostgreSQL 併發錯誤（`PG_CONCURRENCY_EXCEPTIONS_TO_RETRY`：serialization failure、lock not available、deadlock）一律往上丟，讓 Odoo 重試整個請求。savepoint 進入時會先 flush Odoo 自己待寫的欄位（例如頻道的 `last_interest_dt`），兩個人同一瞬間在同一頻道發言就會在這裡撞到；以前吞掉後交易已中止，發訊者拿到 500（`InFailedSqlTransaction`，搭配 cs_portal_discuss 的群組實測 10 輪壞 10 則）。onboarding（`res.users`）與橋接的 `_woow_ai_log_event` 同樣處理。
- **佇列**：見上方 `woow.ai.job`（18.0.1.1.0 起在 cron 程序內處理；備援模式與 1.1.0 相同：postcommit 非 daemon thread）。每頻道 advisory lock、READ COMMITTED cursor、張貼前鎖頻道列再檢查一次、每筆最多 3 次、30 分鐘放棄。檔案下載在鎖頻道列**之前**做（不寫資料庫），鎖住後才建附件並張貼，避免長時間擋住使用者發訊。HTTP 呼叫期間沒有開著的交易（逾時秒數等設定在 commit 前先讀好）。
- **輸入中**：開始呼叫時 `True`、張貼前 `False`；另起一個 daemon thread 用自己的 cursor 每 20 秒重送 `True`。沙盒實測 45 秒呼叫：True 於 0／20／40 秒、結束送 False。
- **轉址**：後端呼叫（`_http_post`）與測試連線都不跟隨轉址（`allow_redirects=False`），3xx 視為 `http_error`；否則 307／308 會把整段對話、身分與簽名網址用純 http 重送到別的主機，301／302 會改成 GET 打任意網址。
- **串流總時限**：沒有 chunked 編碼時 urllib3 一次讀 1024 位元組會等到湊滿或 EOF，小 keepalive 一直重設 socket 逾時，chunk 邊界的時限檢查就失效；`_StreamWatchdog` 在總時限到時 `shutdown` socket，讀取立刻返回並記 timeout。
- **重試**：`max_retries`（預設 1）次，只對連線失敗（請求沒送出：連不上、找不到主機、連線逾時，即 urllib3 `NewConnectionError`／`ConnectTimeout`；收完請求後斷線的 `ProtocolError`／`RemoteDisconnected` 不算）、HTTP 429、503，3 秒、10 秒後，不超過總時限；讀取逾時與其他 HTTP 錯誤不重試（n8n 不保證冪等、Hermes 工具可能已執行）。
- **n8n 回應**寬鬆接受 `{"reply": "文字"}`、`{"output": "文字"}` 與 n8n 的 item 陣列 `[{...}]`；`replies` 最多 10 則。
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
