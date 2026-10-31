# -*- coding: utf-8 -*-
from odoo import fields, models


class WoowAiLivechatSwitchWizard(models.TransientModel):
    """Switch one Live Chat channel from im_livechat_ai to its AI contact."""
    _name = 'woow.ai.livechat.switch.wizard'
    _description = 'Switch a Live Chat channel from im_livechat_ai'

    livechat_channel_id = fields.Many2one('im_livechat.channel', string='Channel', required=True, readonly=True,
                                          ondelete='cascade')
    contact_id = fields.Many2one(related='livechat_channel_id.woow_ai_contact_id', string='AI Contact')
    legacy_enabled = fields.Boolean(related='livechat_channel_id.woow_ai_legacy_enabled')
    migration_note = fields.Text(related='livechat_channel_id.woow_ai_migration_note')
    price_regression_confirmed = fields.Boolean(
        string='The price regression passed on the new backend',
        help='im_livechat_ai looked prices up with its own product tools; the AI contact relies on its backend '
             '(Hermes / n8n with Odoo MCP). Ask the price questions of docs/LIVECHAT.md as a guest first: every '
             'price right, no invented price, links to the website only.')

    def action_switch(self):
        self.ensure_one()
        self.livechat_channel_id._woow_ai_check_system()
        result = self.env['woow.ai.livechat.migration']._switch(
            self.livechat_channel_id, price_regression_confirmed=self.price_regression_confirmed)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._('Switched from im_livechat_ai'),
                'message': self.env._('%(moved)s waiting job(s) moved; %(busy)s conversation(s) were being answered '
                                      'by im_livechat_ai and finish there.', moved=result['moved'], busy=result['busy']),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
