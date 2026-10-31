# Backend protocol

How `woow_ai_contact` talks to an AI backend, and how a backend talks back
(push API). Everything below uses placeholders: `odoo.example.com`,
`llm.example.com`, `n8n.example.com`, `sk-REPLACE-ME`, `wpk_REPLACE-ME`.

Each AI contact has one backend:

| `backend_type` | Endpoint URL (AI contact form) | Typical backends |
|---|---|---|
| `openai` | base URL ending in `/v1`; `/chat/completions` is appended | Hermes, LiteLLM, any OpenAI-compatible server |
| `n8n` | full webhook URL | n8n "Webhook" node (production URL) |

The endpoint must be `https://`. Plain `http://` is accepted only for
`localhost`, `127.0.0.1` and in-cluster hosts ending in `.svc.cluster.local`.

## Common context

Every call carries:

- **history**: the most recent `max_history` messages of the chat (default 50),
  oldest first. Messages of the AI contact are `assistant`, everything else is
  `user`. Messages without text and without attachments are left out.
- **system prompt**: the AI contact's prompt, or nothing when it is empty
  (then the backend decides the persona).
- **session id**: `woow-ai:<first 8 hex of the database uuid>:<discuss.channel id>`.
  It is stable for one user/contact chat, so a backend can keep its own memory.
- **identity** of the person the AI talks to (no login is delegated):

```json
{"user_id": 7, "partner_id": 15, "name": "王小明", "email": "user@example.com",
 "lang": "zh_TW", "tz": "Asia/Taipei", "is_portal": true, "company_id": 1}
```

- **user attachments** as `ATT` objects. `url` is a signed download link valid
  for one hour (`GET`, no cookie needed; only attachments of messages in AI
  chats are ever signed):

```json
{"id": 12, "name": "報價.xlsx", "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
 "size": 12345, "url": "https://odoo.example.com/woow_ai/attachment/12/1793433600.4f1c…",
 "expires_at": "2026-10-31T09:00:00Z"}
```

Timeouts: 10 s to connect, then the AI contact's `timeout_seconds` (default
300 s) to read the answer. A failed *connection* is retried once after 3 s; a
read timeout or an HTTP error is not retried (the user gets the AI contact's
error message). While the backend works the bot shows "typing…".

## `openai` backend

Request (`POST {endpoint_url}/chat/completions`):

```http
POST /v1/chat/completions HTTP/1.1
Host: llm.example.com
Authorization: Bearer sk-REPLACE-ME
Content-Type: application/json
```

```json
{
  "model": "gpt-example",
  "messages": [
    {"role": "system", "content": "你是 WoowTech 的產品顧問。"},
    {"role": "assistant", "content": "您好，我是產品顧問，請問需要什麼協助？"},
    {"role": "user", "content": "[附件] 規格.pdf（application/pdf，120.4 KB）：https://odoo.example.com/woow_ai/attachment/31/1793433600.9be2…"},
    {"role": "user", "content": [
      {"type": "text", "text": "這個型號多少錢？"},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo…"}}
    ]}
  ],
  "user": "woow-ai:0edfec80:42",
  "metadata": {"session_id": "woow-ai:0edfec80:42", "odoo_user": {"user_id": 7, "partner_id": 15, "name": "王小明", "email": "user@example.com", "lang": "zh_TW", "tz": "Asia/Taipei", "is_portal": true, "company_id": 1}}
}
```

- `model` is sent only when the AI contact has one (Hermes can use its own default).
- The `system` message is sent only when the prompt is not empty.
- Images (`image/png`, `image/jpeg`, `image/gif`, `image/webp`, at most 5 MB)
  are inlined as data URIs **only for the 3 most recent user messages**. Older
  images and every other file become a text line
  `[附件] <name>（<mimetype>，<size>）：<signed url>`.
- No `tools` are sent: tools (e.g. the Odoo MCP server) belong to the backend.
- `metadata` is informational; LiteLLM and Hermes ignore what they do not use.

Response: a normal chat completion. The reply text is
`choices[0].message.content` (a string, or a list of `{"type": "text"}` parts).
`<think>…</think>` blocks are removed.

```json
{"choices": [{"message": {"role": "assistant",
  "content": "這款目前售價如下：\n\n| 型號 | 價格 |\n|---|---:|\n| A1 | NT$1,200 |\n\n![規格圖](https://files.example.com/a1.png)"}}]}
```

## `n8n` backend

Request (`POST {endpoint_url}`), JSON body (UTF-8, compact):

```http
POST /webhook/ai-contact HTTP/1.1
Host: n8n.example.com
Authorization: Bearer sk-REPLACE-ME
X-Woow-Timestamp: 1793430000
X-Woow-Signature: sha256=5d2b…
Content-Type: application/json
```

```json
{
  "event": "message",
  "session_id": "woow-ai:0edfec80:42",
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
the earlier context. Several quick messages in a row get **one** call, with
all of them in the context.

**Verifying the call** (recommended): `X-Woow-Signature` is
`sha256=` + hex HMAC-SHA256 of `"<X-Woow-Timestamp>.<raw request body>"`, keyed
with the AI contact's API key (the same value as the Bearer token). Reject
timestamps older than a few minutes. In n8n the Webhook node can check the
`Authorization` header (Header Auth credential); verify the signature in a
Code node if you need replay protection (enable "Raw Body" on the Webhook node).

Response: HTTP 200 with JSON, synchronously (the call waits up to
`timeout_seconds`):

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

### Minimal n8n workflow

1. **Webhook** node — HTTP Method `POST`, Path e.g. `ai-contact`,
   Authentication `Header Auth` (name `Authorization`, value `Bearer sk-REPLACE-ME`),
   Respond `Using 'Respond to Webhook' Node`.
2. **AI Agent** node — Prompt (user message) `{{ $json.body.message.text }}`;
   system message `{{ $json.body.system_prompt || 'You are a helpful assistant.' }}`;
   chat model of your choice; optional memory keyed by
   `{{ $json.body.session_id }}`; tools such as the Odoo MCP client.
3. **Respond to Webhook** node — Respond With `JSON`, Response Body:

```
{{ JSON.stringify({ reply: { text: $json.output } }) }}
```

Put the production webhook URL into the AI contact's *Endpoint URL* and the
same secret (without `Bearer `) into *API Key*.

## Reply rendering

Both backends' text is markdown, rendered to safe HTML by the module (no
external library; everything is escaped first, then sanitized):

- paragraphs and line breaks, `**bold**`, `*italic*`, `~~strike~~`, `` `code` ``,
  fenced code blocks, headings (`#`…`######` → h3–h5), bullet / numbered lists
  (nested by indentation), `>` quotes, `---` rules
- GFM tables → `<table class="table table-sm table-bordered">`
- links: only `http`, `https` and `mailto`; bare URLs are linked
- HTML in the reply is shown as text, never interpreted

Files in replies:

- `![alt](url)` images and links to files (`png jpg jpeg gif webp mp4 webm mov
  pdf xlsx xls csv docx pptx zip`) whose host is in the AI contact's
  *Download Domains* are downloaded (no redirect outside the allowlist, size
  limit *Max File Size*, default 20 MB) and attached to the message; the link
  in the text is replaced by the file name. Other links stay links.
- `attachments[].data_base64` (n8n) is decoded with the same size limit;
  `attachments[].url` follows the allowlist rule (else it is listed as a link).
- The mimetype is detected from the content, never taken from the backend.
  HTML, SVG, XML and JavaScript are stored as `application/octet-stream`.

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

`to` is exactly one of `{"user_id": 7}`, `{"partner_id": 15}` (the user's
partner) or `{"email": "user@example.com"}` (must match one user). The push key
is per AI contact: *Generate Push Key* on the AI contact form shows it once;
only its SHA-256 is stored.

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"ok": true, "channel_id": 42, "message_id": 1001}` | posted (the chat is created when needed) |
| 400 | `{"ok": false, "error": "invalid_json" \| "invalid_body" \| "invalid_recipient" \| "ambiguous_recipient" \| "empty_message" \| "too_large"}` | bad request |
| 401 | `{"ok": false, "error": "invalid_key"}` | missing / wrong key, or the AI contact is archived |
| 403 | `{"ok": false, "error": "not_allowed"}` | the user is not in the AI contact's allowed groups |
| 404 | `{"ok": false, "error": "user_not_found"}` | no active user matches `to` |

The route uses `auth='none'`: the database must be resolvable from the host
(single database or `dbfilter`), as on WoowTech PaaS tenants. A push never
triggers an AI reply. There is no rate limit.

```bash
curl -sS https://odoo.example.com/woow_ai/v1/push \
  -H 'Authorization: Bearer wpk_REPLACE-ME' -H 'Content-Type: application/json' \
  -d '{"to": {"email": "user@example.com"}, "text": "您好，這是一則測試推送。"}'
```

## Signed attachment URLs

`GET /woow_ai/attachment/<attachment id>/<token>` where
`token = <expires unix>.<hex HMAC-SHA256(database.secret, "woow_ai|<id>|<checksum>|<expires>")>`.
Valid for one hour, only for attachments of messages in an AI chat, served as a
download (`Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`,
`Cache-Control: private, no-store`). Anything else answers 404.
