# Woow Odoo Discuss AI

Odoo 18 addons that let internal and portal users chat one-to-one with
**AI contacts** in Discuss. An AI contact is a bot partner connected to an
OpenAI-compatible backend (Hermes, LiteLLM, any `/v1/chat/completions`
server) or to an n8n webhook.

[繁體中文說明](README_zh-TW.md)

| Module | Status | What it does |
|---|---|---|
| `woow_ai_contact` | available | AI contacts, backend connections, durable reply queue, files both ways, push API, Discuss sidebar section, portal `/my/ai` |
| `woow_ai_contact_livechat` | planned | Live Chat channels answered by an AI contact, human handoff (replaces `im_livechat_ai`) |

## Features

- Direct chats with AI contacts from the Discuss sidebar ("AI Contacts"),
  from the portal (`/my` card → `/my/ai`) or from the normal chat search.
- Replies are produced in the background after the user's message is saved:
  one reply per conversation at a time (quick messages get one answer),
  "typing…" while the backend works, 5-minute default timeout, jobs resumed
  by a cron after a restart.
- Users can send images and files: images of the last 3 messages are inlined
  for OpenAI-compatible backends, everything else is a signed 1-hour link.
- Replies are markdown (tables, lists, code…) rendered to safe HTML; images and
  files from allowlisted domains (and n8n base64 files) are attached.
- Push API: a backend can message a user first (`POST /woow_ai/v1/push`,
  one push key per AI contact, stored hashed).
- Access per AI contact by user groups (default: internal + portal users).
- Call logs for administrators: status, HTTP code, duration, error. The
  conversation content is stored only while "Log Request Content" is on.
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
| API Key | write-only; never shown again after saving |
| Model | OpenAI only, optional |
| System Prompt | optional; empty = the backend decides |
| History Messages / Timeout | default 50 messages / 300 s |
| Welcome / Error Message | first message of a new chat / reply when the backend fails |
| Download Domains / Max File Size | hosts whose files in replies are attached (one per line, `*.example.com` allowed) / default 20 MB |
| Allowed Groups | who may chat with it (default Internal User + Portal) |
| Push API | *Generate Push Key* shows the key once |
| Debug | *Log Request Content* — keep off except while debugging |

### Hermes

Endpoint URL `https://<your-hermes-host>/v1` (or the in-cluster
`http://<service>.<namespace>.svc.cluster.local:<port>/v1`), API Key = the
Hermes API server key, Model empty to use Hermes' configured model. Hermes
keeps its own tools and memory; the `user` field carries the session id.

### LiteLLM

Endpoint URL `https://<your-litellm-host>/v1`, API Key = a LiteLLM virtual key
(scope it to the models this contact may use), Model = a LiteLLM model name.

### n8n

Create a workflow *Webhook → AI Agent → Respond to Webhook* (see
[docs/PROTOCOL.md](docs/PROTOCOL.md#minimal-n8n-workflow)), use Header Auth
with `Authorization: Bearer <secret>`, then put the production webhook URL in
Endpoint URL and the same secret in API Key. Calls are also signed
(`X-Woow-Signature`).

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
  http/https/mailto, files are downloaded only from allowlisted hosts with a
  size limit, and their type is detected from the content.

## License

LGPL-3.0 — see [LICENSE](LICENSE).
