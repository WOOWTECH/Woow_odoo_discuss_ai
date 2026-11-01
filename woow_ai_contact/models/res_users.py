# -*- coding: utf-8 -*-
"""Onboarding like OdooBot: the chats with the AI contacts a user may use are
created (pinned, with an unread welcome message) when the user's messaging is
initialized, once per (user, AI contact)."""

import logging

import psycopg2

from odoo import models

_logger = logging.getLogger(__name__)


class ResUsers(models.Model):
    _inherit = 'res.users'

    def _init_messaging(self, store):
        # before super: the chats created here are counted in the unread
        # counter (initChannelsUnreadCounter) of this same answer
        self._woow_ai_onboard()
        return super()._init_messaging(store)

    def _woow_ai_onboard(self):
        """Onboard this user to the AI contacts they may use; never raises
        (except a write on a read-only cursor, which Odoo retries read/write).

        ``/mail/data`` is a read-only route: when nothing is pending this runs
        one read query; the first write on a read-only replica raises
        ReadOnlySqlTransaction and the request is replayed on the primary.
        """
        self.ensure_one()
        Contact = self.env['woow.ai.contact'].sudo()
        if self.env.cr.readonly:
            if Contact._contacts_to_onboard(self):
                # no failed write ("bad query" error log): ask for the read/write replay at once
                raise psycopg2.errors.ReadOnlySqlTransaction(
                    'woow_ai_contact: creating the AI contact chats of user %s' % self.id)
            return self.env['discuss.channel'].sudo()
        try:
            with self.env.cr.savepoint():
                return Contact._onboard_user(self)
        except psycopg2.errors.ReadOnlySqlTransaction:
            raise
        except psycopg2.errors.SerializationFailure:
            raise  # concurrent onboarding: Odoo retries the request
        except Exception:  # noqa: BLE001 - onboarding must never break Discuss
            _logger.exception("AI contacts: could not onboard user %s", self.id)
            return self.env['discuss.channel'].sudo()
