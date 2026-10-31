# -*- coding: utf-8 -*-
"""Trigger AI replies from Discuss, and build the conversation history.

Core module: direct chats (``channel_type == 'chat'``) with an AI contact.
Other channel kinds plug in by overriding the ``_woow_ai_*`` hooks (the Live
Chat bridge overrides ``_woow_ai_channel_contact``, ``_woow_ai_author_role``,
``_woow_ai_message_kind``, ``_woow_ai_is_paused``, ``_woow_ai_requester``,
``_woow_ai_channel_info`` and ``_woow_ai_check_author``).
"""

import html as html_lib
import logging
import re

from odoo import models

_logger = logging.getLogger(__name__)

_BREAK_RE = re.compile(r'<\s*(br|/p|/div|/li|/tr|/h\d)\s*/?\s*>', re.IGNORECASE)
_CELL_RE = re.compile(r'<\s*/t[dh]\s*>', re.IGNORECASE)
_TAG_RE = re.compile(r'<[^>]+>')

# Author checks (_woow_ai_check_author)
AUTHOR_OK = 'ok'
AUTHOR_DENIED = 'denied'
AUTHOR_IGNORE = 'ignore'


def html_to_text(body):
    """Message HTML -> plain text, keeping line breaks."""
    text = _BREAK_RE.sub('\n', str(body or ''))
    text = _CELL_RE.sub(' | ', text)
    text = html_lib.unescape(_TAG_RE.sub('', text))
    text = re.sub(r'[ \t]+\n', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    # --- Hooks (override in bridges) ---

    def _woow_ai_channel_contact(self):
        """The AI contact answering in this channel, or an empty recordset.

        Core: a direct chat whose other member is an AI contact's bot partner.
        """
        self.ensure_one()
        Contact = self.env['woow.ai.contact'].sudo()
        if self.channel_type != 'chat':
            return Contact
        partners = self.sudo().channel_member_ids.partner_id
        if not partners:
            return Contact
        return Contact.search([('partner_id', 'in', partners.ids)], limit=1)

    def _woow_ai_check_author(self, contact, message):
        """AUTHOR_OK (answer), AUTHOR_DENIED (refuse, no backend call) or AUTHOR_IGNORE."""
        self.ensure_one()
        author = message.author_id
        if not author or author == contact.partner_id:
            return AUTHOR_IGNORE
        users = author.sudo().with_context(active_test=False).user_ids
        if not users:
            return AUTHOR_IGNORE
        if any(contact._user_allowed(user) for user in users):
            return AUTHOR_OK
        return AUTHOR_DENIED

    def _woow_ai_message_kind(self, contact, message):
        """Job kind for an allowed message ('reply'; bridges may return 'handoff' or None)."""
        return 'reply'

    def _woow_ai_is_paused(self):
        """True while the AI must not answer (e.g. a human took over). Core: never."""
        return False

    def _woow_ai_author_role(self, contact, message):
        """History role of a message: ('user'|'assistant', text prefix) or None to drop it."""
        if message.author_id and message.author_id == contact.partner_id:
            return 'assistant', ''
        return 'user', ''

    def _woow_ai_requester(self, contact):
        """Partner the AI is talking to (identity sent to the backend)."""
        self.ensure_one()
        members = self.sudo().channel_member_ids.filtered(lambda m: m.partner_id and m.partner_id != contact.partner_id)
        return members.sorted('id')[:1].partner_id

    def _woow_ai_channel_info(self, contact):
        """``channel`` object of the n8n payload."""
        self.ensure_one()
        return {'id': self.id, 'type': self.channel_type}

    # --- Trigger ---

    def _notify_thread(self, message, msg_vals=False, **kwargs):
        rdata = super()._notify_thread(message, msg_vals=msg_vals, **kwargs)
        if len(self) == 1:
            try:
                # Savepoint: a failure here must never undo the user's message.
                with self.env.cr.savepoint():
                    self.sudo()._woow_ai_handle_new_message(message.sudo())
            except Exception:  # noqa: BLE001
                _logger.exception("AI contact: could not handle message %s in channel %s", message.id, self.id)
        return rdata

    def _woow_ai_handle_new_message(self, message):
        """Queue an AI reply (or refuse) for a newly posted message."""
        self.ensure_one()
        if message.message_type != 'comment' or message.model != 'discuss.channel':
            return False
        contact = self._woow_ai_channel_contact()
        if not contact or not contact.active:
            return False
        if not html_to_text(message.body) and not message.attachment_ids:
            return False
        check = self._woow_ai_check_author(contact, message)
        if check == AUTHOR_IGNORE:
            return False
        if check == AUTHOR_DENIED:
            contact._post_text(self, self._woow_ai_refusal_text(message))
            return False
        kind = self._woow_ai_message_kind(contact, message)
        if not kind or self._woow_ai_is_paused():
            return False
        return self.env['woow.ai.job']._create_for_message(self, contact, message, kind=kind)

    def _woow_ai_refusal_text(self, message):
        lang = message.author_id.sudo().lang or self.env.lang
        return self.with_context(lang=lang).env._('You do not have permission to use this AI contact.')

    # --- History ---

    def _woow_ai_context_messages(self, contact):
        """The most recent ``max_history`` comment messages, oldest first."""
        self.ensure_one()
        limit = max(1, min(contact.sudo().max_history or 50, 200))
        messages = self.env['mail.message'].sudo().search([
            ('model', '=', 'discuss.channel'),
            ('res_id', '=', self.id),
            ('message_type', 'in', ('comment', 'email')),
        ], order='id desc', limit=limit)
        return messages.sorted('id')

    def _woow_ai_history(self, contact, messages):
        """Messages -> [{'role', 'text', 'attachments', 'message_id'}] (no empty entries)."""
        self.ensure_one()
        history = []
        for message in messages:
            role = self._woow_ai_author_role(contact, message)
            if not role:
                continue
            role, prefix = role
            text = html_to_text(message.body)
            attachments = message.sudo().attachment_ids
            if not text and not attachments:
                continue
            history.append({
                'role': role,
                'text': (prefix + text) if text else prefix.strip(),
                'attachments': attachments,
                'message_id': message.id,
            })
        return history
