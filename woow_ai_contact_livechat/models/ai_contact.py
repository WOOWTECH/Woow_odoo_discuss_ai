# -*- coding: utf-8 -*-
from odoo import fields, models

from .discuss_channel import HANDOFF_SYSTEM_LINE


class WoowAiContact(models.Model):
    _inherit = 'woow.ai.contact'

    livechat_channel_ids = fields.One2many(
        'im_livechat.channel', 'woow_ai_contact_id', string='Live Chat Channels', readonly=True,
        groups='base.group_system',
        help='Live Chat channels that chose this AI contact. Their visitors (often anonymous) reach '
             'its backend: give it public data tools only.')

    def _openai_messages(self, channel, history, identity, base_url):
        """Live Chat with operators: one system line telling the model how to
        hand over (``[[handoff]]``, owner decision 40), the only handoff
        prompt text Odoo adds; n8n gets ``channel.livechat.handoff_available``."""
        messages = super()._openai_messages(channel, history, identity, base_url)
        if channel.channel_type != 'livechat' or not channel._woow_ai_handoff_available():
            return messages
        if messages and messages[0].get('role') == 'system':
            messages[0] = dict(messages[0], content='%s\n\n%s' % (messages[0]['content'], HANDOFF_SYSTEM_LINE))
            return messages
        return [{'role': 'system', 'content': HANDOFF_SYSTEM_LINE}] + messages
