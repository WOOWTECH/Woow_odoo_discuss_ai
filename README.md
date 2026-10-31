# Woow Odoo Discuss AI

Odoo 18 addons that let internal and portal users chat one-to-one with
**AI contacts** in Discuss. An AI contact is a bot partner connected to an
OpenAI-compatible backend (Hermes, LiteLLM, any `/v1/chat/completions`
server) or to an n8n webhook.

[繁體中文說明](README_zh-TW.md)

| Module | Status | What it does |
|---|---|---|
| `woow_ai_contact` | available | AI contacts, backend connections, durable reply queue, files both ways, push API, Discuss sidebar section, portal `/my/ai` |
| `woow_ai_contact_livechat` | available (auto-installed with Live Chat) | Live Chat channels answered by an AI contact: human handoff, pause, `/ai on` resume, staged migration from `im_livechat_ai` (see [docs/LIVECHAT.md](docs/LIVECHAT.md)) |

## Features

- Direct chats with AI contacts from the Discuss sidebar ("AI Contacts"),
  from the portal (`/my` card → `/my/ai`) or from the normal chat search.
- Replies are produced in the background after the user's message is saved,
  by the scheduled action *AI Contacts: Run AI Replies* in the **cron process**
  (never in a web worker, so web worker restarts do not cut long answers):
  one reply per conversation at a time (quick messages get one answer), up to
  4 conversations in parallel, "typing…" while the backend works, 5-minute
  default timeout, jobs resumed after a restart. See *Server requirements*.
- OpenAI-compatible answers are streamed (SSE) by default, so long Hermes tool
  chains are not cut by proxies such as Cloudflare (about 100 s without data).
- Bounded retries: only connection failures and HTTP 429/503 (0–2 times,
  default 1); never a read timeout or a 502/504/524 (tools may be running).
- Users can send images and files: up to 4 images (about 7 MB) of the last 3
  messages are inlined for OpenAI-compatible backends, everything else is a
  signed 1-hour link.
- The model knows who it talks to: a short identity block (name, language,
  time zone, account type; no email) in the system message.
- Replies are markdown (tables, lists, code…) rendered to safe HTML; images and
  files from allowlisted domains, n8n base64 files and inline `data:image`
  URIs are attached.
- Push API: a backend can message a user first (`POST /woow_ai/v1/push`,
  one push key per AI contact, stored hashed).
- Access per AI contact by user groups (default: internal + portal users).
- Call logs for administrators: status, HTTP code, duration, token usage,
  error. The conversation content is stored only while "Log Request Content"
  is on. A *Test Connection* button checks a backend without running the AI.
- No Odoo-side tool calling: tools (for example the Odoo MCP server) live in
  the backend.

## Install

1. Put this repository (or the `woow_ai_contact` folder) in your addons path.
2. Python dependency: `requests` (shipped with Odoo).
3. Install **AI Contacts for Discuss** (`woow_ai_contact`); it depends on
   `mail` and `portal` only. Traditional Chinese (`zh_TW`) translations are
   included.

## Configure

Settings → AI Contacts → AI Contacts → New (system administrators only).

| Field | Meaning |
|---|---|
| Name, image, description | what users see; the bot partner follows them |
| Backend | `OpenAI compatible` or `n8n webhook` |
| Endpoint URL | OpenAI: base URL ending in `/v1`. n8n: the webhook URL. `https` only (plain `http` allowed for `localhost` and `*.svc.cluster.local`) |
| API Key / Signing Secret | write-only; never shown again after saving. OpenAI: the bearer key. n8n: the HMAC signing secret (never sent) |
| Model | OpenAI only, optional |
| System Prompt | optional; empty = the backend decides |
| History Messages / Timeout | default 50 messages / 300 s for the whole answer |
| Stream Replies | OpenAI only, default on: SSE answer, fails after 30 s without any data |
| Retries | 0–2 (default 1), connection failures and HTTP 429/503 only |
| Welcome / Error Message | first message of a new chat / reply when the backend fails |
| Download Domains / Max File Size | hosts whose files in replies are attached (one per line, `*.example.com` allowed) / default 20 MB |
| Allowed Groups | who may chat with it (default Internal User + Portal); empty = no direct chats (Live Chat only), the bot partner stays archived |
| Push API | *Generate Push Key* shows the key once |
| Debug | *Log Request Content* — keep off except while debugging |

### Server requirements (where replies run)

The web request only saves the message and wakes the cron runner; the cron
"AI Contacts: Run AI Replies" waits for the backend in the cron process and
returns only when its replies are posted. The AI contact form (Backend tab,
*Reply Processing*) shows the mode in use.

- **Cron mode** (normal): needs a cron runner (`max_cron_threads` ≥ 1 in this
  server, or crons running in another process of the same database) and a
  cron time limit long enough for the longest *Timeout*:
  `limit_time_real_cron` (or `limit_time_real` when it is `-1`) ≥ longest
  timeout + 60 s, **+ 70 s with `workers` > 0** (a prefork cron worker's idle
  sleep counts on its watchdog). Default timeout 300 s → `limit_time_real_cron
  = 430` or more (e.g. 600). When crons run in another process (web server
  with `max_cron_threads = 0`), the mode follows what that cron process
  reports (its server type and time limit), not the web server's config.
- A pass holds its cron worker: other scheduled actions of that worker (and
  its other databases) wait. With `max_cron_threads = 1`, or one cron process
  for several databases, each pass starts new replies for at most 60 s (the
  form shows a warning); `max_cron_threads` ≥ 2 and one cron process per
  database with AI contacts are recommended. The cron process needs
  `db_maxconn` ≥ concurrency + 5.
- **Fallback (thread) mode**: without a cron runner, or when the cron time
  limit is too short, replies run in a thread of the web worker that saved the
  message (the 18.0.1.0.0 behaviour): a web worker that restarts during a long
  reply cuts it and the reply is processed again. A warning is logged once and
  shown on the form.
- System parameters: `woow_ai_contact.job_runner` = `auto` (default) | `cron` |
  `thread`; `woow_ai_contact.max_concurrency` (default 4, at most 16; also
  limited by free database connections of the cron process).

### Hermes

Endpoint URL `https://<your-hermes-host>/v1` (or the in-cluster
`http://<service>.<namespace>.svc.cluster.local:<port>/v1`), API Key = the
Hermes API server key, Model empty (Hermes ignores it and uses its configured
model). Keep *Stream Replies* on. Hermes keeps its own tools and memory; the
`user` field carries the session id. Hermes does not read `user` or
`metadata`: the identity reaches the model through the system message.

### LiteLLM

Endpoint URL `https://<your-litellm-host>/v1`, API Key = a LiteLLM virtual key
(scope it to the models this contact may use), Model = a LiteLLM model name.

### n8n

Create a workflow *Webhook (Raw Body) → Code (verify signature) → AI Agent →
Respond to Webhook* (see
[docs/PROTOCOL.md](docs/PROTOCOL.md#minimal-n8n-workflow)), then put the
production webhook URL in Endpoint URL and a random secret in Signing Secret.
Odoo sends no `Authorization` header: every call is authenticated only by
`X-Woow-Timestamp` + `X-Woow-Signature` (HMAC-SHA256 over the exact body bytes,
300 s window). Calls through Cloudflare are cut after about 100 s: answer
`{"skip": true}` quickly and reply with the push API for long workflows.

## Protocol

Request/response formats for both backends, reply rendering rules, the push
API and signed attachment URLs: [docs/PROTOCOL.md](docs/PROTOCOL.md).
Design notes (Traditional Chinese): [docs/DESIGN.md](docs/DESIGN.md).

## Privacy and security

- Direct chats are visible only to their members (standard Odoo rules).
  Administrators see call logs, not content, unless they enable content logging
  for an AI contact (turning it off erases the stored content).
- Backend URLs, keys and prompts are restricted to system administrators; the
  API key is kept outside the ORM fields and never sent to a browser.
- Backend replies are untrusted: HTML is escaped, links are limited to
  http/https/mailto, files are downloaded only from allowlisted hosts (https,
  or http to in-cluster hosts) with a size limit, and their type is detected
  from the content (HTML, SVG, XML and scripts become
  `application/octet-stream`).
- The n8n signing secret never leaves Odoo; the push key is stored hashed.

## License

LGPL-3.0 — see [LICENSE](LICENSE).
