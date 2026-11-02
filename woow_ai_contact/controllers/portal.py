# -*- coding: utf-8 -*-
from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import request

from odoo.addons.portal.controllers.portal import CustomerPortal


class WoowAiContactPortal(CustomerPortal):

    def _prepare_portal_layout_values(self):
        # like the backend (res.users._init_messaging): any portal page (/my,
        # /my/ai, the discussion list of an optional portal Discuss module, ...)
        # first creates the chats with the AI contacts the user may use, so
        # they are there whichever page the user opens first
        user = request.env.user
        if not user._is_public():
            user._woow_ai_onboard()
        return super()._prepare_portal_layout_values()

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if 'woow_ai_contact_count' in counters:
            values['woow_ai_contact_count'] = len(
                request.env['woow.ai.contact']._contacts_for_user(request.env.user))
        return values

    @http.route('/my/ai', type='http', auth='user', website=True)
    def portal_my_ai_contacts(self, **_kw):
        values = self._prepare_portal_layout_values()
        values.update({
            'page_name': 'woow_ai_contacts',
            'ai_contacts': request.env['woow.ai.contact']._contacts_for_user(request.env.user),
        })
        return request.render('woow_ai_contact.portal_my_ai_contacts', values)

    @http.route('/my/ai/<int:contact_id>/open', type='http', auth='user', website=True)
    def portal_my_ai_contact_open(self, contact_id, **_kw):
        user = request.env.user
        contact = request.env['woow.ai.contact'].sudo().browse(contact_id).exists()
        if not contact or not contact.active or not contact._user_allowed(user):
            raise NotFound()
        chat, _created = contact._get_or_create_chat(user)
        if user._is_internal():
            return request.redirect('/odoo/discuss?active_id=discuss.channel_%s' % chat.id)
        return request.redirect(self._woow_ai_portal_chat_url(chat))

    def _woow_ai_portal_chat_url(self, chat):
        """Where a portal user chats with an AI contact: core Odoo's Discuss
        page. A portal Discuss module can add its own parameters (the bridge
        woow_ai_contact_portal_discuss adds the back bar of cs_portal_discuss)."""
        return '/discuss/channel/%s' % chat.id
