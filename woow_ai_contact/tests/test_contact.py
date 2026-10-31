# -*- coding: utf-8 -*-
import base64

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user, tagged

from odoo.addons.woow_ai_contact.models.ai_contact import endpoint_url_allowed, hash_push_key

from .common import TEST_KEY, TINY_PNG, AiContactCase


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
