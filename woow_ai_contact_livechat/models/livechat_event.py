# -*- coding: utf-8 -*-
"""Events of a Live Chat conversation the model must know as facts.

The handoff notice and the paused notice are posted as the AI contact's bot,
so in the history they looked like the model's own words: after "立即恢復
AI" the model told the visitor that "已通知客服" was not true, although the
operators really were alerted (real-LLM acceptance, 10-31). Each such notice,
and each pause / resume by an operator, is recorded here; the bridge drops
the notice messages from the history and gives the events to the model in
the system message (core hook ``_woow_ai_conversation_events``).
"""

from odoo import fields, models


class WoowAiLivechatEvent(models.Model):
    _name = 'woow.ai.livechat.event'
    _description = 'AI Live Chat Conversation Event'
    _order = 'id'

    channel_id = fields.Many2one('discuss.channel', string='Conversation', required=True, index=True,
                                 ondelete='cascade')
    kind = fields.Selection([
        ('handoff', 'Handed over to a human'),
        ('paused_notice', 'Paused notice'),
        ('pause', 'AI paused by an operator'),
        ('resume', 'AI resumed by an operator'),
    ], string='Event', required=True)
    message_id = fields.Many2one('mail.message', string='Notice', index='btree_not_null', ondelete='set null',
                                 help='The notice the AI posted for this event (left out of the history).')
    trigger_message_id = fields.Many2one('mail.message', string='Visitor Message', ondelete='set null')
    user_id = fields.Many2one('res.users', string='Operator', ondelete='set null')
    minutes = fields.Integer(string='Pause (minutes)')
    source = fields.Selection([
        ('keyword', 'Handoff phrase'),
        ('backend', 'AI backend'),
    ], string='Decided By', help='Handoff: a handoff phrase of the channel, or the AI backend (n8n '
                                  '"handoff": true or the [[handoff]] marker). Empty before 18.0.1.2.0: a phrase.')
    reason = fields.Char(string='Reason', help='Handoff reason given by the backend (n8n "handoff_reason").')
