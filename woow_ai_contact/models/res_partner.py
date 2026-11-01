# -*- coding: utf-8 -*-
"""Bot partners of AI contacts: "bot" presence and "start a conversation" search."""

from odoo import api, models

from odoo.addons.mail.tools.discuss import Store


class ResPartner(models.Model):
    _inherit = 'res.partner'

    def _compute_im_status(self):
        super()._compute_im_status()
        ids = [pid for pid in self.ids if isinstance(pid, int)]
        if not ids:
            return
        # sudo: woow.ai.contact - which partners are AI bots is not sensitive
        # (archived contacts too: Live Chat-only bots also show as bots)
        self.env['woow.ai.contact'].flush_model(['partner_id'])
        self.env.cr.execute("SELECT partner_id FROM woow_ai_contact WHERE partner_id IN %s", (tuple(ids),))
        bots = {row[0] for row in self.env.cr.fetchall()}
        for partner in self:
            if partner.id in bots:
                partner.im_status = 'bot'

    @api.readonly
    @api.model
    def im_search(self, name, limit=20, excluded_ids=None):
        """Also return the bot partners of the AI contacts the current user may
        use (allowed groups), so "Start a conversation" finds them; opening one
        goes through discuss.channel.channel_get (access check, welcome)."""
        result = super().im_search(name, limit=limit, excluded_ids=excluded_ids)
        bots = self._woow_ai_search_bots(name, limit, excluded_ids)
        if not bots:
            return result
        return self._woow_ai_merge_store(result, Store(bots).get_result())

    @api.model
    def _woow_ai_search_bots(self, name, limit, excluded_ids):
        user = self.env.user
        if not user or user._is_public():
            return self.browse()
        domain = [('partner_id', '!=', False)]
        if name:
            domain.append(('name', 'ilike', name))
        if excluded_ids:
            domain.append(('partner_id', 'not in', list(excluded_ids)))
        contacts = self.env['woow.ai.contact'].sudo().search(domain)
        contacts = contacts.filtered(lambda c: c._user_allowed(user))[:limit or None]
        # sudo: res.partner - the bot partners of AI contacts the user may use are public to them
        return contacts.partner_id.sudo()

    @staticmethod
    def _woow_ai_merge_store(result, extra):
        """Merge two Store results ({model: [records]} / {model: record})."""
        merged = dict(result)
        for key, value in extra.items():
            current = merged.get(key)
            if isinstance(current, list) and isinstance(value, list):
                known = {rec.get('id') for rec in current if isinstance(rec, dict)}
                merged[key] = current + [rec for rec in value
                                         if not (isinstance(rec, dict) and rec.get('id') in known)]
            elif current is None:
                merged[key] = value
        return merged
