# -*- coding: utf-8 -*-
"""Decisions 30-37: chats created like OdooBot's, search, bot presence, menus."""

from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests.common import new_test_user, tagged

from odoo.addons.mail.tools.discuss import Store

from .common import AiContactCase


@tagged('woow_ai_contact')
class TestAiContactOnboarding(AiContactCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.contact.welcome_message = '您好，我是**產品顧問**'
        cls.n8n.welcome_message = 'Hi from the workflow bot'
        cls.hidden = cls.env['woow.ai.contact'].create({
            'name': 'Hidden Helper', 'allowed_group_ids': [Command.set([])], 'welcome_message': 'never'})
        cls.testers_only = cls.env['woow.ai.contact'].create({
            'name': 'Tester Helper', 'allowed_group_ids': [Command.set([cls.group_testers.id])],
            'welcome_message': 'testers'})

    def init_messaging(self, user):
        store = Store()
        user.with_user(user)._init_messaging(store)
        return store.get_result()

    def member(self, chat, user):
        return chat.channel_member_ids.filtered(lambda m: m.partner_id == user.partner_id)

    def unpin(self, chat, user):
        # a minute ahead: in one test transaction every date is "now", and a chat
        # whose last interest is not older than unpin_dt counts as pinned
        self.member(chat, user).unpin_dt = fields.Datetime.now() + timedelta(minutes=1)
        self.assertFalse(self.member(chat, user).is_pinned)

    # --- 30 / 32: chats created on the first messaging init, welcome unread ---

    def test_onboarding_creates_pinned_chats_with_unread_welcome(self):
        result = self.init_messaging(self.internal)
        chats = {contact: contact._find_chat(self.internal.partner_id) for contact in (self.contact, self.n8n)}
        for contact, chat in chats.items():
            self.assertTrue(chat, contact.name)
            member = self.member(chat, self.internal)
            self.assertTrue(member.is_pinned, contact.name)
            self.assertFalse(member.unpin_dt)
            self.assertTrue(chat.allow_public_upload)
            welcome = self.bot_messages(chat, contact)
            self.assertEqual(len(welcome), 1)
            self.assertEqual(member.message_unread_counter, 1, 'the welcome message is unread (badge)')
            self.assertIn(self.internal, contact.onboarded_user_ids)
            self.assertFalse(self.jobs(chat), 'the welcome message never triggers the AI')
        self.assertIn('<strong>產品顧問</strong>', self.bot_messages(chats[self.contact]).body)
        # the unread counter sent with the same answer counts them (messaging icon badge)
        self.assertGreaterEqual(result['Store']['initChannelsUnreadCounter'], 2)
        # the bot partner of the AI does not get the chat pinned
        bot_member = chats[self.contact].channel_member_ids.filtered(
            lambda m: m.partner_id == self.contact.partner_id)
        self.assertTrue(bot_member.unpin_dt)

    def test_onboarding_only_once(self):
        self.init_messaging(self.internal)
        chat = self.contact._find_chat(self.internal.partner_id)
        self.init_messaging(self.internal)
        self.assertEqual(len(self.bot_messages(chat)), 1, 'no second welcome')
        chats = self.env['discuss.channel'].search([
            ('channel_type', '=', 'chat'), ('channel_member_ids.partner_id', '=', self.contact.partner_id.id)])
        self.assertEqual(len(chats), 1)

    def test_unpinned_chat_does_not_come_back(self):
        self.init_messaging(self.internal)
        chat = self.contact._find_chat(self.internal.partner_id)
        self.unpin(chat, self.internal)
        self.init_messaging(self.internal)
        self.assertFalse(self.member(chat, self.internal).is_pinned, 'an unpinned chat stays unpinned')
        self.assertEqual(len(self.bot_messages(chat)), 1)
        # opening it again (portal / search / push all use _get_or_create_chat) pins it
        again, created = self.contact._get_or_create_chat(self.internal)
        self.assertEqual(again, chat)
        self.assertFalse(created)
        self.assertTrue(self.member(chat, self.internal).is_pinned)
        self.assertEqual(len(self.bot_messages(chat)), 1)

    def test_existing_chat_counts_as_onboarded(self):
        """A chat opened before (old sidebar, portal, push) is kept as it is."""
        chat, _created = self.contact._get_or_create_chat(self.internal)
        self.env['woow.ai.contact'].browse(self.contact.id).sudo().onboarded_user_ids = [
            Command.unlink(self.internal.id)]
        self.unpin(chat, self.internal)
        self.init_messaging(self.internal)
        self.assertFalse(self.member(chat, self.internal).is_pinned)
        self.assertEqual(len(self.bot_messages(chat)), 1)
        self.assertIn(self.internal, self.contact.onboarded_user_ids)

    def test_contact_added_or_allowed_later(self):
        self.init_messaging(self.internal)
        later = self.env['woow.ai.contact'].create({'name': 'Later Bot', 'welcome_message': 'new here'})
        self.assertFalse(later._find_chat(self.internal.partner_id))
        self.init_messaging(self.internal)
        self.assertTrue(later._find_chat(self.internal.partner_id))
        # a contact newly allowed for the user's groups
        self.assertFalse(self.testers_only._find_chat(self.internal.partner_id))
        self.internal.groups_id = [Command.link(self.group_testers.id)]
        self.init_messaging(self.internal)
        chat = self.testers_only._find_chat(self.internal.partner_id)
        self.assertTrue(chat)
        self.assertEqual(self.bot_messages(chat, self.testers_only).mapped('body'), ['<p>testers</p>'])

    def test_disallowed_and_empty_group_contacts_never_onboarded(self):
        self.init_messaging(self.internal)
        self.init_messaging(self.portal)
        for user in (self.internal, self.portal):
            self.assertFalse(self.hidden._find_chat(user.partner_id))
            self.assertFalse(self.testers_only._find_chat(user.partner_id))
            self.assertNotIn(user, self.hidden.onboarded_user_ids)
        self.contact.active = False
        fresh = new_test_user(self.env, 'ai_fresh', groups='base.group_user', name='Fresh Fay')
        self.init_messaging(fresh)
        self.assertFalse(self.contact._find_chat(fresh.partner_id), 'archived contacts are not onboarded')
        self.assertTrue(self.n8n._find_chat(fresh.partner_id))
        # restricted to internal users: portal users are not onboarded to it
        self.n8n.allowed_group_ids = self.env.ref('base.group_user')
        pia = new_test_user(self.env, 'ai_pia', groups='base.group_portal', name='Portal Pia')
        self.env['woow.ai.contact']._onboard_user(pia)
        self.assertFalse(self.n8n._find_chat(pia.partner_id))

    def test_portal_users_are_onboarded_too(self):
        chats = self.env['woow.ai.contact']._onboard_user(self.portal)
        self.assertEqual(len(chats), 2)
        chat = self.contact._find_chat(self.portal.partner_id)
        self.assertEqual(self.member(chat, self.portal).message_unread_counter, 1)

    def test_onboarding_never_breaks_messaging(self):
        Contact = type(self.env['woow.ai.contact'])

        def boom(*args, **kwargs):
            raise ValueError('backend down')
        with self.assertLogs('odoo.addons.woow_ai_contact.models.res_users', level='ERROR'):
            with patch.object(Contact, '_get_or_create_chat', boom):
                result = self.init_messaging(self.internal)
        self.assertIn('Store', result)
        self.assertFalse(self.contact._find_chat(self.internal.partner_id))
        self.assertNotIn(self.internal, self.contact.onboarded_user_ids, 'rolled back: tried again next time')

    # --- 31: "Start a conversation" ---

    def search(self, user, term, excluded=None):
        result = self.env['res.partner'].with_user(user).im_search(term, 10, excluded or [])
        return {p['id'] for p in result.get('res.partner', [])}

    def test_search_finds_allowed_ai_contacts_only(self):
        self.internal.groups_id = [Command.link(self.group_testers.id)]
        found = self.search(self.internal, 'Helper')
        self.assertIn(self.testers_only.partner_id.id, found)
        self.assertNotIn(self.hidden.partner_id.id, found, 'empty allowed groups: nobody finds it')
        self.assertIn(self.contact.partner_id.id, self.search(self.internal, 'Product'))
        self.assertNotIn(self.testers_only.partner_id.id, self.search(self.internal, 'Product'))
        # other users
        other = new_test_user(self.env, 'ai_other', groups='base.group_user', name='Other Otto')
        self.assertNotIn(self.testers_only.partner_id.id, self.search(other, 'Helper'))
        self.assertIn(self.internal.partner_id.id, self.search(other, 'Internal'), 'users are still found')
        # excluded ids and archived contacts
        self.assertNotIn(self.contact.partner_id.id,
                         self.search(self.internal, 'Product', [self.contact.partner_id.id]))
        self.contact.active = False
        self.assertNotIn(self.contact.partner_id.id, self.search(self.internal, 'Product'))
        # portal users: only what they may use
        self.n8n.allowed_group_ids = self.env.ref('base.group_user')
        self.assertNotIn(self.n8n.partner_id.id, self.search(self.portal, 'Workflow'))
        self.n8n.allowed_group_ids = self.env.ref('base.group_portal')
        self.assertIn(self.n8n.partner_id.id, self.search(self.portal, 'Workflow'))

    def test_search_result_shows_bot_status(self):
        result = self.env['res.partner'].with_user(self.internal).im_search('Product', 10, [])
        entry = next(p for p in result['res.partner'] if p['id'] == self.contact.partner_id.id)
        self.assertEqual(entry.get('im_status'), 'bot')
        self.assertEqual(entry.get('name'), 'Product Advisor')

    def test_open_from_search_uses_the_ai_chat(self):
        Channel = self.env['discuss.channel'].with_user(self.internal)
        chat = Channel.channel_get([self.contact.partner_id.id], force_open=True)
        self.assertEqual(chat, self.contact._find_chat(self.internal.partner_id))
        self.assertTrue(chat.allow_public_upload)
        self.assertEqual(len(self.bot_messages(chat)), 1, 'welcome posted like the other entries')
        self.assertIn(self.internal, self.contact.onboarded_user_ids)
        member = self.member(chat.sudo(), self.internal)
        self.assertEqual(member.fold_state, 'open')
        # unpinned, then opened from search again: pinned, same chat, no second welcome
        self.unpin(chat.sudo(), self.internal)
        again = Channel.channel_get([self.contact.partner_id.id])
        self.assertEqual(again, chat)
        self.assertTrue(member.is_pinned)
        self.assertEqual(len(self.bot_messages(chat)), 1)

    def test_open_from_search_checks_access(self):
        Channel = self.env['discuss.channel'].with_user(self.internal)
        for contact in (self.hidden, self.testers_only):
            with self.assertRaises(AccessError):
                Channel.channel_get([contact.partner_id.id])
            self.assertFalse(contact._find_chat(self.internal.partner_id))
        # chats with people are unchanged
        other = new_test_user(self.env, 'ai_peer', groups='base.group_user', name='Peer Pete')
        chat = Channel.channel_get([other.partner_id.id])
        self.assertEqual(chat.channel_type, 'chat')
        self.assertFalse(self.bot_messages(chat))

    # --- 35: presence ---

    def test_bot_im_status(self):
        self.assertEqual(self.contact.partner_id.im_status, 'bot')
        self.assertEqual(self.hidden.partner_id.with_context(active_test=False).im_status, 'bot',
                         'Live Chat-only bots show as bots too')
        self.assertEqual(self.internal.partner_id.im_status, 'offline')
        partner = self.env['res.partner'].create({'name': 'Plain Partner'})
        self.assertEqual(partner.im_status, 'im_partner')
        store = Store(self.contact.partner_id.with_user(self.internal)).get_result()
        self.assertEqual(store['res.partner'][0]['im_status'], 'bot')

    # --- 34: push re-pins ---

    def test_push_pins_an_unpinned_chat(self):
        self.init_messaging(self.internal)
        chat = self.contact._find_chat(self.internal.partner_id)
        self.unpin(chat, self.internal)
        # what /woow_ai/v1/push does
        pushed, created = self.contact._get_or_create_chat(self.internal)
        self.contact._post_text(pushed, 'reminder')
        self.assertFalse(created)
        self.assertTrue(self.member(chat, self.internal).is_pinned)
        self.assertGreaterEqual(self.member(chat, self.internal).message_unread_counter, 1)

    # --- 37: menus ---

    def test_menus_under_discuss_configuration(self):
        config = self.env.ref('mail.menu_configuration')
        system = self.env.ref('base.group_system')
        for xmlid in ('menu_woow_ai_contact', 'menu_woow_ai_job', 'menu_woow_ai_call_log'):
            menu = self.env.ref('woow_ai_contact.%s' % xmlid)
            self.assertEqual(menu.parent_id, config, xmlid)
            self.assertEqual(menu.groups_id, system, xmlid)
        self.assertFalse(self.env.ref('woow_ai_contact.menu_woow_ai_root', raise_if_not_found=False))
        admin = new_test_user(self.env, 'ai_admin', groups='base.group_user,base.group_system')
        visible = self.env['ir.ui.menu'].with_user(admin)._visible_menu_ids()
        self.assertIn(self.env.ref('woow_ai_contact.menu_woow_ai_contact').id, visible)
        visible = self.env['ir.ui.menu'].with_user(self.internal)._visible_menu_ids()
        self.assertNotIn(self.env.ref('woow_ai_contact.menu_woow_ai_contact').id, visible)
        self.assertIn(config.id, visible, 'Discuss > Configuration itself is unchanged')

    def test_sidebar_assets_removed(self):
        paths = [path for path, *_rest in self.env['ir.asset']._get_asset_paths('web.assets_backend', {})]
        self.assertTrue(paths)
        self.assertFalse([p for p in paths if p.startswith('woow_ai_contact/')], 'no backend assets any more')
