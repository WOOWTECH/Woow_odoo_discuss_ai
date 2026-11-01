# -*- coding: utf-8 -*-
{
    'name': 'AI Contacts for Discuss',
    'version': '18.0.1.5.0',
    'category': 'Productivity/Discuss',
    'summary': 'Chat one-to-one with AI contacts (Hermes, LiteLLM, n8n) in Discuss and the portal',
    'description': """
AI Contacts for Discuss
=======================

Internal and portal users chat one-to-one with "AI contacts" in Discuss.
Each AI contact is a bot partner connected to an OpenAI compatible backend
(Hermes, LiteLLM, any /v1/chat/completions) or an n8n webhook.

* Durable reply queue: replies are produced by a scheduled action in the cron
  process (never in a web worker), one at a time per conversation, several
  conversations in parallel, recovered after a restart
* "typing" indicator while the backend answers, 5 minute default timeout;
  OpenAI compatible answers streamed (SSE) so proxies do not cut long answers
* Attachments both ways: user images inlined for OpenAI compatible backends,
  documents (PDF, xlsx, xls, docx, csv, txt, md, json) sent as they are as
  file parts (or as links for Hermes), other files as signed 1-hour download
  links; Odoo converts nothing; files in
  AI replies are attached from allowlisted domains
* Markdown replies (tables, lists, code) rendered to safe HTML
* Push API: backends can message users first (one push key per AI contact)
* Like OdooBot: the chats with the AI contacts a user may use are created
  (pinned, unread welcome message) the first time they open Discuss or the
  portal home; "Start a conversation" finds them; "bot" presence
* Portal /my/ai entry; settings under Discuss > Configuration
* Call logs for administrators (no conversation content unless enabled)

See README.md and docs/PROTOCOL.md in the repository.
    """,
    'author': 'WOOWTECH',
    'website': 'https://github.com/WOOWTECH/Woow_odoo_discuss_ai',
    'license': 'LGPL-3',
    'external_dependencies': {'python': ['requests']},
    'depends': ['mail', 'portal'],
    'data': [
        'security/ir.model.access.csv',
        'security/security.xml',
        'data/data.xml',
        'views/ai_contact_views.xml',
        'views/ai_job_views.xml',
        'views/ai_call_log_views.xml',
        'views/menus.xml',
        'views/portal_templates.xml',
    ],
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'auto_install': False,
    'application': False,
}
