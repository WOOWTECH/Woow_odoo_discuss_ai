# Backend protocol

How `woow_ai_contact` talks to an AI backend, and how a backend talks back
(push API). Everything below uses placeholders: `odoo.example.com`,
`llm.example.com`, `n8n.example.com`, `sk-REPLACE-ME`, `whsec-REPLACE-ME`,
`wpk_REPLACE-ME`.

Each AI contact has one backend:

| `backend_type` | Endpoint URL (AI contact form) | Typical backends |
|---|---|---|
| `openai` | base URL ending in `/v1`; `/chat/completions` is appended | Hermes, LiteLLM, any OpenAI-compatible server |
| `n8n` | full webhook URL | n8n "Webhook" node (production URL) |

The endpoint must be `https://`. Plain `http://` is accepted only for
`localhost`, `127.0.0.1`, `::1` and in-cluster hosts written in full,
ending in `.svc.cluster.local` (for example
`http://hermes.ai.svc.cluster.local:8642/v1`); a short service name such as
`http://hermes:8642/v1` is refused.

## Keys: three different secrets

| Secret | Direction | Where it is set | How it is used |
|---|---|---|---|
| API key (`openai`) | Odoo → backend | AI contact, *API Key* | `Authorization: Bearer <key>` on every call |
| Signing secret (`n8n`) | Odoo → n8n | AI contact, *Signing Secret* (same field, labelled for n8n) | HMAC key of `X-Woow-Signature`; **never sent** |
| Push key | backend → Odoo | AI contact, *Generate Push Key* (shown once) | `Authorization: Bearer wpk_…` on `POST /woow_ai/v1/push` |

Use a different random value for the n8n signing secret and the push key.

## Common context

Every call carries:

- **history**: the most recent `max_history` messages of the chat (default 50),
  oldest first. Messages of the AI contact are `assistant`, everything else is
  `user`. Messages without text and without attachments are left out. The text
  of one message is cut at 16,000 characters (`…[truncated]` is appended).
- **system prompt**: the AI contact's prompt, or nothing when it is empty
  (then the backend decides the persona). Bridge modules may supply their own
  (`discuss.channel._woow_ai_system_prompt`).
- **session id**: `woow-ai-<first 8 hex of the database uuid>-<discuss.channel id>`,
  for example `woow-ai-0edfec80-42` (no colon). It is stable for one
  user/contact chat, so a backend can keep its own memory under it.
- **identity** of the person the AI talks to (no login is delegated):

```json
{"user_id": 7, "partner_id": 15, "name": "王小明", "email": "user@example.com",
 "lang": "zh_TW", "tz": "Asia/Taipei", "is_portal": true, "company_id": 1}
```

  n8n receives it as `user`. For `openai` backends the model sees a short
  identity block in the system message (name, language, time zone, account
  type, user and partner ids — **not** the email, since prompts may stay in
  the model provider's logs), and the full identity is repeated in
  `metadata` (which LiteLLM and Hermes do not pass to the model). Bridges may
  add fields (Live Chat: `is_guest`, `guest_id`, `visitor_key`).
- **user attachments** as `ATT` objects. `url` is a signed download link valid
  for one hour (`GET`, no cookie needed; only attachments of messages in AI
  chats are ever signed):

```json
{"id": 12, "name": "報價.xlsx", "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
 "size": 12345, "url": "https://odoo.example.com/woow_ai/attachment/12/1793433600.4f1c…",
 "expires_at": "2026-10-31T09:00:00Z"}
```

### Timeouts and retries

- Connect timeout: 10 s.
- `openai` with *Stream Replies* on (the default): the call fails when **no
  byte** arrives for 30 s (SSE comments such as `: keepalive` count), and when
  the whole answer takes longer than the AI contact's *Timeout* (default 300 s).
- `openai` without streaming and `n8n`: the answer must arrive within *Timeout*.
- *Retries* (0–2, default 1): only when the connection failed (the request was
  not sent) or the backend answered **HTTP 429 or 503**, after 3 s and then 10 s,
  and only while the total *Timeout* allows it. A read timeout, HTTP 502, 504,
  524 or any other answer is **never** retried: the backend may already be
  running tools (a retried Hermes call would run them twice).
- Any failure posts the AI contact's error message in the chat and is kept in
  the call log (status, HTTP code, duration; never the upstream body).

While the backend works the bot shows "typing…".

### Cloudflare and other proxies (about 100 s)

Backends reached through Cloudflare (public hostnames) get HTTP 524 when no
byte is sent for about 100 s, whatever the AI contact's timeout says. Hermes
tool chains can take minutes. Therefore:

- `openai` backends: keep *Stream Replies* on. Hermes sends a keepalive comment
  every 10 s and LiteLLM streams tokens, so the connection never idles.
- `n8n`: a synchronous webhook cannot stream. For long workflows answer
  `{"skip": true}` at once and send the reply later with the push API; or call
  n8n in-cluster (`http://<service>.<namespace>.svc.cluster.local:5678/webhook/…`)
  when a NetworkPolicy allows it.

## `openai` backend

Request (`POST {endpoint_url}/chat/completions`):

```http
POST /v1/chat/completions HTTP/1.1
Host: llm.example.com
Authorization: Bearer sk-REPLACE-ME
Content-Type: application/json
Accept: text/event-stream
```

```json
{
  "model": "gpt-example",
  "messages": [
    {"role": "system", "content": "你是 WoowTech 的產品顧問。\n\nYou are chatting in Odoo with this person (information only, no login is delegated):\n- name: 王小明\n- language: zh_TW\n- timezone: Asia/Taipei\n- account: portal user (customer)\n- user_id: 7\n- partner_id: 15"},
    {"role": "user", "content": "[附件] 規格.pdf（application/pdf，120.4 KB）：https://odoo.example.com/woow_ai/attachment/31/1793433600.9be2…"},
    {"role": "assistant", "content": "收到規格書，請問想了解哪個型號？"},
    {"role": "user", "content": [
      {"type": "text", "text": "這個型號多少錢？"},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo…"}}
    ]}
  ],
  "user": "woow-ai-0edfec80-42",
  "metadata": {"session_id": "woow-ai-0edfec80-42", "odoo_user_id": "7", "odoo_partner_id": "15",
               "odoo_name": "王小明", "odoo_email": "user@example.com", "odoo_lang": "zh_TW",
               "odoo_tz": "Asia/Taipei", "odoo_is_portal": "true", "odoo_company_id": "1"},
  "stream": true
}
```

- `model` is sent only when the AI contact has one. Hermes ignores it and uses
  its own configured model.
- The `system` message is always there: the prompt (when not empty) followed
  by the identity block.
- `messages` always **start and end with a `user` turn** (Hermes takes the last
  item as the new input; Anthropic-style models would continue a trailing
  assistant turn): assistant messages before the first user message (the
  welcome message) are dropped; assistant messages posted after the last user
  message (push, refusal or error messages) are moved just before it, and
  when that would put them first they are quoted in one leading user turn.
- Images (`image/png`, `image/jpeg`, `image/gif`, `image/webp`, type checked
  on the content, at most 5 MB each) are inlined as data URIs only for the
  **3 most recent user messages**, newest first, at most **4 images and about
  7 MB of data URIs per request** (Hermes refuses requests over 10 MB). Every
  other file, and every image over these limits, becomes a text line
  `[附件] <name>（<mimetype>，<size>）：<signed url>`.
- `metadata` holds strings only (at most 16 keys, 512 characters each), as the
  OpenAI API requires; it is informational.
- `stream` is sent only while *Stream Replies* is on.
- No `tools` are sent: tools (e.g. the Odoo MCP server) belong to the backend.

### Response

Streamed (`Content-Type: text/event-stream`, the default):

```text
: keepalive

event: hermes.tool.progress
data: {"tool": "odoo.search_read", "status": "running"}

data: {"choices": [{"delta": {"reasoning_content": "…"}}]}

data: {"choices": [{"delta": {"content": "這款目前"}}]}

data: {"choices": [{"delta": {"content": "售價如下…"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 812, "completion_tokens": 64, "total_tokens": 876}}

data: [DONE]
```

- Lines starting with `:` and events other than the default `message` event
  are ignored; `delta.content` pieces are joined; `reasoning_content` is
  ignored; `[DONE]` ends the answer. A stream that ends without `[DONE]` and
  without a `finish_reason` is an invalid response.
- A chunk carrying `error` is an invalid response.

Not streamed (or a backend that ignores `stream` and answers JSON): a normal
chat completion; the reply text is `choices[0].message.content` (a string, or a
list of `{"type": "text"}` parts).

```json
{"choices": [{"message": {"role": "assistant",
  "content": "這款目前售價如下：\n\n| 型號 | 價格 |\n|---|---:|\n| A1 | NT$1,200 |\n\n![規格圖](https://files.example.com/a1.png)"}}],
 "usage": {"prompt_tokens": 812, "completion_tokens": 64, "total_tokens": 876}}
```

`usage` (when given) is stored in the call log (`prompt_tokens`,
`completion_tokens`, `total_tokens`; 0 when missing). Streams carry usage only
when the backend adds it by itself (Odoo does not send `stream_options`).

## `n8n` backend

Request (`POST {endpoint_url}`), JSON body (UTF-8, compact):

```http
POST /webhook/ai-contact HTTP/1.1
Host: n8n.example.com
Content-Type: application/json
X-Woow-Timestamp: 1793430000
X-Woow-Signature: sha256=5d2b…
```

There is **no `Authorization` header**: the signing secret is never sent. (n8n
keeps the headers and body of every execution; a bearer secret would be
readable by everyone who can open the workflow's executions.)

```json
{
  "event": "message",
  "session_id": "woow-ai-0edfec80-42",
  "contact": {"id": 3, "name": "產品顧問"},
  "user": {"user_id": 7, "partner_id": 15, "name": "王小明", "email": "user@example.com",
           "lang": "zh_TW", "tz": "Asia/Taipei", "is_portal": true, "company_id": 1},
  "channel": {"id": 42, "type": "chat"},
  "system_prompt": "",
  "message": {"id": 991, "text": "幫我看這份報價單",
              "attachments": [{"id": 12, "name": "報價.xlsx", "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               "size": 12345, "url": "https://odoo.example.com/woow_ai/attachment/12/1793433600.4f1c…",
                               "expires_at": "2026-10-31T09:00:00Z"}]},
  "history": [
    {"role": "assistant", "text": "您好，我是產品顧問。", "attachments": []},
    {"role": "user", "text": "你好", "attachments": []}
  ]
}
```

`message` is the newest user message (the one being answered); `history` holds
the earlier context, unchanged (the welcome message and pushes included).
Several quick messages in a row get **one** call, with all of them in the
context. Live Chat channels (bridge module) send `"type": "livechat"` and a
`livechat` object in `channel`.

The *Test Connection* button sends a signed `{"event": "ping", "contact": {…}}`;
answer any HTTP 200 (for example `{"skip": true}`) to non-`message` events.

### Verifying the call (required)

`X-Woow-Signature` is `sha256=` + hex HMAC-SHA256 keyed with the AI contact's
signing secret, over the bytes `"<X-Woow-Timestamp>.<raw request body>"`. The
body Odoo sends is exactly the body it signed: verify the **raw bytes**
(enable *Raw Body* on the n8n Webhook node), never a re-serialised JSON.
Reject a timestamp more than **300 s** away from your clock (replay window) and
compare in constant time. When the check fails answer a non-200 status (401):
Odoo records `http_error` and posts the AI contact's error message.

n8n Code node (needs `NODE_FUNCTION_ALLOW_BUILTIN=crypto`; keep the secret in
an environment variable, never in the workflow):

```js
const crypto = require('crypto');
const SECRET = $env.WOOW_AI_SIGNING_SECRET;
const item = $input.first();
const raw = (await this.helpers.getBinaryDataBuffer(0, 'data')).toString('utf8');
const h = Object.fromEntries(Object.entries(item.json.headers || {}).map(([k, v]) => [k.toLowerCase(), v]));
const ts = String(h['x-woow-timestamp'] || '');
if (!/^\d+$/.test(ts) || Math.abs(Date.now() / 1000 - Number(ts)) > 300) throw new Error('stale');
const expected = 'sha256=' + crypto.createHmac('sha256', SECRET).update(ts + '.' + raw).digest('hex');
const a = Buffer.from(expected), b = Buffer.from(String(h['x-woow-signature'] || ''));
if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) throw new Error('bad signature');
return [{ json: JSON.parse(raw) }];
```

### Response

HTTP 200 with JSON, synchronously (the call waits up to *Timeout*; see the
Cloudflare note above):

```json
{"reply": {"text": "markdown 文字", "attachments": [
  {"name": "報價.pdf", "mimetype": "application/pdf", "data_base64": "JVBERi0xLjQK…"},
  {"name": "圖表.png", "url": "https://files.example.com/chart.png"}
]}}
```

```json
{"replies": [{"text": "第一則"}, {"text": "第二則"}]}
```

```json
{"skip": true}
```

- `reply` → one message; `replies` → several messages (at most 10); `skip` →
  no message at all.
- For convenience a bare string (`{"reply": "text"}`) and an n8n item list
  (`[{"reply": …}]`) are accepted too.
- Anything else (non-200, not JSON, no `reply`/`replies`/`skip`, empty reply)
  is an error: the user gets the error message, the call log keeps the status.
  **When the agent fails, answer non-200** (for example 502 with
  `{"error": "agent_failed"}`), not 200 with an apology text: only then do
  administrators see the failure in the call logs.

### Minimal n8n workflow

1. **Webhook** node — HTTP Method `POST`, Path e.g. `ai-contact`,
   Authentication `None`, option *Raw Body* on, Respond
   `Using 'Respond to Webhook' Node`.
2. **Code** node — the signature check above. It throws on a bad signature,
   so nothing else runs and n8n answers a non-200 status (or route failures to
   a *Respond to Webhook* node with status 401).
3. **AI Agent** node — Prompt (user message) `{{ $json.message.text }}`;
   system message `{{ $json.system_prompt || 'You are a helpful assistant.' }}`;
   chat model of your choice; optional memory keyed by
   `{{ $json.session_id }}`; tools such as the Odoo MCP client.
4. **Respond to Webhook** node — Respond With `JSON`, Response Body:

```
{{ JSON.stringify({ reply: { text: $json.output } }) }}
```

Put the production webhook URL into the AI contact's *Endpoint URL* and the
signing secret into *Signing Secret*. n8n stores executions with their webhook
body (the conversation): set an execution retention period, or turn off saving
successful executions.

## Reply rendering

Both backends' text is markdown, rendered to safe HTML by the module (no
external library; everything is escaped first, then sanitized):

- `<think>…</think>` and `<thinking>…</thinking>` blocks (also unclosed, or
  reasoning ending with a lone closing tag) and special tokens such as
  `<|eot_id|>` are removed first
- paragraphs and line breaks, `**bold**`, `*italic*`, `~~strike~~`, `` `code` ``,
  fenced code blocks, headings (`#`…`######` → h3–h5), bullet / numbered lists
  (nested by indentation), `>` quotes, `---` rules
- GFM tables → `<table class="table table-sm table-bordered">`
- links: only `http`, `https` and `mailto`; bare URLs are linked. A bridge may
  restrict clickable links to some hosts (Live Chat: the website's own
  domains, against prompt-injected phishing links); other links then show as
  plain text
- HTML in the reply is shown as text, never interpreted

Files in replies:

- `![alt](url)` images and links to files (`png jpg jpeg gif webp mp4 webm mov
  pdf xlsx xls csv docx pptx zip`) whose host is in the AI contact's
  *Download Domains* are downloaded (`https` only; plain `http` only for
  allowlisted `localhost` / `*.svc.cluster.local` hosts; no redirect outside
  the allowlist; size limit *Max File Size*, default 20 MB) and attached to
  the message; the link in the text is replaced by the file name. Other links
  stay links.
- `data:image/(png|jpeg|gif|webp);base64,…` URIs in the text (as
  `![alt](data:…)` or bare, as Hermes produces for images it generated) are
  decoded and attached **without** the domain allowlist (nothing is
  downloaded), with the same size limit; the content must really be one of
  those image types. Refused or other `data:` URIs are replaced by a short note.
- `MEDIA:<path>` left in the text (a file Hermes could not send) is replaced
  by a note "the AI produced a file that could not be delivered"; the call log
  records it in *Error*.
- `attachments[].data_base64` (n8n) is decoded with the same size limit;
  `attachments[].url` follows the allowlist rule (else it is listed as a link).
- At most 10 files per reply.
- The mimetype is detected from the content, never taken from the backend.
  Active types are stored as `application/octet-stream`: `text/html`,
  `application/xhtml+xml`, `image/svg+xml`, `text/xml`, `application/xml`,
  any `…+xml`, and JavaScript / ECMAScript types. Office files (xlsx, docx,
  pptx) keep their own types.

## Push API (AI writes first)

```http
POST /woow_ai/v1/push HTTP/1.1
Host: odoo.example.com
Authorization: Bearer wpk_REPLACE-ME
Content-Type: application/json
```

```json
{"to": {"user_id": 7},
 "text": "**提醒**：您的訂單已出貨。",
 "attachments": [{"name": "出貨單.pdf", "data_base64": "JVBERi0xLjQK…"}]}
```

`to` is exactly one of `{"user_id": 7}` (recommended: take it from the webhook
payload's `user.user_id`), `{"partner_id": 15}` (the user's partner) or
`{"email": "user@example.com"}` (must match exactly one user). The push key is
per AI contact: *Generate Push Key* on the AI contact form shows it once; only
its SHA-256 is stored. The text follows the reply rendering rules above.

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"ok": true, "channel_id": 42, "message_id": 1001}` | posted (the chat is created when needed) |
| 400 | `{"ok": false, "error": "invalid_json"}` | the body is not JSON |
| 400 | `{"ok": false, "error": "invalid_body"}` | not an object, or `to` / `text` / `attachments` of the wrong type |
| 400 | `{"ok": false, "error": "too_large"}` | body over 80 MB |
| 400 | `{"ok": false, "error": "empty_message"}` | no text and no usable attachment |
| 400 | `{"ok": false, "error": "invalid_recipient"}` | `to` is not exactly one valid key |
| 400 | `{"ok": false, "error": "ambiguous_recipient"}` | the email matches several users |
| 401 | `{"ok": false, "error": "invalid_key"}` | missing / wrong key, or the AI contact is archived |
| 403 | `{"ok": false, "error": "not_allowed"}` | the user is not in the AI contact's allowed groups |
| 404 | `{"ok": false, "error": "user_not_found"}` | no active user matches `to` (a partner without user included) |

The route uses `auth='none'`: the database must be resolvable from the host
(single database or `dbfilter`), as on WoowTech PaaS tenants. A push never
triggers an AI reply. There is no rate limit.

```bash
curl -sS https://odoo.example.com/woow_ai/v1/push \
  -H 'Authorization: Bearer wpk_REPLACE-ME' -H 'Content-Type: application/json' \
  -d '{"to": {"user_id": 7}, "text": "您好，這是一則測試推送。"}'
```

## Test Connection (administrators)

The AI contact form has a *Test Connection* button (system administrators
only). It never runs the AI:

- `openai`: `GET {endpoint_url}/models` with the API key (a chat completion
  would start a Hermes agent and its tools).
- `n8n`: a signed `{"event": "ping"}` POST; any HTTP 200 is a success.

Only the HTTP status and one sentence are shown, never the upstream answer.

## Signed attachment URLs

`GET /woow_ai/attachment/<attachment id>/<token>` where
`token = <expires unix>.<hex HMAC-SHA256(database.secret, "woow_ai|<id>|<checksum>|<expires>")>`.
Valid for one hour, only for attachments of messages in an AI chat, served as a
download (`Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`,
`Cache-Control: private, no-store`). Anything else answers 404.
