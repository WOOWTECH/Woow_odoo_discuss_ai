# -*- coding: utf-8 -*-
"""Sharing an AI contact with single users (portal customers), decision 45."""
from unittest.mock import patch

from markupsafe import Markup

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests.common import HttpCase, new_test_user, tagged


@tagged('post_install', '-at_install', 'woow_ai_contact')
class TestAiContactSharing(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.alice = new_test_user(cls.env, 'aish_alice', groups='base.group_portal', name='Portal Alice')
        cls.bob = new_test_user(cls.env, 'aish_bob', groups='base.group_portal', name='Portal Bob')
        cls.contact = cls.env['woow.ai.contact'].create({
            'name': 'Shared Advisor', 'backend_type': 'openai', 'endpoint_url': 'https://llm.example.com/v1',
            'welcome_message': '歡迎'})
        cls.Job = cls.env['woow.ai.job']

    def setUp(self):
        super().setUp()
        patcher = patch.object(type(self.env['woow.ai.job']), '_start_after_commit')
        patcher.start()
        self.addCleanup(patcher.stop)

    def _member(self, chat, user):
        return chat.channel_member_ids.filtered(lambda m: m.partner_id == user.partner_id)

    def _post(self, chat, user, text='你好'):
        return chat.with_user(user).message_post(body=Markup(text), message_type='comment',
                                                 subtype_xmlid='mail.mt_comment')

    def test_internal_only_by_default(self):
        self.assertEqual(self.contact.allowed_group_ids, self.env.ref('base.group_user'))
        self.assertFalse(self.contact.portal_open_to_all)
        self.assertFalse(self.contact._user_allowed(self.alice))
        self.assertFalse(self.env['woow.ai.contact']._contacts_for_user(self.alice) & self.contact)
        self.assertFalse(self.contact.with_user(self.alice).search([('id', '=', self.contact.id)]))
        self.assertFalse(self.env['woow.ai.contact']._contacts_to_onboard(self.alice) & self.contact)
        with self.assertRaises(AccessError):
            self.env['discuss.channel'].with_user(self.alice).channel_get([self.contact.partner_id.id])

    def test_share_creates_the_chat_at_once(self):
        self.contact.shared_user_ids = [Command.link(self.alice.id)]
        self.assertTrue(self.contact._user_allowed(self.alice))
        self.assertFalse(self.contact._user_allowed(self.bob))
        chat = self.contact._find_chat(self.alice.partner_id)
        self.assertTrue(chat, 'chat created when shared')
        self.assertTrue(self._member(chat, self.alice).is_pinned)
        self.assertEqual(self._member(chat, self.alice).message_unread_counter, 1, 'welcome message unread')
        self.assertFalse(self.contact._find_chat(self.bob.partner_id))
        # the portal user may read the contact (image on the portal) and the AI answers them
        self.assertTrue(self.contact.with_user(self.alice).search([('id', '=', self.contact.id)]))
        self._post(chat, self.alice)
        self.assertEqual(len(self.Job.search([('channel_id', '=', chat.id)])), 1)
        # Bob is not shared: the portal pages do not offer it, opening it is refused
        self.authenticate('aish_bob', 'aish_bob')
        self.assertEqual(self.url_open('/my/ai/%s/open' % self.contact.id, allow_redirects=False).status_code, 404)

    def test_unshare_hides_the_chat_and_the_ai_refuses(self):
        self.contact.shared_user_ids = [Command.link(self.alice.id)]
        chat = self.contact._find_chat(self.alice.partner_id)
        self.contact.shared_user_ids = [Command.unlink(self.alice.id)]
        self.assertFalse(self.contact._user_allowed(self.alice))
        self.assertFalse(self._member(chat, self.alice).is_pinned, 'chat left her lists')
        before = self.env['mail.message'].search_count([('model', '=', 'discuss.channel'), ('res_id', '=', chat.id)])
        self._post(chat, self.alice)
        self.assertFalse(self.Job.search([('channel_id', '=', chat.id)]), 'no backend call')
        messages = self.env['mail.message'].search([('model', '=', 'discuss.channel'), ('res_id', '=', chat.id)])
        self.assertEqual(len(messages), before + 2, 'her message and the refusal')
        # shared again: the chat comes back (no second welcome)
        self.contact.shared_user_ids = [Command.link(self.alice.id)]
        self.assertTrue(self._member(chat, self.alice).is_pinned)
        self.assertEqual(self.contact._find_chat(self.alice.partner_id), chat)

    def test_portal_group_warning_and_removal(self):
        portal = self.env.ref('base.group_portal')
        self.contact.allowed_group_ids = [Command.link(portal.id)]
        self.assertTrue(self.contact.portal_open_to_all)
        self.assertTrue(self.contact._user_allowed(self.bob))
        chat, _created = self.contact._get_or_create_chat(self.bob)
        self.contact.shared_user_ids = [Command.link(self.alice.id)]
        # the Portal group removed: only the shared user keeps the AI contact
        self.contact.allowed_group_ids = [Command.unlink(portal.id)]
        self.assertFalse(self.contact.portal_open_to_all)
        self.assertFalse(self._member(chat, self.bob).is_pinned)
        alice_chat = self.contact._find_chat(self.alice.partner_id)
        self.assertTrue(self._member(alice_chat, self.alice).is_pinned)

    def test_portal_warning_while_editing(self):
        # the form computes the warning before saving (onchange)
        from odoo.tests import Form
        with Form(self.contact) as form:
            self.assertFalse(form.portal_open_to_all)
            form.allowed_group_ids.add(self.env.ref('base.group_portal'))
            self.assertTrue(form.portal_open_to_all)
            form.allowed_group_ids.remove(self.env.ref('base.group_portal').id)
            self.assertFalse(form.portal_open_to_all)

    def test_shared_only_contact_partner_is_active(self):
        contact = self.env['woow.ai.contact'].create({
            'name': 'Only Shared', 'endpoint_url': 'https://llm.example.com/v1',
            'allowed_group_ids': [Command.set([])], 'shared_user_ids': [Command.set([self.alice.id])]})
        self.assertTrue(contact.partner_id.active)
        self.assertTrue(contact._find_chat(self.alice.partner_id), 'chat created on create too')
        contact.shared_user_ids = [Command.clear()]
        self.assertFalse(contact.partner_id.with_context(active_test=False).active)

    def test_archiving_is_not_unsharing(self):
        self.contact.shared_user_ids = [Command.link(self.alice.id)]
        chat = self.contact._find_chat(self.alice.partner_id)
        self.contact.active = False
        self.assertTrue(self._member(chat, self.alice).is_pinned, 'archived note path, chat kept')
