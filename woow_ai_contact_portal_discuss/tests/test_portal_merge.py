# -*- coding: utf-8 -*-
import re
from datetime import timedelta
from unittest.mock import patch

from markupsafe import Markup

from odoo import Command, fields
from odoo.tests import HttpCase, new_test_user, tagged


@tagged('post_install', '-at_install', 'woow_ai_contact')
class TestPortalMerge(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.portal = new_test_user(cls.env, 'aipd_portal', groups='base.group_portal', name='Portal Pia')
        cls.internal = new_test_user(cls.env, 'aipd_internal', groups='base.group_user', name='Internal Ivy')
        groups = [cls.env.ref('base.group_user').id, cls.env.ref('base.group_portal').id]
        cls.advisor = cls.env['woow.ai.contact'].create({
            'name': 'Merge Advisor', 'backend_type': 'openai', 'endpoint_url': 'https://llm.example.com/v1',
            'welcome_message': '歡迎', 'description': 'Answers product questions',
            'allowed_group_ids': [Command.set(groups)]})
        cls.helper = cls.env['woow.ai.contact'].create({
            'name': 'Merge Helper', 'backend_type': 'openai', 'endpoint_url': 'https://llm.example.com/v1',
            'allowed_group_ids': [Command.set(groups)]})
        # a person-to-person chat, to tell both tabs apart
        cls.human_chat = cls.env['discuss.channel'].with_user(cls.internal).channel_get(
            [cls.portal.partner_id.id]).with_user(cls.env.user)
        cls.human_chat.with_user(cls.internal).message_post(body=Markup('Hi <b>Pia</b>'), message_type='comment')

    def setUp(self):
        super().setUp()
        # never start real worker threads from HTTP requests in tests
        patcher = patch.object(type(self.env['woow.ai.job']), '_start_after_commit')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.authenticate('aipd_portal', 'aipd_portal')

    def _page(self, url):
        response = self.url_open(url)
        self.assertEqual(response.status_code, 200, url)
        return response.text

    def _tabs(self, page):
        return re.findall(r'data-tab="([a-z_]+)"', page)

    def test_one_entry_on_the_portal_home(self):
        home = self._page('/my')
        self.assertIn('href="/my/discussions"', home)
        self.assertNotIn('href="/my/ai"', home)

    def test_ai_tab(self):
        page = self._page('/my/discussions')
        # the first portal page created the AI chats (onboarding)
        advisor_chat = self.advisor._find_chat(self.portal.partner_id)
        helper_chat = self.helper._find_chat(self.portal.partner_id)
        self.assertTrue(advisor_chat and helper_chat)
        self.assertEqual(self._tabs(page), ['all', 'ai', 'chat'])
        for chat in (advisor_chat, helper_chat, self.human_chat):
            self.assertIn('/discuss/channel/%s?' % chat.id, page)

        ai_page = self._page('/my/discussions?filterby=ai')
        back = 'back=/my/discussions%3Ffilterby%3Dai'
        self.assertIn('/discuss/channel/%s?discussions=1&amp;%s' % (advisor_chat.id, back), ai_page)
        self.assertIn('/discuss/channel/%s?discussions=1&amp;%s' % (helper_chat.id, back), ai_page)
        self.assertNotIn('/discuss/channel/%s?' % self.human_chat.id, ai_page)
        self.assertIn('AI Assistant', ai_page)
        self.assertNotIn('o_woow_ai_start_contact', ai_page)

        chat_page = self._page('/my/discussions?filterby=chat')
        self.assertIn('/discuss/channel/%s?' % self.human_chat.id, chat_page)
        self.assertNotIn('/discuss/channel/%s?' % advisor_chat.id, chat_page)

    def test_closed_ai_chat_is_offered_again(self):
        self._page('/my/discussions')
        chat = self.advisor._find_chat(self.portal.partner_id)
        member = chat.channel_member_ids.filtered(lambda m: m.partner_id == self.portal.partner_id)
        # after the welcome message (same second would still count as pinned)
        member.unpin_dt = fields.Datetime.now() + timedelta(minutes=1)
        self.env.flush_all()
        page = self._page('/my/discussions?filterby=ai')
        self.assertNotIn('/discuss/channel/%s?' % chat.id, page)
        self.assertIn('href="/my/ai/%s/open"' % self.advisor.id, page)
        self.assertIn('Answers product questions', page)
        self.assertNotIn('href="/my/ai/%s/open"' % self.helper.id, page)
        # opening it pins the chat again
        self.url_open('/my/ai/%s/open' % self.advisor.id, allow_redirects=False)
        self.assertIn('/discuss/channel/%s?' % chat.id, self._page('/my/discussions?filterby=ai'))

    def test_my_ai_leads_to_the_ai_tab(self):
        response = self.url_open('/my/ai', allow_redirects=False)
        self.assertIn(response.status_code, (302, 303))
        self.assertTrue(response.headers['Location'].endswith('/my/discussions?filterby=ai'))
        response = self.url_open('/my/ai/%s/open' % self.advisor.id, allow_redirects=False)
        chat = self.advisor._find_chat(self.portal.partner_id)
        location = response.headers['Location']
        self.assertTrue(location.endswith(
            '/discuss/channel/%s?discussions=1&back=/my/discussions%%3Ffilterby%%3Dai' % chat.id), location)
        page = self._page(location)
        self.assertIn('href="/my/discussions?filterby=ai"', page)
        self.assertIn('cs_discuss_back_bar', page)
