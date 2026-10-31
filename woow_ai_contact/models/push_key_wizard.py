# -*- coding: utf-8 -*-
from odoo import fields, models


class WoowAiPushKeyWizard(models.TransientModel):
    """Shows a newly generated push key once. Never saved: the key only exists
    in the action context of the response that generated it."""
    _name = 'woow.ai.push.key.wizard'
    _description = 'AI Contact Push Key'

    contact_id = fields.Many2one('woow.ai.contact', readonly=True)
    push_key = fields.Char(string='Push Key', readonly=True)
