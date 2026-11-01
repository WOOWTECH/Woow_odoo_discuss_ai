# -*- coding: utf-8 -*-
{
    'name': 'AI Contacts for Live Chat',
    'version': '18.0.1.2.0',
    'category': 'Website/Live Chat',
    'summary': 'Let an AI contact answer Live Chat visitors, with human handoff and pause',
    'description': """
AI Contacts for Live Chat
=========================

Bridge between "AI Contacts for Discuss" (woow_ai_contact) and Live Chat.
Installed automatically when both are installed.

* A Live Chat channel picks one AI contact that answers its visitors
  (guests, portal customers, employees chatting on the website)
* Human handoff decided by the backend: the reply ends with "[[handoff]]"
  (openai backends, n8n "output") or n8n answers "handoff": true; the AI
  pauses and the operators are alerted (OdooBot message and sticky
  notification). Optional handoff phrases (empty by default) hand over
  without calling the backend; an operator's message pauses the AI too
* A visitor who writes while nobody answers gets one notice; operators resume
  the AI at once with the "/ai on" command or the session's button
* Operator replies are sent to the backend as labelled colleague replies
  ("[真人客服 name]", never as the AI's words); handoff / paused notices,
  pauses and resumes are given to the model as conversation events (facts),
  not as its own messages; links in AI replies stay clickable only for the
  website's own domains
* Coexists with im_livechat_ai 1.1.0 (a conversation is never answered twice)
  and migrates its channels in stages: prepare on install, switch per channel,
  finish, then uninstall im_livechat_ai

See docs/LIVECHAT.md in the repository.
    """,
    'author': 'WOOWTECH',
    'website': 'https://github.com/WOOWTECH/Woow_odoo_discuss_ai',
    'license': 'LGPL-3',
    'depends': ['woow_ai_contact', 'im_livechat'],
    'data': [
        'security/ir.model.access.csv',
        'security/security.xml',
        'views/im_livechat_channel_views.xml',
        'views/discuss_channel_views.xml',
        'views/ai_contact_views.xml',
        'wizard/switch_wizard_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'woow_ai_contact_livechat/static/src/**/*',
        ],
    },
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'auto_install': True,
    'application': False,
}
