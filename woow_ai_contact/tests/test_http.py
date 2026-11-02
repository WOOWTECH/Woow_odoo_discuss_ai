# -*- coding: utf-8 -*-
import base64
import json
import time
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.tests.common import HttpCase, new_test_user, tagged

from odoo.addons.woow_ai_contact.tools import media

from .common import TEST_KEY, TINY_PDF, TINY_PNG


@tagged('post_install', '-at_install', 'woow_ai_contact')
class TestAiContactHttp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.internal = new_test_user(cls.env, 'aih_internal', groups='base.group_user', name='Internal Ivy',
                                     email='ivy@example.com')
        cls.portal = new_test_user(cls.env, 'aih_portal', groups='base.group_portal', name='Portal Pia',
                                   email='pia@example.com')
        cls.contact = cls.env['woow.ai.contact'].create({
            'name': 'HTTP Advisor',
            'backend_type': 'openai',
            'endpoint_url': 'https://llm.example.com/v1',
            'api_key': TEST_KEY,
            'welcome_message': '歡迎使用',
            'allowed_group_ids': [Command.set([cls.env.ref('base.group_user').id, cls.env.ref('base.group_portal').id])],
            'download_domains': 'files.example.com',
        })
        cls.Job = cls.env['woow.ai.job']

    def setUp(self):
        super().setUp()
        # never start real worker threads from HTTP requests in tests
        patcher = patch.object(type(self.env['woow.ai.job']), '_start_after_commit')
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- signed attachment URLs ---

    def _chat_with_attachment(self):
        chat, _created = self.contact._get_or_create_chat(self.internal)
        att = self.env['ir.attachment'].with_user(self.internal).sudo().create({
            'name': '報價.pdf', 'raw': TINY_PDF, 'res_model': 'mail.compose.message', 'res_id': 0})
        chat.with_user(self.internal).message_post(body='see file', message_type='comment',
                                                   subtype_xmlid='mail.mt_comment', attachment_ids=att.ids)
        return chat, att

    def test_signed_attachment_url(self):
        _chat, att = self._chat_with_attachment()
        info = self.env['woow.ai.contact']._attachment_info(att, self.base_url())
        response = self.url_open(info['url'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, TINY_PDF)
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn('attachment', response.headers['Content-Disposition'])
        # tampered / expired / other attachment
        token = info['url'].rsplit('/', 1)[1]
        exp, sig = token.split('.')
        bad_sig = sig[:-1] + ('0' if sig[-1] != '0' else '1')
        self.assertEqual(self.url_open('/woow_ai/attachment/%s/%s.%s' % (att.id, exp, bad_sig)).status_code, 404)
        self.assertEqual(self.url_open('/woow_ai/attachment/%s/%s.%s' % (att.id, int(exp) + 10, sig)).status_code,
                         404)
        secret = self.env['woow.ai.contact']._signing_secret()
        expired, _exp = media.make_token(secret, att.id, att.checksum, now=time.time() - 2 * media.SIGNED_URL_TTL)
        self.assertEqual(self.url_open('/woow_ai/attachment/%s/%s' % (att.id, expired)).status_code, 404)

    def test_signed_url_only_for_ai_channels(self):
        other = new_test_user(self.env, 'aih_other', groups='base.group_user')
        human = self.env['discuss.channel'].with_user(self.internal).channel_get([other.partner_id.id])
        att = self.env['ir.attachment'].with_user(self.internal).sudo().create({
            'name': 'private.pdf', 'raw': TINY_PDF, 'res_model': 'mail.compose.message', 'res_id': 0})
        human.with_user(self.internal).message_post(body='x', message_type='comment', attachment_ids=att.ids)
        secret = self.env['woow.ai.contact']._signing_secret()
        token, _exp = media.make_token(secret, att.id, att.checksum)
        self.assertEqual(self.url_open('/woow_ai/attachment/%s/%s' % (att.id, token)).status_code, 404)

    # --- push API ---

    def push(self, body, key=None, raw=None):
        headers = {'Content-Type': 'application/json'}
        if key is not None:
            headers['Authorization'] = 'Bearer %s' % key
        return self.url_open('/woow_ai/v1/push', data=raw if raw is not None else json.dumps(body),
                             headers=headers)

    def test_push_api(self):
        key = self.contact.action_generate_push_key()['context']['default_push_key']
        self.assertEqual(self.push({'to': {'user_id': self.internal.id}, 'text': 'x'}).status_code, 401)
        self.assertEqual(self.push({'to': {'user_id': self.internal.id}, 'text': 'x'}, key='wpk_wrong').status_code,
                         401)
        self.assertEqual(self.push(None, key=key, raw='{not json').status_code, 400)
        self.assertEqual(self.push({'to': {}, 'text': 'x'}, key=key).status_code, 400)
        self.assertEqual(self.push({'to': {'user_id': self.internal.id}, 'text': ''}, key=key).status_code, 400)
        self.assertEqual(self.push({'to': {'user_id': 999999}, 'text': 'x'}, key=key).status_code, 404)
        self.assertEqual(self.push({'to': {'email': 'nobody@example.com'}, 'text': 'x'}, key=key).status_code, 404)
        # allowed
        response = self.push({'to': {'email': 'ivy@example.com'}, 'text': '**提醒**：明天開會',
                              'attachments': [{'name': 'a.png', 'data_base64': base64.b64encode(TINY_PNG).decode()}]},
                             key=key)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertTrue(data['ok'])
        chat = self.env['discuss.channel'].browse(data['channel_id'])
        self.assertEqual(chat.channel_type, 'chat')
        self.assertEqual(set(chat.channel_member_ids.partner_id.ids),
                         {self.internal.partner_id.id, self.contact.partner_id.id})
        message = self.env['mail.message'].browse(data['message_id'])
        self.assertEqual(message.author_id, self.contact.partner_id)
        self.assertIn('<strong>提醒</strong>', message.body)
        self.assertEqual(message.attachment_ids.mimetype, 'image/png')
        self.assertFalse(self.Job.search([('channel_id', '=', chat.id)]), 'a push never triggers the AI')
        # the same chat is reused (by partner id this time)
        response = self.push({'to': {'partner_id': self.internal.partner_id.id}, 'text': 'again'}, key=key)
        self.assertEqual(response.json()['channel_id'], chat.id)
        # not in the allowed groups
        self.contact.allowed_group_ids = self.env.ref('base.group_portal')
        self.assertEqual(self.push({'to': {'user_id': self.internal.id}, 'text': 'x'}, key=key).status_code, 403)
        self.assertEqual(self.push({'to': {'user_id': self.portal.id}, 'text': 'x'}, key=key).status_code, 200)
        # archived contact: the key stops working
        self.contact.active = False
        self.assertEqual(self.push({'to': {'user_id': self.portal.id}, 'text': 'x'}, key=key).status_code, 401)

    def test_push_email_is_an_exact_match(self):
        # '_' and '%' in the address must not act as SQL wildcards
        from odoo.addons.woow_ai_contact.controllers.main import WoowAiContactController
        recipient = WoowAiContactController._push_recipient
        near = new_test_user(self.env, 'aih_near', groups='base.group_user', email='johnXdoe@example.com')
        self.assertEqual(recipient(self.env, {'email': 'john_doe@example.com'})[1], 'user_not_found')
        self.assertEqual(recipient(self.env, {'email': '%@example.com'})[1], 'user_not_found')
        self.assertEqual(recipient(self.env, {'email': 'john%doe@example.com'})[1], 'user_not_found')
        self.assertEqual(recipient(self.env, {'email': 'JOHNXDOE@example.com'}), (near, None))  # case-insensitive
        exact = new_test_user(self.env, 'aih_exact', groups='base.group_user', email='john_doe@example.com')
        self.assertEqual(recipient(self.env, {'email': 'John_Doe@example.com'}), (exact, None))
        key = self.contact.action_generate_push_key()['context']['default_push_key']
        response = self.push({'to': {'email': 'john_doe@example.com'}, 'text': 'only for John'}, key=key)
        self.assertEqual(response.status_code, 200, response.text)
        chat = self.env['discuss.channel'].browse(response.json()['channel_id'])
        self.assertIn(exact.partner_id, chat.channel_member_ids.partner_id)
        self.assertNotIn(near.partner_id, chat.channel_member_ids.partner_id)

    # --- portal entry ---

    def test_portal_open_and_post(self):
        self.authenticate('aih_portal', 'aih_portal')
        counters = self.make_jsonrpc_request('/my/counters', {'counters': ['woow_ai_contact_count']})
        self.assertGreaterEqual(counters['woow_ai_contact_count'], 1)
        home = self.url_open('/my')
        self.assertIn('href="/my/ai"', home.text)
        page = self.url_open('/my/ai')
        self.assertEqual(page.status_code, 200)
        self.assertIn('HTTP Advisor', page.text)
        response = self.url_open('/my/ai/%s/open' % self.contact.id, allow_redirects=False)
        self.assertIn(response.status_code, (302, 303))
        chat = self.contact._find_chat(self.portal.partner_id)
        self.assertTrue(chat)
        location = response.headers['Location']
        self.assertIn('/discuss/channel/%s' % chat.id, location)
        if 'cs_portal_discuss' not in self.env.registry._init_modules:
            self.assertTrue(location.endswith('/discuss/channel/%s' % chat.id))
        else:
            # the portal back bar leads back to /my/ai, where the user came from
            self.assertTrue(location.endswith('/discuss/channel/%s?discussions=1&back=/my/ai' % chat.id), location)
        # welcome message posted once
        welcome = self.env['mail.message'].search([('model', '=', 'discuss.channel'), ('res_id', '=', chat.id)])
        self.assertEqual(len(welcome), 1)
        # the public Discuss page opens for the portal user...
        self.assertEqual(self.url_open(location).status_code, 200)
        # ...and the portal user can post there, which queues an AI reply
        self.make_jsonrpc_request('/mail/message/post', {
            'thread_model': 'discuss.channel',
            'thread_id': chat.id,
            'post_data': {'body': '請問價格', 'message_type': 'comment', 'subtype_xmlid': 'mail.mt_comment'},
        })
        job = self.Job.search([('channel_id', '=', chat.id)])
        self.assertEqual(len(job), 1)
        self.assertEqual(job.message_id.author_id, self.portal.partner_id)
        # opening again reuses the chat
        self.url_open('/my/ai/%s/open' % self.contact.id, allow_redirects=False)
        self.assertEqual(self.contact._find_chat(self.portal.partner_id), chat)

    def test_portal_contact_not_allowed(self):
        self.contact.allowed_group_ids = self.env.ref('base.group_user')
        self.authenticate('aih_portal', 'aih_portal')
        self.assertNotIn('HTTP Advisor', self.url_open('/my/ai').text)
        self.assertEqual(self.url_open('/my/ai/%s/open' % self.contact.id, allow_redirects=False).status_code, 404)
        self.assertFalse(self.contact._find_chat(self.portal.partner_id))

    def test_internal_portal_page_redirects_to_backend(self):
        self.authenticate('aih_internal', 'aih_internal')
        response = self.url_open('/my/ai/%s/open' % self.contact.id, allow_redirects=False)
        chat = self.contact._find_chat(self.internal.partner_id)
        self.assertIn('/odoo/discuss?active_id=discuss.channel_%s' % chat.id, response.headers['Location'])

    # --- onboarding (decisions 30, 32, 34, 36) ---

    def _member(self, chat, user):
        return chat.channel_member_ids.filtered(lambda m: m.partner_id == user.partner_id)

    def test_backend_messaging_init_onboards(self):
        """/mail/data (read-only route) with init_messaging creates the chat once."""
        hidden = self.env['woow.ai.contact'].create({'name': 'Hidden', 'endpoint_url': 'https://x.example.com/v1',
                                                     'allowed_group_ids': [Command.set([])]})
        self.authenticate('aih_internal', 'aih_internal')
        data = self.make_jsonrpc_request('/mail/data', {'init_messaging': {}})
        chat = self.contact._find_chat(self.internal.partner_id)
        self.assertTrue(chat)
        self.assertFalse(hidden._find_chat(self.internal.partner_id))
        self.assertGreaterEqual(data['Store']['initChannelsUnreadCounter'], 1)
        member = self._member(chat, self.internal)
        self.assertTrue(member.is_pinned)
        self.assertEqual(member.message_unread_counter, 1)
        channels = self.make_jsonrpc_request('/mail/data', {'channels_as_member': True})
        self.assertIn(chat.id, [t['id'] for t in channels.get('discuss.channel', [])])
        # unpinned by the user: not back on the next load
        self.make_jsonrpc_request('/web/dataset/call_kw/discuss.channel/channel_pin', {
            'model': 'discuss.channel', 'method': 'channel_pin', 'args': [chat.id],
            'kwargs': {'pinned': False}})
        self.make_jsonrpc_request('/mail/data', {'init_messaging': {}})
        self.env.invalidate_all()
        self.assertTrue(member.unpin_dt, 'unpinned by the user, not pinned again by the next load')
        welcome = self.env['mail.message'].search([('model', '=', 'discuss.channel'), ('res_id', '=', chat.id)])
        self.assertEqual(len(welcome), 1)

    def test_start_a_conversation_search_and_open(self):
        hidden = self.env['woow.ai.contact'].create({'name': 'HTTP Hidden', 'endpoint_url': 'https://x.example.com/v1',
                                                     'allowed_group_ids': [Command.set([])]})
        self.authenticate('aih_internal', 'aih_internal')
        found = self.make_jsonrpc_request('/web/dataset/call_kw/res.partner/im_search', {
            'model': 'res.partner', 'method': 'im_search', 'args': ['HTTP', 10, []], 'kwargs': {}})
        ids = [p['id'] for p in found.get('res.partner', [])]
        self.assertIn(self.contact.partner_id.id, ids)
        self.assertNotIn(hidden.partner_id.id, ids)
        result = self.make_jsonrpc_request('/web/dataset/call_kw/discuss.channel/channel_get', {
            'model': 'discuss.channel', 'method': 'channel_get', 'args': [],
            'kwargs': {'partners_to': [self.contact.partner_id.id], 'force_open': True}})
        chat = self.contact._find_chat(self.internal.partner_id)
        self.assertTrue(any(t.get('id') == chat.id for t in result.get('discuss.channel', [])), result.keys())
        self.assertTrue(chat.allow_public_upload)
        with self.assertRaises(Exception):
            self.make_jsonrpc_request('/web/dataset/call_kw/discuss.channel/channel_get', {
                'model': 'discuss.channel', 'method': 'channel_get', 'args': [],
                'kwargs': {'partners_to': [hidden.partner_id.id]}})
        self.assertFalse(hidden._find_chat(self.internal.partner_id))

    def test_portal_home_onboards_once(self):
        self.authenticate('aih_portal', 'aih_portal')
        self.assertEqual(self.url_open('/my').status_code, 200)
        chat = self.contact._find_chat(self.portal.partner_id)
        self.assertTrue(chat)
        member = self._member(chat, self.portal)
        self.assertTrue(member.is_pinned)
        self.assertEqual(member.message_unread_counter, 1)
        self.assertIn(self.portal, self.contact.onboarded_user_ids)
        member.unpin_dt = '2026-01-01 00:00:00'
        self.env.flush_all()
        self.url_open('/my')
        self.env.invalidate_all()
        self.assertTrue(member.unpin_dt, 'not pinned again by itself')
        welcome = self.env['mail.message'].search([('model', '=', 'discuss.channel'), ('res_id', '=', chat.id)])
        self.assertEqual(len(welcome), 1)
        # the /my card and /my/ai stay
        self.assertIn('href="/my/ai"', self.url_open('/my').text)
        self.assertIn('HTTP Advisor', self.url_open('/my/ai').text)

    def test_any_portal_page_onboards(self):
        # not only /my: a bookmark to another portal page (here /my/ai, or the
        # discussion list of a portal Discuss module) creates the chats first
        self.authenticate('aih_portal', 'aih_portal')
        self.assertFalse(self.contact._find_chat(self.portal.partner_id))
        self.assertEqual(self.url_open('/my/ai').status_code, 200)
        self.assertTrue(self.contact._find_chat(self.portal.partner_id))
        self.assertIn(self.portal, self.contact.onboarded_user_ids)

    def test_push_pins_the_chat_again(self):
        key = self.contact.action_generate_push_key()['context']['default_push_key']
        chat, _created = self.contact._get_or_create_chat(self.internal)
        member = self._member(chat, self.internal)
        member.unpin_dt = fields.Datetime.now() + timedelta(minutes=1)
        self.env.flush_all()
        self.assertFalse(member.is_pinned)
        response = self.push({'to': {'user_id': self.internal.id}, 'text': '提醒'}, key=key)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['channel_id'], chat.id)
        self.env.invalidate_all()
        self.assertTrue(member.is_pinned)

    def test_sidebar_routes_removed(self):
        self.authenticate('aih_internal', 'aih_internal')
        for route in ('/woow_ai/contacts', '/woow_ai/open'):
            response = self.url_open(route, data='{}', headers={'Content-Type': 'application/json'})
            self.assertEqual(response.status_code, 404, route)
