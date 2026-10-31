# -*- coding: utf-8 -*-
"""Install / uninstall hooks."""

import logging

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    # Booleans added to existing rows stay NULL (Odoo fills only non-False
    # defaults): reports with "= false" would miss them (L1 finding).
    env.cr.execute("UPDATE im_livechat_channel SET woow_ai_enabled = FALSE WHERE woow_ai_enabled IS NULL")
    env.cr.execute("UPDATE im_livechat_channel SET woow_ai_legacy_enabled = FALSE WHERE woow_ai_legacy_enabled IS NULL")
    env.cr.execute("UPDATE discuss_channel SET woow_ai_pause_notice_sent = FALSE "
                   "WHERE woow_ai_pause_notice_sent IS NULL AND channel_type = 'livechat'")
    # Stage 1 of the migration from im_livechat_ai 1.1.0: prepare only, never switch.
    try:
        with env.cr.savepoint():
            env['woow.ai.livechat.migration']._prepare()
    except Exception:  # noqa: BLE001 - installing the bridge must not fail because of old data
        _logger.exception("woow_ai_contact_livechat: preparing the im_livechat_ai migration failed; "
                          "use 'Prepare again' on a Live Chat channel after fixing the cause")


def uninstall_hook(env):
    # The shared bot of im_livechat_ai (handed to this module by "Finish
    # migration") must survive: old AI messages are authored by it, and a
    # deleted author would make them look like visitor messages. Records
    # under "__export__" belong to no module and are never removed.
    env['woow.ai.livechat.migration']._release_legacy_xmlids()
