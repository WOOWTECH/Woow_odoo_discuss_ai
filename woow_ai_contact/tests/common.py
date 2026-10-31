# -*- coding: utf-8 -*-
"""Shared fixtures (no real HTTP, no real key: every request is mocked)."""

import base64
import json
from unittest.mock import MagicMock

from odoo.tests.common import TransactionCase, new_test_user

BACKEND_POST = 'odoo.addons.woow_ai_contact.models.ai_contact_backend.requests.post'
MEDIA_GET = 'odoo.addons.woow_ai_contact.tools.media.requests.get'
BACKEND_SLEEP = 'odoo.addons.woow_ai_contact.models.ai_contact_backend.time.sleep'

TEST_KEY = 'test-backend-key'  # placeholder, not a real key

# 1x1 transparent PNG
TINY_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')
TINY_PDF = b'%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n'


def http_response(status=200, json_data=None, text=None, headers=None, chunks=None):
    response = MagicMock()
    response.status_code = status
    response.headers = headers or {}
    if json_data is not None:
        response.json.return_value = json_data
        response.text = json.dumps(json_data)
    else:
        response.json.side_effect = ValueError('not json')
        response.text = text or ''
    response.iter_content.return_value = chunks or []
    return response


def openai_ok(content='您好，我是 AI。'):
    return http_response(200, {'choices': [{'message': {'role': 'assistant', 'content': content}}]})


class AiContactCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.internal = new_test_user(cls.env, 'ai_internal', groups='base.group_user', name='Internal Iris',
                                     email='iris@example.com')
        cls.portal = new_test_user(cls.env, 'ai_portal', groups='base.group_portal', name='Portal Pat',
                                   email='pat@example.com')
        cls.group_testers = cls.env['res.groups'].create({'name': 'AI testers'})
        cls.contact = cls.env['woow.ai.contact'].create({
            'name': 'Product Advisor',
            'backend_type': 'openai',
            'endpoint_url': 'https://llm.example.com/v1',
            'api_key': TEST_KEY,
            'model': 'test-model',
            'system_prompt': 'You are helpful.',
            'download_domains': 'files.example.com\n*.cdn.example.com',
        })
        cls.n8n = cls.env['woow.ai.contact'].create({
            'name': 'Workflow Bot',
            'backend_type': 'n8n',
            'endpoint_url': 'https://n8n.example.com/webhook/abc',
            'api_key': TEST_KEY,
            'download_domains': 'files.example.com',
        })
        cls.Job = cls.env['woow.ai.job']

    def chat(self, user, contact=None):
        chat, _created = (contact or self.contact)._get_or_create_chat(user)
        return chat

    def post(self, chat, user, body='hello', attachments=None, message_type='comment'):
        return chat.with_user(user).message_post(
            body=body, message_type=message_type, subtype_xmlid='mail.mt_comment',
            attachment_ids=(attachments or self.env['ir.attachment']).ids,
        )

    def attachment(self, chat, user, name='a.png', raw=TINY_PNG, mimetype=None):
        # pending upload (as the composer does): message_post links it to the channel
        vals = {'name': name, 'raw': raw, 'res_model': 'mail.compose.message', 'res_id': 0}
        if mimetype:
            vals['mimetype'] = mimetype
        return self.env['ir.attachment'].with_user(user).sudo().create(vals)

    def jobs(self, chat):
        return self.Job.search([('channel_id', '=', chat.id)], order='id')

    def bot_messages(self, chat, contact=None):
        return self.env['mail.message'].search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', chat.id),
            ('author_id', '=', (contact or self.contact).partner_id.id),
        ], order='id')
