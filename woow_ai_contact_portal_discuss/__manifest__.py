# -*- coding: utf-8 -*-
{
    'name': 'AI Contacts in Portal Discussions',
    'version': '18.0.1.0.0',
    'category': 'Productivity/Discuss',
    'summary': 'One portal entry: the AI contacts become a tab of the portal discussion list',
    'description': """
AI Contacts in Portal Discussions
=================================

Bridge between "AI Contacts for Discuss" (woow_ai_contact) and the portal
discussion list of cs_portal_discuss. Installed automatically when both are
installed.

* One entry on the portal home: the "Discussions" card; the "AI Contacts"
  card is hidden
* /my/discussions gets an "AI Assistants" tab (?filterby=ai) next to
  All / Direct Message / Group / Channel; direct messages with AI contacts
  are listed there (and no longer under Direct Message)
* The AI tab also offers the AI contacts the user has no open chat with
  ("More AI assistants")
* /my/ai leads to the AI tab; a chat opened from it has the portal back bar,
  which returns to the AI tab
    """,
    'author': 'WOOWTECH',
    'website': 'https://github.com/WOOWTECH/Woow_odoo_discuss_ai',
    'license': 'LGPL-3',
    'depends': ['woow_ai_contact', 'cs_portal_discuss'],
    'data': [
        'views/portal_templates.xml',
    ],
    'installable': True,
    'auto_install': True,
    'application': False,
}
