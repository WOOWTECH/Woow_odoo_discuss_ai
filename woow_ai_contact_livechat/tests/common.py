# -*- coding: utf-8 -*-
"""Shared fixtures for the Live Chat bridge (every HTTP request is mocked, no real key)."""

import json

from odoo import Command
from odoo.tests.common import TransactionCase, new_test_user

from odoo.addons.woow_ai_contact.tests.common import BACKEND_POST, TEST_KEY, http_response, openai_ok  # noqa: F401


class LivechatCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.env['ir.config_parameter'].sudo().set_param('web.base.url', 'https://shop.example.com')
        for code in ('zh_TW', 'en_US'):  # whatever the database languages are
            cls.env['res.lang']._activate_lang(code)
        cls.contact = cls.env['woow.ai.contact'].create({
            'name': 'AI 客服助理',
            'backend_type': 'openai',
            'endpoint_url': 'https://llm.example.com/v1',
            'api_key': TEST_KEY,
            'model': 'test-model',
            'system_prompt': 'You are the shop assistant.',
            'openai_stream': False,
            'allowed_group_ids': [Command.clear()],  # Live Chat only
        })
        cls.bot = cls.contact.partner_id
        cls.operator_user = new_test_user(
            cls.env, login='lc_operator', name='王小明', email='op@example.com',
            groups='base.group_user,im_livechat.im_livechat_group_user')
        cls.operator_user.sudo().livechat_username = '客服小明'
        cls.operator = cls.operator_user.partner_id
        cls.manager_user = new_test_user(
            cls.env, login='lc_manager', name='Live Chat Manager',
            groups='base.group_user,im_livechat.im_livechat_group_manager')
        cls.portal_user = new_test_user(cls.env, login='lc_portal', name='入口網站客戶', email='pat@example.com',
                                        groups='base.group_portal')
        cls.portal = cls.portal_user.partner_id
        cls.visitor = cls.env['res.partner'].create({'name': 'Website Visitor', 'email': 'v@example.com',
                                                     'lang': 'en_US'})
        cls.livechat = cls.env['im_livechat.channel'].create({
            'name': 'Website',
            'user_ids': [Command.link(cls.operator_user.id)],
            'woow_ai_enabled': True,
            'woow_ai_contact_id': cls.contact.id,
        })
        cls.Job = cls.env['woow.ai.job']

    # --- conversations ---

    @classmethod
    def new_session(cls, visitor=None, guest=None, operator=None, livechat=None, name='Visitor Session'):
        operator = operator or cls.operator
        session = cls.env['discuss.channel'].create({
            'name': name,
            'channel_type': 'livechat',
            'livechat_operator_id': operator.id,
            'livechat_channel_id': (livechat or cls.livechat).id,
            'channel_member_ids': [Command.create({'partner_id': operator.id})],
        })
        if guest:
            cls.env['discuss.channel.member'].create({'channel_id': session.id, 'guest_id': guest.id})
        elif visitor:
            cls.env['discuss.channel.member'].create({'channel_id': session.id, 'partner_id': visitor.id})
        return session

    def new_guest(self, name='訪客 Amy', lang='zh_TW', timezone='Asia/Taipei'):
        return self.env['mail.guest'].create({'name': name, 'lang': lang, 'timezone': timezone})

    def post(self, session, author, body='請問營業時間？', message_type='comment', attachments=None):
        return session.message_post(
            body=body, author_id=author.id, message_type=message_type, subtype_xmlid='mail.mt_comment',
            attachment_ids=(attachments or self.env['ir.attachment']).ids)

    def post_guest(self, session, guest, body='請問營業時間？'):
        public = self.env.ref('base.public_user')
        # like the Live Chat controller: guests post through sudo with the guest in the context
        message = session.with_user(public).with_context(guest=guest).sudo().message_post(
            body=body, message_type='comment', subtype_xmlid='mail.mt_comment')
        self.assertEqual(message.author_guest_id, guest)
        return message

    def jobs(self, session):
        return self.Job.search([('channel_id', '=', session.id)], order='id')

    def bot_messages(self, session, partner=None):
        return self.env['mail.message'].search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', session.id),
            ('author_id', '=', (partner or self.bot).id),
        ], order='id')

    def odoobot_messages(self, partner):
        odoobot = self.env.ref('base.partner_root')
        chats = self.env['discuss.channel'].search([
            ('channel_type', '=', 'chat'),
            ('channel_member_ids.partner_id', '=', partner.id),
            ('channel_member_ids.partner_id', '=', odoobot.id),
        ])
        return self.env['mail.message'].search([
            ('model', '=', 'discuss.channel'), ('res_id', 'in', chats.ids), ('author_id', '=', odoobot.id),
        ], order='id')

    @staticmethod
    def notifications(bus_mock):
        """payloads of the simple_notification bus sends (other bus sends ignored)"""
        return [c.args[1] for c in bus_mock.call_args_list if c.args and c.args[0] == 'simple_notification']

    @staticmethod
    def run_job(job):
        job._run_job(commit=False)
        return job

    @staticmethod
    def request_body(post_mock, index=-1):
        return json.loads(post_mock.call_args_list[index][1]['data'])
