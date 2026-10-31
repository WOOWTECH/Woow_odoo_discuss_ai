# -*- coding: utf-8 -*-
"""18.0.1.1.0: conversation events (woow.ai.livechat.event).

Handoff notices posted before the upgrade become events too (from the done
handoff jobs that still exist), so conversations in progress stop showing
them to the model as its own words.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        INSERT INTO woow_ai_livechat_event
               (channel_id, kind, message_id, trigger_message_id, minutes,
                create_uid, create_date, write_uid, write_date)
        SELECT j.channel_id, 'handoff', j.reply_message_id, j.message_id, 0,
               1, COALESCE(m.create_date, j.write_date), 1, COALESCE(m.create_date, j.write_date)
          FROM woow_ai_job j
          JOIN discuss_channel c ON c.id = j.channel_id AND c.channel_type = 'livechat'
          LEFT JOIN mail_message m ON m.id = j.reply_message_id
         WHERE j.kind = 'handoff' AND j.state = 'done' AND j.reply_message_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM woow_ai_livechat_event e WHERE e.message_id = j.reply_message_id)
    """)
    _logger.info("woow_ai_contact_livechat 18.0.1.1.0: %s handoff notice(s) recorded as events", cr.rowcount)
