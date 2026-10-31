# -*- coding: utf-8 -*-
"""Staged migration from im_livechat_ai 1.1.0 and coexistence.

Runs only in a database where im_livechat_ai 18.0.1.1.0 is installed
(sandbox db aicontact_lc_mig); skipped elsewhere. The 1.1.0 settings are
written with SQL like the migration reads them."""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.sql_db import db_connect
from odoo.tests.common import tagged

from odoo.addons.woow_ai_contact_livechat.models.im_livechat_channel import (
    DEFAULT_HANDOFF_KEYWORDS, LEGACY_DEFAULT_HANDOFF_KEYWORDS,
)
from odoo.addons.woow_ai_contact_livechat.models.legacy_migration import (
    LEGACY_ICP_KEYS, LEGACY_LOCK_NAMESPACE, MIGRATED_TIMEOUT_SECONDS, PRODUCT_GUARD_NO_TOOLS,
)

from .common import LivechatCase

LEGACY_KEY = 'sk-legacy-placeholder'  # not a real key


@tagged('woow_ai_contact_livechat', 'woow_ai_contact_livechat_migration', 'post_install', '-at_install')
class TestMigration(LivechatCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Migration = cls.env['woow.ai.livechat.migration']
        cls.legacy = cls.Migration._legacy_ready()[0]
        if not cls.legacy:
            return
        ICP = cls.env['ir.config_parameter'].sudo()
        ICP.set_param('im_livechat_ai.base_url', 'https://litellm.example.com/v1/chat/completions')
        ICP.set_param('im_livechat_ai.api_key', LEGACY_KEY)
        ICP.set_param('im_livechat_ai.log_retention_days', '14')
        cls.bot_a = cls.env['res.partner'].create({'name': '小幫手', 'active': False})
        Channel = cls.env['im_livechat.channel']
        cls.chan_a = Channel.create({'name': 'Legacy A', 'user_ids': [(4, cls.operator_user.id)]})
        cls.chan_b = Channel.create({'name': 'Legacy B', 'user_ids': [(4, cls.operator_user.id)]})
        cls.chan_c = Channel.create({'name': 'Legacy C', 'user_ids': [(4, cls.operator_user.id)]})
        cls.chan_d = Channel.create({'name': 'Never configured'})
        cls.env.flush_all()
        cr = cls.env.cr
        common = """ai_model = 'm1', ai_system_prompt = '你是客服。', ai_bot_name = '小幫手', ai_max_history = 30,
                    ai_max_retries = 2, ai_error_message = '抱歉', ai_handoff_pause_minutes = 15"""
        cr.execute("UPDATE im_livechat_channel SET ai_enabled = TRUE, ai_bot_partner_id = %%s, "
                   "ai_handoff_keywords = %%s, %s WHERE id = %%s" % common,
                   (cls.bot_a.id, LEGACY_DEFAULT_HANDOFF_KEYWORDS, cls.chan_a.id))
        cr.execute("UPDATE im_livechat_channel SET ai_enabled = FALSE, ai_handoff_keywords = '專人', "
                   "ai_handoff_message = '專人馬上來', %s WHERE id = %%s" % common, (cls.chan_b.id,))
        cr.execute("""UPDATE im_livechat_channel SET ai_enabled = FALSE, ai_model = 'm2', ai_system_prompt = '另一個',
                      ai_bot_name = NULL, ai_handoff_keywords = NULL WHERE id = %s""", (cls.chan_c.id,))
        cr.execute("UPDATE im_livechat_channel SET ai_enabled = FALSE, ai_system_prompt = NULL, "
                   "ai_bot_partner_id = NULL WHERE id = %s", (cls.chan_d.id,))
        Channel.invalidate_model()

    def setUp(self):
        super().setUp()
        if not self.legacy:
            self.skipTest('im_livechat_ai 18.0.1.1.0 is not installed in this database')

    def _legacy_enabled(self, channel):
        self.env.flush_all()
        self.env.cr.execute("SELECT ai_enabled FROM im_livechat_channel WHERE id = %s", (channel.id,))
        return self.env.cr.fetchone()[0]

    def _switch(self, channel):
        wizard = self.env['woow.ai.livechat.switch.wizard'].create({
            'livechat_channel_id': channel.id, 'price_regression_confirmed': True})
        return wizard.action_switch()

    # --- prepare ---

    def test_prepare_groups_channels_into_contacts(self):
        prepared = self.Migration._prepare()
        self.assertTrue({self.chan_a.id, self.chan_b.id, self.chan_c.id} <= set(prepared))
        self.assertNotIn(self.chan_d.id, prepared)
        contact = self.chan_a.woow_ai_contact_id
        self.assertEqual(self.chan_b.woow_ai_contact_id, contact)
        self.assertNotEqual(self.chan_c.woow_ai_contact_id, contact)
        self.assertFalse(self.chan_d.woow_ai_contact_id)
        self.assertEqual(self.chan_d.woow_ai_migration_state, 'none')
        self.assertEqual(contact.name, '小幫手')
        self.assertEqual(contact.backend_type, 'openai')
        self.assertEqual(contact.endpoint_url, 'https://litellm.example.com/v1')
        self.assertTrue(contact.api_key_set)
        self.assertEqual(contact._get_api_key(), LEGACY_KEY)
        self.assertEqual(contact.model, 'm1')
        self.assertEqual(contact.system_prompt, '你是客服。\n\n' + PRODUCT_GUARD_NO_TOOLS)
        self.assertEqual((contact.max_history, contact.max_retries, contact.error_message), (30, 2, '抱歉'))
        self.assertEqual(contact.timeout_seconds, MIGRATED_TIMEOUT_SECONDS)
        self.assertFalse(contact.allowed_group_ids)
        self.assertFalse(contact.log_payload)
        self.assertEqual(contact.partner_id, self.bot_a)  # same bot: same name and avatar
        self.assertFalse(self.bot_a.active)
        self.assertEqual(self.chan_c.woow_ai_contact_id.name, 'AI 客服助理')
        self.assertTrue(self.chan_c.woow_ai_contact_id.system_prompt.startswith('另一個\n\n'))
        # channels: staged, AI off, handoff settings, 1.1.0 untouched
        self.assertEqual((self.chan_a.woow_ai_migration_state, self.chan_a.woow_ai_enabled,
                          self.chan_a.woow_ai_legacy_enabled), ('staged', False, True))
        self.assertEqual(self.chan_a.woow_ai_legacy_bot_partner_id, self.bot_a)
        self.assertEqual(self.chan_a.woow_ai_pause_minutes, 15)
        self.assertEqual(self.chan_a.woow_ai_handoff_keywords, DEFAULT_HANDOFF_KEYWORDS)  # the broad default replaced
        self.assertIn('product tools', self.chan_a.woow_ai_migration_note)
        self.assertEqual((self.chan_b.woow_ai_handoff_keywords, self.chan_b.woow_ai_handoff_message),
                         ('專人', '專人馬上來'))
        self.assertFalse(self.chan_c.woow_ai_handoff_keywords)  # off in 1.1.0: stays off
        self.assertTrue(self._legacy_enabled(self.chan_a))

    def test_prepare_is_idempotent(self):
        self.Migration._prepare()
        count = self.env['woow.ai.contact'].search_count([])
        contact = self.chan_a.woow_ai_contact_id
        self.assertEqual(self.Migration._prepare(), {})
        self.assertEqual(self.env['woow.ai.contact'].search_count([]), count)
        self.assertEqual(self.chan_a.woow_ai_contact_id, contact)

    def test_prepare_refuses_old_versions_and_leaked_key_columns(self):
        self.env.cr.execute("UPDATE ir_module_module SET latest_version = '18.0.1.0.0' WHERE name = 'im_livechat_ai'")
        self.assertEqual(self.Migration._prepare(), {})
        with self.assertRaises(UserError):
            self.chan_a.action_woow_ai_legacy_prepare()
        self.env.cr.execute("UPDATE ir_module_module SET latest_version = '18.0.1.1.0' WHERE name = 'im_livechat_ai'")
        self.env.cr.execute("ALTER TABLE im_livechat_channel ADD COLUMN ai_api_key VARCHAR")
        self.assertEqual(self.Migration._prepare(), {})
        self.assertFalse(self.chan_a.woow_ai_contact_id)

    def test_prepare_without_url_or_key_marks_them(self):
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('im_livechat_ai.base_url', 'http://plain.example.com/v1')  # not https: never used
        ICP.set_param('im_livechat_ai.api_key', False)
        self.Migration._prepare()
        contact = self.chan_a.woow_ai_contact_id
        self.assertFalse(contact.endpoint_url)
        self.assertFalse(contact.api_key_set)
        self.assertIn('endpoint URL', self.chan_a.woow_ai_migration_note)
        with self.assertRaises(UserError):
            self._switch(self.chan_a)

    # --- coexistence ---

    def test_coexistence_never_two_answers(self):
        self.Migration._prepare()
        session = self.new_session(visitor=self.visitor, livechat=self.chan_a)
        with self.assertRaises(ValidationError):
            self.chan_a.write({'woow_ai_enabled': True})
        # even forced on: im_livechat_ai still answers, the AI contact stays silent
        self.env.cr.execute("UPDATE im_livechat_channel SET woow_ai_enabled = TRUE WHERE id = %s", (self.chan_a.id,))
        self.chan_a.invalidate_recordset()
        self.post(session, self.visitor, '你好')
        self.assertFalse(self.jobs(session))
        legacy_jobs = self.env['im_livechat.ai.job'].search([('discuss_channel_id', '=', session.id)])
        self.assertEqual(len(legacy_jobs), 1)
        # and the other way round: im_livechat_ai cannot be turned on where the AI contact answers
        self._legacy_off_and_switch_check()

    def _legacy_off_and_switch_check(self):
        self.env.cr.execute("UPDATE im_livechat_channel SET woow_ai_enabled = FALSE WHERE id = %s", (self.chan_a.id,))
        self.chan_a.invalidate_recordset()
        self._switch(self.chan_a)
        self.assertTrue(self.chan_a.woow_ai_enabled)
        with self.assertRaises(ValidationError):
            self.chan_a.write({'ai_enabled': True})

    # --- switch ---

    def test_switch_needs_confirmation_and_backend(self):
        self.Migration._prepare()
        wizard = self.env['woow.ai.livechat.switch.wizard'].create({'livechat_channel_id': self.chan_a.id})
        with self.assertRaises(UserError):
            wizard.action_switch()
        self.chan_a.woow_ai_contact_id.action_clear_api_key()
        with self.assertRaises(UserError):
            self._switch(self.chan_a)
        with self.assertRaises(UserError):
            self._switch(self.chan_d)  # never prepared

    def test_switch_moves_pause_and_fresh_jobs(self):
        self.Migration._prepare()
        paused = self.new_session(visitor=self.portal, livechat=self.chan_a, name='paused')
        waiting = self.new_session(visitor=self.visitor, livechat=self.chan_a, name='waiting')
        legacy_until = fields.Datetime.now() + timedelta(minutes=20)
        self.env.flush_all()
        self.env.cr.execute("UPDATE discuss_channel SET ai_paused_until = %s WHERE id = %s", (legacy_until, paused.id))
        message = self.post(waiting, self.visitor, '請問有貨嗎？')  # 1.1.0 queues its job
        legacy_job = self.env['im_livechat.ai.job'].search([('discuss_channel_id', '=', waiting.id)])
        self.assertEqual((len(legacy_job), legacy_job.state), (1, 'pending'))
        self.assertFalse(self.jobs(waiting))
        self._switch(self.chan_a)
        self.assertFalse(self._legacy_enabled(self.chan_a))
        self.assertTrue(self.chan_a.woow_ai_enabled)
        self.assertEqual(self.chan_a.woow_ai_migration_state, 'switched')
        paused.invalidate_recordset()
        self.assertEqual(paused.woow_ai_paused_until, legacy_until)
        moved = self.jobs(waiting)
        self.assertEqual((len(moved), moved.kind, moved.message_id), (1, 'reply', message))
        legacy_job.invalidate_recordset()
        self.assertEqual(legacy_job.state, 'skipped')
        self.assertIn('Switched', self.chan_a.woow_ai_migration_note)
        # from now on the AI contact answers, 1.1.0 does not
        self.post(waiting, self.visitor, '還有一個問題')
        self.assertEqual(len(self.env['im_livechat.ai.job'].search([('discuss_channel_id', '=', waiting.id)])), 1)
        self.assertEqual(len(self.jobs(waiting)), 2)

    def test_switch_keeps_off_channels_off(self):
        self.Migration._prepare()
        self._switch(self.chan_b)
        self.assertFalse(self.chan_b.woow_ai_enabled)
        self.assertEqual(self.chan_b.woow_ai_migration_state, 'switched')

    def test_switch_leaves_conversations_1_1_0_is_answering(self):
        self.Migration._prepare()
        session = self.new_session(visitor=self.visitor, livechat=self.chan_a)
        self.post(session, self.visitor, '你好')
        legacy_job = self.env['im_livechat.ai.job'].search([('discuss_channel_id', '=', session.id)])
        self.env.flush_all()
        # im_livechat_ai is processing this conversation: its session lock, held by another
        # connection (a real one: registry.cursor() shares the test connection)
        other = db_connect(self.env.cr.dbname).cursor()
        try:
            other.execute("SELECT pg_advisory_lock(%s, %s)", (LEGACY_LOCK_NAMESPACE, session.id))
            result = self.Migration._switch(self.chan_a, price_regression_confirmed=True)
        finally:
            other.execute("SELECT pg_advisory_unlock(%s, %s)", (LEGACY_LOCK_NAMESPACE, session.id))
            other.rollback()
            other.close()
        self.assertEqual(result, {'moved': 0, 'busy': 1})
        self.assertFalse(self.jobs(session))
        legacy_job.invalidate_recordset()
        self.assertEqual(legacy_job.state, 'pending')  # 1.1.0 answers it once

    def test_switch_back(self):
        self.Migration._prepare()
        self._switch(self.chan_a)
        self.chan_a.action_woow_ai_legacy_switch_back()
        self.assertTrue(self._legacy_enabled(self.chan_a))
        self.assertFalse(self.chan_a.woow_ai_enabled)
        self.assertEqual(self.chan_a.woow_ai_migration_state, 'staged')

    def test_old_ai_messages_stay_assistant(self):
        self.Migration._prepare()
        self._switch(self.chan_a)
        session = self.new_session(visitor=self.visitor, livechat=self.chan_a)
        shared = self.env.ref('im_livechat_ai.partner_ai_bot')
        self.post(session, self.bot_a, '舊的 AI 回答')
        self.post(session, shared, '共用機器人的回答')
        self.post(session, self.visitor, '謝謝')
        contact = self.chan_a.woow_ai_contact_id
        history = session._woow_ai_history(contact, session._woow_ai_context_messages(contact))
        self.assertEqual([e['role'] for e in history], ['assistant', 'assistant', 'user'])
        self.assertEqual(len(self.jobs(session)), 1)

    # --- finish ---

    def test_finish(self):
        self.Migration._prepare()
        with self.assertRaises(UserError):
            self.Migration._finish()  # nothing switched yet
        Channel = self.env['im_livechat.channel']
        for channel in Channel.search([('woow_ai_migration_state', '=', 'staged')]):
            contact = channel.woow_ai_contact_id
            if not contact.endpoint_url:
                contact.endpoint_url = 'https://litellm.example.com/v1'
            if not contact.api_key_set:
                contact.api_key = LEGACY_KEY
            self.Migration._switch(channel, price_regression_confirmed=True)
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_enabled = FALSE")
        self.env.cr.execute("UPDATE im_livechat_ai_job SET state = 'skipped' WHERE state IN ('pending', 'running')")
        shared = self.env.ref('im_livechat_ai.partner_ai_bot')
        result = self.Migration._finish()
        self.assertGreaterEqual(result['channels'], 3)
        self.assertEqual(self.chan_a.woow_ai_migration_state, 'finalized')
        ICP = self.env['ir.config_parameter'].sudo()
        for key in LEGACY_ICP_KEYS:
            self.assertFalse(ICP.get_param(key), key)
        self.assertEqual(self.env.ref('woow_ai_contact_livechat.legacy_partner_ai_bot'), shared)
        self.assertFalse(self.env.ref('im_livechat_ai.partner_ai_bot', raise_if_not_found=False))
        self.assertTrue(self.env.ref('woow_ai_contact_livechat.legacy_partner_category_ai_bot'))
        data = self.env['ir.model.data'].search([('module', '=', 'woow_ai_contact_livechat'),
                                                 ('name', '=', 'legacy_partner_ai_bot')])
        self.assertTrue(data.noupdate)
        # the shared bot is still a bot after the xmlid moved
        session = self.new_session(visitor=self.visitor, livechat=self.chan_a)
        self.post(session, shared, '舊回答')
        self.assertFalse(self.jobs(session))
        # uninstall of this module: the bot is kept under __export__
        self.Migration._release_legacy_xmlids()
        self.assertEqual(self.env.ref('__export__.woow_ai_contact_livechat_legacy_partner_ai_bot'), shared)

    def test_finish_blocked_by_running_1_1_0(self):
        self.Migration._prepare()
        for channel in self.env['im_livechat.channel'].search([('woow_ai_migration_state', '=', 'staged')]):
            contact = channel.woow_ai_contact_id
            contact.write({'endpoint_url': contact.endpoint_url or 'https://litellm.example.com/v1'})
            if not contact.api_key_set:
                contact.api_key = LEGACY_KEY
            self.Migration._switch(channel, price_regression_confirmed=True)
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_enabled = TRUE WHERE id = %s", (self.chan_d.id,))
        with self.assertRaises(UserError):
            self.Migration._finish()
