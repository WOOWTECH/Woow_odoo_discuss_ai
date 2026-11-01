# -*- coding: utf-8 -*-
from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import request

from odoo.addons.portal.controllers.portal import CustomerPortal


class WoowAiContactPortal(CustomerPortal):

    @http.route()
    def home(self, **kw):
        # like the backend (res.users._init_messaging): the first visit of the
        # portal home creates the chats with the AI contacts the user may use
        user = request.env.user
        if not user._is_public():
            user._woow_ai_onboard()
        return super().home(**kw)

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
        # cs_portal_discuss (when installed) reads ?discussions=1; core Odoo would
        # only log a warning about an unknown argument
        suffix = '?discussions=1' if 'cs_portal_discuss' in request.env.registry._init_modules else ''
        return request.redirect('/discuss/channel/%s%s' % (chat.id, suffix))
