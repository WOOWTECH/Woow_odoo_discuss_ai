# -*- coding: utf-8 -*-
from urllib.parse import quote

from odoo import http, _
from odoo.http import request
from odoo.osv import expression

from odoo.addons.portal.controllers.portal import CustomerPortal

AI_TAB = 'ai'
AI_TAB_URL = '/my/discussions?filterby=%s' % AI_TAB


class WoowAiPortalDiscussions(CustomerPortal):
    """Loaded after woow_ai_contact and cs_portal_discuss (depends on both):
    first in the combined portal controller, so these overrides apply."""

    def _woow_ai_partner_ids(self):
        # archived contacts too: their chats keep their history
        return request.env['woow.ai.contact'].sudo().with_context(active_test=False).search([]).partner_id.ids

    def _portal_discussion_filters(self):
        filters = super()._portal_discussion_filters()
        with_ai = [('partner_id', 'in', self._woow_ai_partner_ids())]
        filters[AI_TAB] = {
            'label': _('AI Assistants'),
            'domain': [('channel_type', '=', 'chat'), ('channel_member_ids', 'any', with_ai)],
            'sequence': 10,
        }
        if 'chat' in filters:
            # Direct Message: people only
            filters['chat'] = dict(filters['chat'], domain=expression.AND([
                filters['chat']['domain'], [('channel_member_ids', 'not any', with_ai)]]))
        return filters

    def _prepare_portal_discussion_rows(self, channels, back_url=None):
        rows = super()._prepare_portal_discussion_rows(channels, back_url)
        ai_partner_ids = set(self._woow_ai_partner_ids())
        for row in rows:
            channel = row['channel']
            if channel.channel_type == 'chat' and ai_partner_ids & set(channel.channel_member_ids.partner_id.ids):
                row['type_label'] = _('AI Assistant')
        return rows

    @http.route()
    def portal_discussions(self, page=1, sortby=None, filterby=None, search=None, search_in='all', **kwargs):
        response = super().portal_discussions(
            page=page, sortby=sortby, filterby=filterby, search=search, search_in=search_in, **kwargs)
        if filterby == AI_TAB and getattr(response, 'qcontext', None) is not None:
            response.qcontext['woow_ai_contacts_to_start'] = self._woow_ai_contacts_to_start(search)
        return response

    def _woow_ai_contacts_to_start(self, search=None):
        """AI contacts the user may use but has no open chat with (never
        opened, or closed): offered below the AI tab's list."""
        user = request.env.user
        contacts = request.env['woow.ai.contact']._contacts_for_user(user)
        open_chats = request.env['discuss.channel'].sudo().search(expression.AND([
            self._get_portal_discussion_domain(), self._portal_discussion_filters()[AI_TAB]['domain']]))
        chatting = set(open_chats.channel_member_ids.partner_id.ids)
        contacts = contacts.filtered(lambda contact: contact.partner_id.id not in chatting)
        if search:
            contacts = contacts.filtered(lambda contact: search.casefold() in (contact.name or '').casefold())
        return contacts

    @http.route()
    def portal_my_ai_contacts(self, **_kw):
        # one portal entry: the AI contacts are a tab of the discussion list
        return request.redirect(AI_TAB_URL)

    def _woow_ai_portal_chat_url(self, chat):
        # cs_portal_discuss back bar (?discussions=1), back to the AI tab
        return '/discuss/channel/%s?discussions=1&back=%s' % (chat.id, quote(AI_TAB_URL, safe='/'))
