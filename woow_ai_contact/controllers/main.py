# -*- coding: utf-8 -*-
import json
import logging
import time

from werkzeug.exceptions import NotFound

from odoo import http, SUPERUSER_ID
from odoo.http import request

from odoo.addons.mail.tools.discuss import Store

from ..tools import media

_logger = logging.getLogger(__name__)

MAX_PUSH_BODY_BYTES = 80 * 1024 * 1024  # base64 attachments included


def _json_response(data, status=200):
    return request.make_json_response(data, status=status)


class WoowAiContactController(http.Controller):

    # --- Backend Discuss sidebar ---

    @http.route('/woow_ai/contacts', type='json', auth='user', readonly=True)
    def woow_ai_contacts(self):
        user = request.env.user
        result = []
        for contact in request.env['woow.ai.contact']._contacts_for_user(user):
            chat = contact._find_chat(user.partner_id)
            result.append({
                'id': contact.id,
                'name': contact.name,
                'description': contact.description or '',
                'partner_id': contact.partner_id.id,
                'channel_id': chat.id or False,
            })
        return result

    @http.route('/woow_ai/open', type='json', auth='user')
    def woow_ai_open(self, contact_id):
        user = request.env.user
        contact = request.env['woow.ai.contact'].sudo().browse(int(contact_id)).exists()
        if not contact or not contact.active or not contact._user_allowed(user):
            raise NotFound()
        chat, _created = contact._get_or_create_chat(user)
        channel = request.env['discuss.channel'].browse(chat.id)  # as the user, for the store
        return {'channel_id': channel.id, 'store_data': Store(channel).get_result()}

    # --- Signed download of user attachments (for the backends) ---

    @http.route('/woow_ai/attachment/<int:attachment_id>/<string:token>', type='http', auth='public',
                methods=['GET'], readonly=True)
    def woow_ai_attachment(self, attachment_id, token):
        env = request.env(user=SUPERUSER_ID, su=True)
        attachment = env['ir.attachment'].browse(attachment_id).exists()
        if not attachment:
            raise NotFound()
        secret = env['woow.ai.contact']._signing_secret()
        if not media.check_token(secret, attachment.id, attachment.checksum, token):
            raise NotFound()
        message = env['mail.message'].search([
            ('attachment_ids', 'in', attachment.ids), ('model', '=', 'discuss.channel'),
        ], limit=1)
        channel = env['discuss.channel'].browse(message.res_id).exists() if message else None
        if not channel or not channel._woow_ai_channel_contact():
            raise NotFound()
        stream = env['ir.binary']._get_stream_from(attachment, 'raw')
        response = stream.get_response(as_attachment=True)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    # --- Push API ---

    @http.route('/woow_ai/v1/push', type='http', auth='none', methods=['POST'], csrf=False, save_session=False,
                readonly=False)
    def woow_ai_push(self, **_kwargs):
        if not request.db:
            raise NotFound()
        env = request.env(user=SUPERUSER_ID, su=True)
        started = time.time()
        auth = request.httprequest.headers.get('Authorization') or ''
        key = auth[7:].strip() if auth[:7].lower() == 'bearer ' else ''
        contact = env['woow.ai.contact']._find_by_push_key(key)
        if not contact or not contact.active:
            return _json_response({'ok': False, 'error': 'invalid_key'}, status=401)

        def fail(status, code, detail=None):
            env['woow.ai.contact'].browse(contact.id)._create_call_log(
                None, None, {'status': 'error', 'http_status': status, 'error': 'push: %s' % code,
                             'request': None, 'response': None},
                int((time.time() - started) * 1000), backend_type='push')
            body = {'ok': False, 'error': code}
            if detail:
                body['detail'] = detail
            return _json_response(body, status=status)

        raw = request.httprequest.get_data()  # cached: Odoo may retry the request
        if len(raw) > MAX_PUSH_BODY_BYTES:
            return fail(400, 'too_large')
        try:
            data = json.loads(raw or b'{}')
        except ValueError:
            return fail(400, 'invalid_json')
        if not isinstance(data, dict):
            return fail(400, 'invalid_body')
        to = data.get('to')
        text = data.get('text') or ''
        attachments = data.get('attachments') or []
        if not isinstance(to, dict) or not isinstance(text, str) or not isinstance(attachments, list):
            return fail(400, 'invalid_body')
        if not text.strip() and not attachments:
            return fail(400, 'empty_message')
        user, error = self._push_recipient(env, to)
        if error:
            return fail(400 if error in ('invalid_recipient', 'ambiguous_recipient') else 404, error)
        if not contact._user_allowed(user):
            return fail(403, 'not_allowed')
        # rendered with the default rules (the chat may not exist yet)
        prepared = contact._prepare_reply({'text': text, 'attachments': attachments})
        if not str(prepared['html']).strip() and not prepared['files']:
            return fail(400, 'empty_message', 'no text and no usable attachment')
        chat, _created = contact._get_or_create_chat(user)
        message = contact._post_prepared(chat, prepared)
        warnings = '; '.join(prepared.get('warnings') or []) or None
        contact._create_call_log(chat, None, {'status': 'ok', 'http_status': 200, 'error': warnings,
                                              'request': None, 'response': None},
                                 int((time.time() - started) * 1000), backend_type='push')
        return _json_response({'ok': True, 'channel_id': chat.id, 'message_id': message.id})

    @staticmethod
    def _push_recipient(env, to):
        """`to` object -> (res.users, error code or None)."""
        Users = env['res.users']
        keys = [k for k in ('user_id', 'partner_id', 'email') if to.get(k) not in (None, '', False)]
        if len(keys) != 1:
            return Users, 'invalid_recipient'
        key = keys[0]
        value = to[key]
        if key in ('user_id', 'partner_id'):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                return Users, 'invalid_recipient'
            if key == 'user_id':
                users = Users.browse(value).exists()
            else:
                users = Users.search([('partner_id', '=', value)], limit=1)
        else:
            if not isinstance(value, str) or '@' not in value:
                return Users, 'invalid_recipient'
            users = Users.search([('email', '=ilike', value.strip())], limit=2)
            if len(users) > 1:
                return Users, 'ambiguous_recipient'
        users = users.filtered(lambda u: u.active and not u._is_public())
        if not users:
            return Users, 'user_not_found'
        return users[:1], None
