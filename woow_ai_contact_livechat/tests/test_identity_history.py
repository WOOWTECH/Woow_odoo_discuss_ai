# -*- coding: utf-8 -*-
"""Who wrote what (visitor / operator / bot), history roles, system prompt,
visitor identity, n8n channel object, links and attachments."""

import json
from unittest.mock import patch

from odoo.tests.common import new_test_user, tagged

from odoo.addons.woow_ai_contact.tests.common import TINY_PDF, TINY_PNG
from odoo.addons.woow_ai_contact_livechat.models.discuss_channel import OPERATOR_SYSTEM_NOTE

from .common import BACKEND_POST, TEST_KEY, LivechatCase, http_response, openai_ok


@tagged('woow_ai_contact_livechat', 'post_install', '-at_install')
class TestAuthorKind(LivechatCase):

    def _kind(self, session, author=None):
        message = self.env['mail.message'].create({
            'body': 'x', 'model': 'discuss.channel', 'res_id': session.id, 'message_type': 'comment',
            'author_id': author.id if author else False,
        })
        return session._woow_ai_author_kind(message)

    def test_kinds(self):
        session = self.new_session(visitor=self.visitor)
        self.assertEqual(self._kind(session), 'visitor')  # no author (e.g. a connector)
        self.assertEqual(self._kind(session, self.visitor), 'visitor')
        self.assertEqual(self._kind(session, self.portal), 'visitor')
        self.assertEqual(self._kind(session, self.env.ref('base.public_user').partner_id), 'visitor')
        self.assertEqual(self._kind(session, self.operator), 'operator')
        self.assertEqual(self._kind(session, self.manager_user.partner_id), 'operator')
        self.assertEqual(self._kind(session, self.bot), 'bot')
        self.assertEqual(self._kind(session, self.env.ref('base.partner_root')), 'bot')

    def test_archived_internal_user_is_still_an_operator(self):
        user = new_test_user(self.env, login='lc_archived', groups='base.group_user')
        user.active = False
        session = self.new_session(visitor=self.visitor)
        self.assertEqual(self._kind(session, user.partner_id), 'operator')

    def test_employee_chatting_on_the_website_is_a_visitor(self):
        employee = new_test_user(self.env, login='lc_employee', name='Employee Eve', groups='base.group_user')
        session = self.new_session(visitor=employee.partner_id)
        self.assertEqual(self._kind(session, employee.partner_id), 'visitor')
        self.post(session, employee.partner_id, '測試一下')
        self.assertEqual(len(self.jobs(session)), 1)
        self.assertFalse(session.woow_ai_paused_until)

    def test_previous_bot_partners_are_bots(self):
        category = self.env['res.partner.category'].create({'name': 'AI 機器人'})
        self.env['ir.model.data'].create({
            'module': 'woow_ai_contact_livechat', 'name': 'legacy_partner_category_ai_bot',
            'model': 'res.partner.category', 'res_id': category.id, 'noupdate': True})
        tagged_bot = self.env['res.partner'].create({'name': 'Old bot', 'active': False,
                                                    'category_id': [(4, category.id)]})
        channel_bot = self.env['res.partner'].create({'name': 'Channel bot', 'active': False})
        self.livechat.with_context(woow_ai_migration=True).woow_ai_legacy_bot_partner_id = channel_bot
        session = self.new_session(visitor=self.visitor)
        self.assertEqual(self._kind(session, tagged_bot), 'bot')
        self.assertEqual(self._kind(session, channel_bot), 'bot')

    def test_visitor_member_ignores_later_operators(self):
        guest = self.new_guest()
        session = self.new_session(guest=guest)
        self.assertEqual(session._woow_ai_visitor_member().guest_id, guest)
        session = self.new_session(visitor=self.visitor)
        self.env.flush_all()
        self.env.cr.execute("UPDATE discuss_channel SET create_date = create_date - interval '1 hour' WHERE id = %s",
                            (session.id,))
        session.invalidate_recordset(['create_date'])
        # every member is now "later" than the session: none is the visitor
        self.assertFalse(session._woow_ai_visitor_member())


@tagged('woow_ai_contact_livechat', 'post_install', '-at_install')
class TestHistory(LivechatCase):

    def test_roles_prefix_and_colleague_note(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '燈泡多少錢？')
        self.post(session, self.bot, 'AI 的回答')
        self.post(session, self.operator, '我幫您查')
        self.post(session, self.visitor, '謝謝')
        history = session._woow_ai_history(self.contact, session._woow_ai_context_messages(self.contact))
        self.assertEqual([(e['role'], e['text']) for e in history], [
            ('user', '燈泡多少錢？'),
            ('assistant', 'AI 的回答'),
            ('assistant', '[真人客服 客服小明] 我幫您查'),
            ('user', '謝謝'),
        ])
        prompt = session._woow_ai_system_prompt(self.contact, history)
        self.assertEqual(prompt, 'You are the shop assistant.\n\n' + OPERATOR_SYSTEM_NOTE)

    def test_no_colleague_note_without_operator_messages(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '你好')
        history = session._woow_ai_history(self.contact, session._woow_ai_context_messages(self.contact))
        self.assertEqual(session._woow_ai_system_prompt(self.contact, history), 'You are the shop assistant.')

    def test_colleague_note_without_custom_prompt(self):
        self.contact.system_prompt = False
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.operator, '我來')
        session.woow_ai_paused_until = False
        self.post(session, self.visitor, '好')
        history = session._woow_ai_history(self.contact, session._woow_ai_context_messages(self.contact))
        self.assertEqual(session._woow_ai_system_prompt(self.contact, history), OPERATOR_SYSTEM_NOTE)

    def test_operator_name_falls_back_to_partner_name(self):
        user = new_test_user(self.env, login='lc_noname', name='李大華', groups='base.group_user')
        session = self.new_session(visitor=self.visitor)
        self.post(session, user.partner_id, '我來')
        history = session._woow_ai_history(self.contact, session._woow_ai_context_messages(self.contact))
        self.assertEqual(history[-1]['text'], '[真人客服 李大華] 我來')

    def test_job_sends_operator_as_assistant(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '有貨嗎？')
        self.post(session, self.operator, '請稍等')
        session.woow_ai_paused_until = False
        self.post(session, self.visitor, '好')
        with patch(BACKEND_POST, return_value=openai_ok('好的')) as post:
            self.run_job(self.jobs(session)[-1:])  # the newest job answers the whole conversation
        messages = self.request_body(post)['messages']
        self.assertIn(OPERATOR_SYSTEM_NOTE, messages[0]['content'])
        self.assertEqual(messages[1], {'role': 'user', 'content': '有貨嗎？'})
        self.assertEqual(messages[2], {'role': 'assistant', 'content': '[真人客服 客服小明] 請稍等'})
        self.assertEqual(messages[-1], {'role': 'user', 'content': '好'})


@tagged('woow_ai_contact_livechat', 'post_install', '-at_install')
class TestIdentity(LivechatCase):

    def test_guest_identity(self):
        guest = self.new_guest()
        session = self.new_session(guest=guest)
        identity = session._woow_ai_identity(self.contact)
        self.assertEqual(identity, {
            'user_id': None, 'partner_id': None, 'name': '訪客 Amy', 'email': '', 'lang': 'zh_TW',
            'tz': 'Asia/Taipei', 'is_portal': False, 'company_id': None, 'is_guest': True,
            'guest_id': guest.id, 'visitor_key': 'guest:%s' % guest.id,
        })
        self.assertEqual(session._woow_ai_visitor_kind(), 'guest')
        self.assertIn('website visitor (guest)', self.contact._identity_system_text(identity))

    def test_portal_identity(self):
        session = self.new_session(visitor=self.portal)
        identity = session._woow_ai_identity(self.contact)
        self.assertEqual((identity['user_id'], identity['partner_id'], identity['is_portal'], identity['is_guest']),
                         (self.portal_user.id, self.portal.id, True, False))
        self.assertEqual(identity['email'], 'pat@example.com')
        self.assertEqual(identity['visitor_key'], 'partner:%s' % self.portal.id)
        self.assertEqual(session._woow_ai_visitor_kind(), 'portal')
        # owner decision 27: the model never sees the email
        self.assertNotIn('pat@example.com', self.contact._identity_system_text(identity))

    def test_employee_partner_and_public_identity(self):
        employee = new_test_user(self.env, login='lc_emp2', groups='base.group_user')
        session = self.new_session(visitor=employee.partner_id)
        self.assertEqual(session._woow_ai_identity(self.contact)['user_id'], employee.id)
        self.assertEqual(session._woow_ai_visitor_kind(), 'employee')
        session = self.new_session(visitor=self.visitor)
        identity = session._woow_ai_identity(self.contact)
        self.assertEqual((identity['user_id'], identity['partner_id'], identity['lang']), (None, self.visitor.id, 'en_US'))
        self.assertEqual(session._woow_ai_visitor_kind(), 'partner')
        session = self.new_session(visitor=self.env.ref('base.public_user').partner_id)
        identity = session._woow_ai_identity(self.contact)
        self.assertEqual((identity['partner_id'], identity['name'], identity['is_guest'], identity['visitor_key']),
                         (None, '', True, 'public'))
        self.assertEqual(session._woow_ai_visitor_kind(), 'public')

    def test_openai_request_identity_and_session(self):
        guest = self.new_guest()
        session = self.new_session(guest=guest)
        self.post_guest(session, guest)
        with patch(BACKEND_POST, return_value=openai_ok('您好')) as post:
            self.run_job(self.jobs(session))
        body = self.request_body(post)
        self.assertEqual(body['user'], self.env['woow.ai.contact']._session_id(session))
        self.assertEqual(body['metadata']['odoo_is_guest'], 'true')
        self.assertEqual(body['metadata']['odoo_visitor_key'], 'guest:%s' % guest.id)
        self.assertIn('- name: 訪客 Amy', body['messages'][0]['content'])
        dumped = json.dumps(body, ensure_ascii=False)
        for forbidden in ('ip_address', 'visitor_id', 'page'):
            self.assertNotIn(forbidden, dumped)

    def test_n8n_channel_object(self):
        n8n = self.env['woow.ai.contact'].create({
            'name': 'Flow', 'backend_type': 'n8n', 'endpoint_url': 'https://n8n.example.com/webhook/x',
            'api_key': TEST_KEY, 'allowed_group_ids': [(5, 0, 0)]})
        self.livechat.woow_ai_contact_id = n8n
        guest = self.new_guest()
        session = self.new_session(guest=guest)
        session.country_id = self.env.ref('base.tw')
        message = self.post_guest(session, guest, '你好')
        history = session._woow_ai_history(n8n, session._woow_ai_context_messages(n8n))
        _url, headers, _body, payload = n8n._build_request(session, history, message)
        self.assertNotIn('Authorization', headers)
        self.assertEqual(payload['channel'], {
            'id': session.id, 'type': 'livechat',
            'livechat': {
                'livechat_channel_id': self.livechat.id, 'livechat_channel_name': 'Website',
                'operator_name': '客服小明', 'country_code': 'TW', 'visitor_kind': 'guest',
            },
        })
        self.assertEqual(payload['user']['visitor_key'], 'guest:%s' % guest.id)
        self.assertEqual(payload['message']['text'], '你好')

    def test_error_message_in_guest_language(self):
        guest = self.new_guest(lang='en_US')
        session = self.new_session(guest=guest)
        self.post_guest(session, guest)
        job = self.jobs(session)
        self.assertEqual(session._woow_ai_identity(self.contact)['lang'], 'en_US')
        self.assertTrue(job._error_text())

    def test_uninstalled_guest_language_falls_back(self):
        """Sandbox finding: a guest language that is not installed made the
        translated error message raise and the job stay "running"."""
        inactive = self.env['res.lang'].with_context(active_test=False).search(
            [('active', '=', False), ('code', '!=', 'en_US')], limit=1)
        self.operator.lang = 'zh_TW'
        guest = self.new_guest(lang=inactive.code)
        session = self.new_session(guest=guest)
        self.post_guest(session, guest)
        self.assertEqual(session._woow_ai_identity(self.contact)['lang'], 'zh_TW')  # the operator's language
        job = self.jobs(session)
        with patch(BACKEND_POST, return_value=http_response(500, {'error': 'x'})):
            self.run_job(job)
        self.assertEqual(job.state, 'failed')
        self.assertTrue(self.bot_messages(session))


@tagged('woow_ai_contact_livechat', 'post_install', '-at_install')
class TestLinksAndFiles(LivechatCase):

    def test_only_site_links_are_clickable(self):
        session = self.new_session(visitor=self.visitor)
        prepared = self.contact._prepare_reply(
            {'text': '看 [商品](https://shop.example.com/shop/p1) 或 [這裡](https://evil.example.net/login)'},
            channel=session)
        html = str(prepared['html'])
        self.assertIn('href="https://shop.example.com/shop/p1"', html)
        self.assertNotIn('href="https://evil.example.net', html)
        self.assertIn('這裡', html)
        # other channels keep the core default (any http/https link)
        chat = self.env['discuss.channel'].create({'name': 'chat', 'channel_type': 'chat'})
        self.assertIsNone(chat._woow_ai_link_hosts(self.contact))

    def test_site_hosts_include_websites(self):
        hosts = self.env['im_livechat.channel']._woow_ai_site_hosts()
        self.assertIn('shop.example.com', hosts)
        if 'website' in self.env:
            self.env['website'].create({'name': 'Second', 'domain': 'https://www.second.example.org'})
            self.assertIn('www.second.example.org', self.env['im_livechat.channel']._woow_ai_site_hosts())

    def test_guest_image_inlined_and_pdf_linked(self):
        session = self.new_session(visitor=self.visitor)
        image = self.env['ir.attachment'].create({'name': 'a.png', 'raw': TINY_PNG, 'res_model': 'mail.compose.message',
                                                  'res_id': 0})
        pdf = self.env['ir.attachment'].create({'name': 'spec.pdf', 'raw': TINY_PDF, 'res_model': 'mail.compose.message',
                                                'res_id': 0})
        self.post(session, self.visitor, '這個型號？', attachments=image | pdf)
        with patch(BACKEND_POST, return_value=openai_ok('是的')) as post:
            self.run_job(self.jobs(session))
        last = self.request_body(post)['messages'][-1]
        self.assertEqual(last['role'], 'user')
        parts = {part['type']: part for part in last['content']}
        self.assertTrue(parts['image_url']['image_url']['url'].startswith('data:image/png;base64,'))
        self.assertIn('spec.pdf', parts['text']['text'])
        self.assertEqual(parts['file']['file']['filename'], 'spec.pdf')  # document_mode file (default)
        # document_mode link: the signed link
        self.contact.document_mode = 'link'
        self.post(session, self.visitor, '再看一次')
        with patch(BACKEND_POST, return_value=openai_ok('好')) as post:
            self.run_job(self.jobs(session)[-1])
        earlier = next(m for m in self.request_body(post)['messages'] if 'spec.pdf' in self.text_parts(m))
        self.assertIn('/woow_ai/attachment/%s/' % pdf.id, self.text_parts(earlier))

    @staticmethod
    def text_parts(turn):
        content = turn['content']
        return content if isinstance(content, str) else '\n'.join(p.get('text', '') for p in content)

    def test_ai_off_closes_signed_downloads(self):
        session = self.new_session(visitor=self.visitor)
        self.assertEqual(session._woow_ai_channel_contact(), self.contact)
        self.livechat.woow_ai_enabled = False
        self.assertFalse(session._woow_ai_channel_contact())
