# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
import json
from unittest.mock import patch

import requests

from odoo.addons.base.controllers.rpc import dumps as xmlrpc_dumps
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user, tagged

from odoo.addons.woow_ai_contact.models.ai_contact import endpoint_url_allowed, hash_push_key

from .common import BACKEND_GET, BACKEND_POST, TEST_KEY, TINY_PNG, AiContactCase, http_response


@tagged('woow_ai_contact')
class TestAiContact(AiContactCase):

    def test_bot_partner_created_and_synced(self):
        partner = self.contact.partner_id
        self.assertTrue(partner.active)
        self.assertFalse(partner.user_ids)
        self.assertEqual(partner.name, 'Product Advisor')
        self.assertIn(self.env.ref('woow_ai_contact.partner_category_ai_contact'), partner.category_id)
        self.contact.write({'name': '產品顧問', 'image_1920': base64.b64encode(TINY_PNG)})
        self.assertEqual(partner.name, '產品顧問')
        self.assertTrue(partner.image_1920)
        self.contact.active = False
        self.assertFalse(partner.active)

    def test_bot_partner_archived_without_groups(self):
        """Empty allowed groups = nobody can DM it (Live Chat only): partner archived."""
        partner = self.contact.partner_id
        self.contact.allowed_group_ids = False
        self.assertFalse(partner.active)
        self.contact.allowed_group_ids = self.env.ref('base.group_user')
        self.assertTrue(partner.active)
        livechat_only = self.env['woow.ai.contact'].create({'name': 'Site bot', 'allowed_group_ids': False})
        self.assertFalse(livechat_only.partner_id.active)
        self.assertTrue(livechat_only.partner_id.exists())
        # archived AI contact with groups: archived partner; unarchived: active again
        self.contact.active = False
        self.assertFalse(partner.active)
        self.contact.active = True
        self.assertTrue(partner.active)
        # no groups: unarchiving the AI contact keeps the partner archived
        livechat_only.active = False
        livechat_only.active = True
        self.assertFalse(livechat_only.partner_id.active)

    def test_new_backend_defaults(self):
        self.assertTrue(self.contact.openai_stream)
        self.assertEqual(self.contact.max_retries, 1)
        with self.assertRaises(ValidationError):
            self.contact.max_retries = 3
        self.contact.max_retries = 0

    def test_test_connection_openai(self):
        with patch(BACKEND_GET, return_value=http_response(200, {'data': [{'id': 'secret-model-list'}]})) as get:
            action = self.contact.action_test_connection()
        args, kwargs = get.call_args
        self.assertEqual(args[0], 'https://llm.example.com/v1/models')
        self.assertEqual(kwargs['headers'], {'Authorization': 'Bearer %s' % TEST_KEY})
        self.assertFalse(kwargs['allow_redirects'])
        params = action['params']
        self.assertEqual(action['tag'], 'display_notification')
        self.assertEqual(params['type'], 'success')
        self.assertIn('200', params['message'])
        self.assertNotIn('secret-model-list', json.dumps(action))
        for status in (401, 404, 502):
            with patch(BACKEND_GET, return_value=http_response(status, text='upstream detail')):
                params = self.contact.action_test_connection()['params']
            self.assertEqual(params['type'], 'danger')
            self.assertIn(str(status), params['message'])
            self.assertNotIn('upstream detail', params['message'])
        with patch(BACKEND_GET, side_effect=requests.ConnectionError('down')):
            self.assertEqual(self.contact.action_test_connection()['params']['type'], 'danger')
        with patch(BACKEND_GET, side_effect=requests.ReadTimeout('slow')):
            self.assertEqual(self.contact.action_test_connection()['params']['type'], 'danger')
        with patch(BACKEND_POST) as post, patch(BACKEND_GET, return_value=http_response(200, {})):
            self.contact.action_test_connection()
        post.assert_not_called()  # never a chat completion (it would run the agent)

    def test_test_connection_n8n(self):
        with patch(BACKEND_POST, return_value=http_response(200, {'skip': True})) as post:
            params = self.n8n.action_test_connection()['params']
        self.assertEqual(params['type'], 'success')
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://n8n.example.com/webhook/abc')
        headers = kwargs['headers']
        self.assertNotIn('Authorization', headers)
        expected = hmac.new(TEST_KEY.encode(), headers['X-Woow-Timestamp'].encode() + b'.' + kwargs['data'],
                            hashlib.sha256).hexdigest()
        self.assertEqual(headers['X-Woow-Signature'], 'sha256=%s' % expected)
        self.assertEqual(json.loads(kwargs['data'])['event'], 'ping')
        with patch(BACKEND_POST, return_value=http_response(401)):
            self.assertEqual(self.n8n.action_test_connection()['params']['type'], 'danger')

    def test_test_connection_admin_only(self):
        with self.assertRaises(AccessError):
            self.contact.with_user(self.internal).action_test_connection()
        self.contact.action_clear_api_key()
        with patch(BACKEND_GET) as get:
            params = self.contact.action_test_connection()['params']
        get.assert_not_called()
        self.assertEqual(params['type'], 'danger')

    def test_default_groups(self):
        self.assertEqual(set(self.contact.allowed_group_ids.ids),
                         {self.env.ref('base.group_user').id, self.env.ref('base.group_portal').id})

    def test_api_key_write_only(self):
        self.assertEqual(self.contact._get_api_key(), TEST_KEY)
        data = self.contact.read(['api_key', 'api_key_set'])[0]
        self.assertFalse(data['api_key'])
        self.assertTrue(data['api_key_set'])
        self.assertNotIn('api_key_secret', self.env['woow.ai.contact']._fields)
        # web_save read-back does not echo the key
        result = self.contact.web_save({'api_key': 'new-key'}, {'api_key': {}, 'api_key_set': {}})
        self.assertFalse(result[0]['api_key'])
        self.assertEqual(self.contact._get_api_key(), 'new-key')
        # empty keeps the key
        self.contact.write({'api_key': ''})
        self.assertEqual(self.contact._get_api_key(), 'new-key')
        self.contact.action_clear_api_key()
        self.assertFalse(self.contact._get_api_key())
        self.assertFalse(self.contact.api_key_set)
        # never in an export either
        export = self.contact.export_data(['api_key'])['datas']
        self.assertFalse(export[0][0])

    def test_api_key_removed_when_the_server_changes(self):
        contact = self.contact
        # same server, other path: kept
        contact.endpoint_url = 'https://llm.example.com/v2'
        self.assertEqual(contact._get_api_key(), TEST_KEY)
        contact.endpoint_url = 'HTTPS://LLM.example.com:443/v1'
        self.assertEqual(contact._get_api_key(), TEST_KEY)
        # other host without a new key: removed before anything is sent there
        contact.endpoint_url = 'https://openrouter.example.org/api/v1'
        self.assertFalse(contact.api_key_set)
        self.assertEqual(contact._get_api_key(), '')
        with patch(BACKEND_GET) as get:
            result = contact.action_test_connection()
        get.assert_not_called()
        self.assertEqual(result['params']['type'], 'danger')
        # other host with a new key in the same write: the new key is kept
        contact.write({'endpoint_url': 'https://llm2.example.com/v1', 'api_key': 'second-placeholder'})
        self.assertEqual(contact._get_api_key(), 'second-placeholder')
        # other port / scheme counts as another server
        contact.endpoint_url = 'https://llm2.example.com:8443/v1'
        self.assertFalse(contact.api_key_set)
        # a key set while the URL was empty survives filling the URL in (migration)
        contact.write({'endpoint_url': False, 'api_key': 'third-placeholder'})
        contact.endpoint_url = 'https://llm.example.com/v1'
        self.assertEqual(contact._get_api_key(), 'third-placeholder')
        # n8n signing secret -> openai: never becomes a Bearer token
        self.n8n.backend_type = 'openai'
        self.assertFalse(self.n8n.api_key_set)
        self.n8n.write({'backend_type': 'n8n', 'api_key': 'n8n-placeholder'})
        self.n8n.write({'name': 'Renamed', 'backend_type': 'n8n'})  # unchanged type: kept
        self.assertEqual(self.n8n._get_api_key(), 'n8n-placeholder')

    def test_uninstall_hook_archives_bot_partners(self):
        from odoo.addons.woow_ai_contact.hooks import uninstall_hook
        chat = self.chat(self.portal)
        self.assertTrue(self.contact.partner_id.active)
        uninstall_hook(self.env)
        self.assertFalse(self.contact.partner_id.active)
        self.assertFalse(self.n8n.partner_id.active)
        self.assertTrue(self.contact.partner_id.exists(), 'kept: it authors the old AI messages')
        member = chat.channel_member_ids.filtered(lambda m: m.partner_id == self.portal.partner_id)
        self.assertTrue(member.unpin_dt)
        self.assertFalse(chat.allow_public_upload)

    def test_non_admin_cannot_read_connection(self):
        contact = self.contact.with_user(self.internal)
        self.assertEqual(contact.name, 'Product Advisor')
        with self.assertRaises(AccessError):
            contact.read(['endpoint_url'])
        with self.assertRaises(AccessError):
            contact.read(['api_key_set'])
        with self.assertRaises(AccessError):
            contact.write({'name': 'x'})
        with self.assertRaises(AccessError):
            self.contact.with_user(self.portal).read(['push_key_hint'])

    def test_record_rule_allowed_groups(self):
        Contact = self.env['woow.ai.contact']
        self.contact.allowed_group_ids = self.group_testers
        self.assertNotIn(self.contact, Contact.with_user(self.internal).search([]))
        self.assertNotIn(self.contact, Contact.with_user(self.portal).search([]))
        self.assertIn(self.n8n, Contact.with_user(self.portal).search([]))
        tester = new_test_user(self.env, 'ai_tester', groups='base.group_user')
        tester.groups_id |= self.group_testers
        self.assertIn(self.contact, Contact.with_user(tester).search([]))
        self.assertTrue(self.contact._user_allowed(tester))
        self.assertFalse(self.contact._user_allowed(self.internal))
        self.assertFalse(self.contact._user_allowed(self.env.ref('base.public_user')))

    def test_endpoint_https_rule(self):
        self.assertTrue(endpoint_url_allowed('https://llm.example.com/v1'))
        self.assertTrue(endpoint_url_allowed('http://localhost:8080/v1'))
        self.assertTrue(endpoint_url_allowed('http://hermes.ai.svc.cluster.local:8642/v1'))
        self.assertFalse(endpoint_url_allowed('http://llm.example.com/v1'))
        self.assertFalse(endpoint_url_allowed('http://svc.cluster.local.evil.com/v1'))
        self.assertFalse(endpoint_url_allowed('ftp://llm.example.com'))
        with self.assertRaises(ValidationError):
            self.contact.endpoint_url = 'http://llm.example.com/v1'

    def test_limits(self):
        with self.assertRaises(ValidationError):
            self.contact.max_history = 0
        with self.assertRaises(ValidationError):
            self.contact.max_history = 201
        with self.assertRaises(ValidationError):
            self.contact.timeout_seconds = 1
        with self.assertRaises(ValidationError):
            self.contact.max_download_mb = 0

    def test_push_key_hash_only(self):
        action = self.contact.action_generate_push_key()
        key = action['context']['default_push_key']
        self.assertTrue(key.startswith('wpk_'))
        self.assertEqual(self.contact.push_key_hash, hash_push_key(key))
        self.assertNotIn(key, self.contact.push_key_hint)
        self.assertEqual(self.env['woow.ai.contact']._find_by_push_key(key), self.contact)
        self.assertFalse(self.env['woow.ai.contact']._find_by_push_key(key + 'x'))
        self.assertFalse(self.env['woow.ai.contact']._find_by_push_key(''))
        self.assertFalse(self.env['woow.ai.contact']._find_by_push_key(self.contact.push_key_hash))
        # regenerate: the old key stops working
        new_key = self.contact.action_generate_push_key()['context']['default_push_key']
        self.assertFalse(self.env['woow.ai.contact']._find_by_push_key(key))
        self.assertEqual(self.env['woow.ai.contact']._find_by_push_key(new_key), self.contact)
        self.contact.action_revoke_push_key()
        self.assertFalse(self.env['woow.ai.contact']._find_by_push_key(new_key))
        with self.assertRaises(AccessError):
            self.contact.with_user(self.internal).action_generate_push_key()

    def test_public_actions_xmlrpc_marshallable(self):
        """Regression: action_revoke_push_key returned None and XML-RPC callers
        got Fault 'cannot marshal None' although the action had committed.
        Every public action_* method must return a value Odoo's XML-RPC
        marshaller accepts (no None anywhere in the result)."""
        Contact = type(self.env['woow.ai.contact'])
        # the methods this module defines (core action_archive/unarchive return
        # None in Odoo 18 itself and are out of scope)
        names = sorted(name for name in dir(Contact)
                       if name.startswith('action_') and callable(getattr(Contact, name))
                       and getattr(Contact, name).__module__.startswith('odoo.addons.woow_ai_contact.'))
        # guard against the introspection silently selecting nothing
        self.assertTrue({'action_revoke_push_key', 'action_clear_api_key', 'action_generate_push_key',
                         'action_test_connection', 'action_view_call_logs',
                         'action_open_bot_partner'} <= set(names), names)
        for name in names:
            contact = self.contact.copy()
            contact.write({'api_key': TEST_KEY, 'endpoint_url': self.contact.endpoint_url})
            with self.subTest(action=name), \
                    patch(BACKEND_GET, return_value=http_response(200)), \
                    patch(BACKEND_POST, return_value=http_response(200)):
                result = getattr(contact, name)()
                self.assertIsNotNone(result)
                xmlrpc_dumps((result,))  # raises TypeError on None
        # the two former offenders, explicitly
        self.contact.action_generate_push_key()
        self.assertIs(self.contact.action_revoke_push_key(), True)
        self.assertFalse(self.contact.push_key_hash)
        self.assertIs(self.contact.action_clear_api_key(), True)
        self.assertFalse(self.contact._get_api_key())

    def test_copy_gets_own_partner(self):
        copy = self.contact.copy()
        self.assertTrue(copy.partner_id)
        self.assertNotEqual(copy.partner_id, self.contact.partner_id)
        self.assertFalse(copy._get_api_key())

    def test_welcome_message_only_on_creation(self):
        self.contact.welcome_message = '您好！**歡迎**'
        chat, created = self.contact._get_or_create_chat(self.internal)
        self.assertTrue(created)
        self.assertEqual(chat.channel_type, 'chat')
        self.assertTrue(chat.allow_public_upload, 'portal users must be able to upload files')
        self.assertEqual(set(chat.channel_member_ids.partner_id.ids),
                         {self.internal.partner_id.id, self.contact.partner_id.id})
        welcome = self.bot_messages(chat)
        self.assertEqual(len(welcome), 1)
        self.assertIn('<strong>歡迎</strong>', welcome.body)
        again, created = self.contact._get_or_create_chat(self.internal)
        self.assertFalse(created)
        self.assertEqual(again, chat)
        self.assertEqual(len(self.bot_messages(chat)), 1)
        self.assertFalse(self.jobs(chat), 'the welcome message must not trigger the AI')

    def test_contacts_for_user(self):
        self.n8n.allowed_group_ids = self.env.ref('base.group_portal')
        self.assertEqual(self.env['woow.ai.contact']._contacts_for_user(self.internal), self.contact)
        self.assertEqual(self.env['woow.ai.contact']._contacts_for_user(self.portal), self.contact | self.n8n)

    def test_existing_chat_reused_without_welcome(self):
        """A chat opened from the normal "start a conversation" (no upload flag yet)
        is reused: no second welcome message, uploads get enabled."""
        self.contact.welcome_message = 'Hello'
        chat = self.env['discuss.channel'].with_user(self.portal).sudo().channel_get([self.contact.partner_id.id])
        self.assertFalse(chat.allow_public_upload)
        again, created = self.contact._get_or_create_chat(self.portal)
        self.assertEqual(again, chat)
        self.assertFalse(created)
        self.assertTrue(chat.allow_public_upload)
        self.assertFalse(self.bot_messages(chat))
