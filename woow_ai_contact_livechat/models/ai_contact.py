# -*- coding: utf-8 -*-
from odoo import fields, models


class WoowAiContact(models.Model):
    _inherit = 'woow.ai.contact'

    livechat_channel_ids = fields.One2many(
        'im_livechat.channel', 'woow_ai_contact_id', string='Live Chat Channels', readonly=True,
        groups='base.group_system',
        help='Live Chat channels that chose this AI contact. Their visitors (often anonymous) reach '
             'its backend: give it public data tools only.')
