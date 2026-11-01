# -*- coding: utf-8 -*-
from unittest.mock import patch

from psycopg2 import errors

from odoo import Command
from odoo.tests.common import new_test_user, tagged
from odoo.tools import mute_logger

from .common import BACKEND_POST, AiContactCase


@tagged('woow_ai_contact')
class TestTrigger(AiContactCase):

    def test_user_message_creates_job(self):
        chat = self.chat(self.internal)
        message = self.post(chat, self.internal, 'hello')
        job = self.jobs(chat)
        self.assertEqual(len(job), 1)
        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.kind, 'reply')
        self.assertEqual(job.contact_id, self.contact)
        self.assertEqual(job.message_id, message)
        self.assertTrue(job.run_after)

    def test_portal_user_message_creates_job(self):
        chat = self.chat(self.portal, self.n8n)
        self.post(chat, self.portal, '你好')
        self.assertEqual(len(self.jobs(chat)), 1)
        self.assertEqual(self.jobs(chat).contact_id, self.n8n)

    def test_attachment_only_message_creates_job(self):
        chat = self.chat(self.internal)
        att = self.attachment(chat, self.internal)
        message = self.post(chat, self.internal, '', attachments=att)
        self.assertEqual(message.attachment_ids, att)
        self.assertEqual(len(self.jobs(chat)), 1)

    def test_no_job_cases(self):
        chat = self.chat(self.internal)
        # empty message
        self.post(chat, self.internal, '')
        # notification, not a comment
        chat.message_post(body='note', author_id=self.internal.partner_id.id, message_type='notification')
        # the bot itself
        self.contact._post_text(chat, 'I am the bot')
        self.assertFalse(self.jobs(chat))
        # archived AI contact
        self.contact.active = False
        self.post(chat, self.internal, 'still there?')
        self.assertFalse(self.jobs(chat))

    def test_archived_contact_note_once_per_chat(self):
        # QA C: a user writing to an archived AI contact got silence
        self.env['res.lang']._activate_lang('zh_TW')
        self.internal.lang = 'zh_TW'
        chat = self.chat(self.internal)
        before = len(self.bot_messages(chat))
        self.contact.active = False
        with patch(BACKEND_POST) as post:
            self.post(chat, self.internal, 'still there?')
            self.post(chat, self.internal, 'hello?')
        post.assert_not_called()
        self.assertFalse(self.jobs(chat))
        notes = self.bot_messages(chat)[before:]
        self.assertEqual(len(notes), 1)  # once per conversation
        self.assertIn('這個 AI 聯絡人已停用。', notes.body)
        self.assertTrue(chat.woow_ai_inactive_notice_sent)
        # reactivated: answers again; archived again later: the note comes back once
        self.contact.active = True
        self.assertFalse(chat.woow_ai_inactive_notice_sent)
        self.post(chat, self.internal, 'back?')
        self.assertEqual(len(self.jobs(chat)), 1)
        self.contact.active = False
        self.post(chat, self.internal, 'gone again?')
        self.assertEqual(len(self.bot_messages(chat)[before:]), 2)

    def test_no_archived_note_in_human_chats(self):
        other = new_test_user(self.env, 'ai_other_note', groups='base.group_user')
        chat = self.env['discuss.channel'].with_user(self.internal).channel_get([other.partner_id.id])
        self.post(chat, self.internal, 'hi colleague')
        self.assertFalse(chat.woow_ai_inactive_notice_sent)
        self.assertEqual(chat.message_ids.author_id, self.internal.partner_id)

    def test_not_in_group_channel_or_human_chat(self):
        group = self.env['discuss.channel'].create({
            'name': 'group', 'channel_type': 'group',
            'channel_member_ids': [Command.create({'partner_id': self.internal.partner_id.id}),
                                   Command.create({'partner_id': self.contact.partner_id.id})],
        })
        self.post(group, self.internal, 'hello group')
        self.assertFalse(self.jobs(group))
        other = new_test_user(self.env, 'ai_other', groups='base.group_user')
        human_chat = self.env['discuss.channel'].with_user(self.internal).channel_get([other.partner_id.id])
        self.post(human_chat, self.internal, 'hi')
        self.assertFalse(self.jobs(human_chat))

    def test_denied_user_gets_refusal_without_backend_call(self):
        self.contact.allowed_group_ids = self.env.ref('base.group_portal')
        chat, _created = self.contact.sudo()._get_or_create_chat(self.internal)
        with patch(BACKEND_POST) as post:
            self.post(chat, self.internal, 'can I?')
        post.assert_not_called()
        self.assertFalse(self.jobs(chat))
        refusal = self.bot_messages(chat)
        self.assertEqual(len(refusal), 1)
        self.assertIn('permission', refusal.body)

    @mute_logger('odoo.addons.woow_ai_contact.models.discuss_channel')
    def test_trigger_failure_never_blocks_the_message(self):
        chat = self.chat(self.internal)
        with patch.object(type(self.env['woow.ai.job']), '_create_for_message', side_effect=RuntimeError('boom')):
            message = self.post(chat, self.internal, 'hello')
        self.assertTrue(message.exists())
        self.assertFalse(self.jobs(chat))

    def test_concurrency_errors_reach_odoo_retry(self):
        """Two people posting in the same channel at once (a group chat with a
        portal user, cs_portal_discuss): Odoo's own pending write (the
        channel's last_interest_dt) fails with a serialization error when the
        guard's savepoint flushes. Logging and swallowing it left the request
        in an aborted transaction (HTTP 500 for the sender); it must reach
        Odoo's request retry like without this module."""
        chat = self.chat(self.internal)
        for exc in (errors.SerializationFailure, errors.LockNotAvailable, errors.DeadlockDetected):
            with self.subTest(exc=exc.__name__), patch.object(
                    type(chat), '_woow_ai_handle_new_message', side_effect=exc('concurrent update')):
                with self.assertRaises(exc):
                    self.post(chat, self.internal, 'hello')
        self.assertFalse(self.jobs(chat))

    def test_messages_in_user_language(self):
        self.env['res.lang']._activate_lang('zh_TW')
        self.env['ir.module.module']._load_module_terms(['woow_ai_contact'], ['zh_TW'], overwrite=True)
        self.internal.lang = 'zh_TW'
        self.contact.allowed_group_ids = self.env.ref('base.group_portal')
        chat = self.chat(self.internal)
        self.post(chat, self.internal, '可以用嗎？')
        self.assertIn('您沒有使用這個 AI 聯絡人的權限', self.bot_messages(chat).body)
