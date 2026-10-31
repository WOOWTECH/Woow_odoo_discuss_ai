# -*- coding: utf-8 -*-
{
    'name': 'AI Contacts for Live Chat',
    'version': '18.0.1.0.0',
    'category': 'Website/Live Chat',
    'summary': 'Let an AI contact answer Live Chat visitors, with human handoff and pause',
    'description': """
AI Contacts for Live Chat
=========================

Bridge between "AI Contacts for Discuss" (woow_ai_contact) and Live Chat.
Installed automatically when both are installed.

* A Live Chat channel picks one AI contact that answers its visitors
  (guests, portal customers, employees chatting on the website)
* Human handoff: an operator's message pauses the AI for the conversation;
  visitor phrases such as "找真人" or "轉人工" send a handoff notice, pause the
  AI and alert the operators (OdooBot message and sticky notification)
* A visitor who writes while nobody answers gets one notice; operators resume
  the AI at once with the "/ai on" command or the session's button
* Operator replies are sent to the backend as colleague replies
  ("[真人客服 name]"); links in AI replies stay clickable only for the
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
