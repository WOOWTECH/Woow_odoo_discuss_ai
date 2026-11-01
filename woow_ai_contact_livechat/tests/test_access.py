# -*- coding: utf-8 -*-
"""Who may configure what (owner decision 25: Live Chat managers pick any AI contact)."""

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import tagged

from .common import LivechatCase


@tagged('woow_ai_contact_livechat', 'post_install', '-at_install')
class TestAccess(LivechatCase):

    def test_manager_sees_every_contact_but_not_the_backend(self):
        internal_only = self.env['woow.ai.contact'].create({
            'name': 'Internal Helper', 'endpoint_url': 'https://internal.example.com/v1'})
        Contact = self.env['woow.ai.contact'].with_user(self.manager_user)
        found = Contact.search([])
        self.assertIn(self.contact, found)  # Live Chat only (no allowed groups)
        self.assertIn(internal_only, found)
        self.assertEqual(Contact.browse(self.contact.id).name, 'AI 客服助理')
        with self.assertRaises(AccessError):
            Contact.browse(self.contact.id).read(['endpoint_url'])
        with self.assertRaises(AccessError):
            Contact.browse(self.contact.id).write({'name': 'renamed'})

    def test_contact_list_for_managers_not_operators(self):
        # QA C: the AI contact list / form is for administrators; Live Chat managers pick a channel's contact
        spec = {'name': {}}
        rows = self.env['woow.ai.contact'].with_user(self.manager_user).web_search_read(
            [('id', '=', self.contact.id)], spec)
        self.assertEqual(rows['records'][0]['name'], 'AI 客服助理')
        with self.assertRaises(AccessError):
            self.env['woow.ai.contact'].with_user(self.operator_user).web_search_read([], spec)
        # the channel form still shows the chosen contact's name to managers
        values = self.livechat.with_user(self.manager_user).web_read(
            {'woow_ai_contact_id': {'fields': {'display_name': {}}}})
        self.assertEqual(values[0]['woow_ai_contact_id']['display_name'], 'AI 客服助理')

    def test_manager_configures_the_channel(self):
        other = self.env['woow.ai.contact'].create({'name': 'Other', 'endpoint_url': 'https://o.example.com/v1'})
        channel = self.livechat.with_user(self.manager_user)
        channel.write({'woow_ai_contact_id': other.id, 'woow_ai_pause_minutes': 10,
                       'woow_ai_handoff_keywords': '找真人'})
        self.assertEqual(self.livechat.woow_ai_contact_id, other)

    def test_operators_cannot_configure(self):
        channel = self.livechat.with_user(self.operator_user)
        with self.assertRaises(AccessError):
            channel.write({'woow_ai_enabled': False})
        with self.assertRaises(AccessError):
            channel.read(['woow_ai_contact_id'])

    def test_constraints(self):
        with self.assertRaises(ValidationError):
            self.livechat.write({'woow_ai_contact_id': False})
        with self.assertRaises(ValidationError):
            self.livechat.write({'woow_ai_enabled': False, 'woow_ai_pause_minutes': 0})
        with self.assertRaises(ValidationError):
            self.livechat.write({'woow_ai_pause_minutes': 1441})
        self.contact.active = False
        with self.assertRaises(ValidationError):
            self.livechat.write({'woow_ai_enabled': True})

    def test_migration_fields_are_written_by_the_migration_only(self):
        with self.assertRaises(UserError):
            self.livechat.write({'woow_ai_migration_state': 'switched'})
        with self.assertRaises(UserError):
            self.env['im_livechat.channel'].create({'name': 'x', 'woow_ai_legacy_enabled': True})
        with self.assertRaises(AccessError):
            self.livechat.with_user(self.manager_user).action_woow_ai_legacy_finish()
        with self.assertRaises(AccessError):
            self.livechat.with_user(self.manager_user).action_woow_ai_legacy_switch_wizard()

    def test_never_installed_im_livechat_ai_prepares_nothing(self):
        if self.env['woow.ai.livechat.migration']._legacy_installed():
            self.skipTest('im_livechat_ai is installed in this database')
        before = self.env['woow.ai.contact'].search_count([])
        self.assertEqual(self.env['woow.ai.livechat.migration']._prepare(), {})
        self.assertEqual(self.env['woow.ai.contact'].search_count([]), before)
        self.assertFalse(self.livechat.woow_ai_legacy_available)
        with self.assertRaises(UserError):
            self.livechat.action_woow_ai_legacy_prepare()

    def test_coexistence_guard_with_a_legacy_column(self):
        """Simulated im_livechat_ai column (rolled back with the test): a channel it
        still answers is never answered by the AI contact too."""
        if self.env['woow.ai.livechat.migration']._legacy_installed():
            self.skipTest('covered by test_migration with the real module')
        session = self.new_session(visitor=self.visitor)
        self.env.flush_all()
        self.env.cr.execute("ALTER TABLE im_livechat_channel ADD COLUMN ai_enabled BOOLEAN")
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_enabled = TRUE WHERE id = %s", (self.livechat.id,))
        self.assertFalse(session._woow_ai_channel_contact())
        self.post(session, self.visitor)
        self.assertFalse(self.jobs(session))
        with self.assertRaises(ValidationError):
            self.livechat.write({'woow_ai_contact_id': self.contact.id})
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_enabled = FALSE WHERE id = %s", (self.livechat.id,))
        self.assertEqual(session._woow_ai_channel_contact(), self.contact)
