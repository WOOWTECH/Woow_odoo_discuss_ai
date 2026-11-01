# -*- coding: utf-8 -*-
"""18.0.1.3.0: chats are created automatically, once per (user, AI contact).

Users who already have a direct chat with an AI contact (opened from the old
Discuss sidebar, the portal or a push) count as onboarded: no second chat and
no second welcome message, and a chat they unpinned stays unpinned.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        INSERT INTO woow_ai_contact_onboarded_user_rel (contact_id, user_id)
        SELECT DISTINCT c.id, u.id
          FROM woow_ai_contact c
          JOIN discuss_channel_member mb ON mb.partner_id = c.partner_id
          JOIN discuss_channel ch ON ch.id = mb.channel_id AND ch.channel_type = 'chat'
          JOIN discuss_channel_member mu ON mu.channel_id = ch.id AND mu.partner_id <> c.partner_id
          JOIN res_users u ON u.partner_id = mu.partner_id
        ON CONFLICT DO NOTHING
    """)
    _logger.info("woow_ai_contact 18.0.1.3.0: %s (user, AI contact) pair(s) with an existing chat marked as "
                 "onboarded", cr.rowcount)
