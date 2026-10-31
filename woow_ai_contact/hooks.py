# -*- coding: utf-8 -*-
"""Install / uninstall hooks."""

import logging

_logger = logging.getLogger(__name__)


def uninstall_hook(env):
    """Bot partners are created at runtime (no xmlid), so the uninstall would
    leave them active: they would stay in "Start a conversation" and in the
    sidebar, and nothing would answer any more. They are archived, not
    deleted (they author the old AI messages), and the AI chats are unpinned
    and closed for portal uploads. The cron runner report (a system
    parameter written with SQL, no xmlid) is removed."""
    env.cr.execute("DELETE FROM ir_config_parameter WHERE key = %s", ('woow_ai_contact.dispatcher_state',))
    contacts = env['woow.ai.contact'].sudo().with_context(active_test=False).search([])
    partners = contacts.mapped('partner_id').with_context(active_test=False)
    if not partners:
        return
    partners.filtered('active').write({'active': False})
    Member = env['discuss.channel.member'].sudo()
    chats = Member.search([('partner_id', 'in', partners.ids),
                           ('channel_id.channel_type', '=', 'chat')]).mapped('channel_id')
    if chats:
        others = Member.search([('channel_id', 'in', chats.ids), ('partner_id', 'not in', partners.ids),
                                ('unpin_dt', '=', False)])
        others.write({'unpin_dt': env.cr.now()})
        chats.filtered('allow_public_upload').write({'allow_public_upload': False})
    _logger.info("woow_ai_contact uninstalled: %s bot partner(s) archived, %s AI chat(s) unpinned",
                 len(partners), len(chats))
