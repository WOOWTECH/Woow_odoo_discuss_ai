# -*- coding: utf-8 -*-
"""Trigger AI replies from Discuss, and build the conversation history.

Core module: direct chats (``channel_type == 'chat'``) with an AI contact.
Other channel kinds plug in by overriding the ``_woow_ai_*`` hooks (the Live
Chat bridge overrides ``_woow_ai_channel_contact``, ``_woow_ai_author_role``,
``_woow_ai_message_kind``, ``_woow_ai_is_paused``, ``_woow_ai_requester``,
``_woow_ai_identity``, ``_woow_ai_system_prompt``, ``_woow_ai_link_hosts``,
``_woow_ai_channel_info``, ``_woow_ai_check_author`` and
``_woow_ai_conversation_events``).
"""

import html as html_lib
import logging
import re

from odoo import api, fields, models
from odoo.exceptions import AccessError

from odoo.addons.mail.tools.discuss import Store

_logger = logging.getLogger(__name__)

_BREAK_RE = re.compile(r'<\s*(br|/p|/div|/li|/tr|/h\d)\s*/?\s*>', re.IGNORECASE)
_CELL_RE = re.compile(r'<\s*/t[dh]\s*>', re.IGNORECASE)
_TAG_RE = re.compile(r'<[^>]+>')

MAX_MESSAGE_TEXT = 16000  # characters of one history message sent to a backend
TRUNCATED_MARKER = '\n…[truncated]'

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

    def _woow_ai_identity(self, contact):
        """Identity of the person the AI talks to (docs/PROTOCOL.md "identity"):
        the n8n ``user`` object, the openai ``metadata`` and system identity
        block, and the language of the default messages. Bridges return their
        own (e.g. a Live Chat guest without partner: ``is_guest``, name, lang)."""
        self.ensure_one()
        return contact._identity(self._woow_ai_requester(contact))

    def _woow_ai_system_prompt(self, contact, history):
        """System prompt sent to the backend ('' = none: the backend decides)."""
        return (contact.sudo().system_prompt or '').strip()

    def _woow_ai_conversation_events(self, contact, history):
        """Facts about the conversation the model must know but did not say
        itself (docs/PROTOCOL.md "Conversation events"): a bridge returns e.g.
        "handed over to a human, the operators were alerted" or "the AI was
        resumed". Messages that only announce such an event (a notice posted as
        the AI) should then be dropped from the history by
        ``_woow_ai_author_role``, so the model does not take them for its own
        words. Core: none.

        Returns:
            list of dict: ``date`` (UTC datetime), ``kind`` (short code),
            ``text`` (one sentence for the model), oldest first
        """
        return []

    def _woow_ai_link_hosts(self, contact):
        """Hosts whose links in AI replies stay clickable, or None (any http(s)
        link). Bridges facing anonymous visitors return their own site hosts
        (prompt-injection phishing); other links are shown as plain text."""
        return None

    def _woow_ai_channel_info(self, contact):
        """``channel`` object of the n8n payload."""
        self.ensure_one()
        return {'id': self.id, 'type': self.channel_type}

    # --- Direct chats opened from "Start a conversation" ---

    @api.model
    @api.returns('self', lambda channels: Store(channels).get_result())
    def channel_get(self, partners_to, pin=True, force_open=False):
        """A chat with an AI contact's bot partner ("Start a conversation",
        avatar card) goes through the same access check and chat creation as
        the other entries (``woow.ai.contact._get_or_create_chat``: welcome
        message, uploads allowed, onboarded)."""
        if not self.env.context.get('woow_ai_channel_get'):
            own = self.env.user.partner_id.id
            others = [pid for pid in partners_to or [] if pid != own]
            contact = self.env['woow.ai.contact']._contact_for_partners(others)
            if contact and len(set(others)) == 1:
                user = self.env.user
                if not contact.active or not contact._user_allowed(user):
                    raise AccessError(self.env._('You do not have permission to use this AI contact.'))
                chat, _created = contact._get_or_create_chat(user)
                if force_open:
                    member = chat.channel_member_ids.filtered(lambda m: m.partner_id.id == own)
                    member.write({'fold_state': 'open', 'last_interest_dt': fields.Datetime.now()})
                return self.browse(chat.id)
        return super().channel_get(partners_to, pin=pin, force_open=force_open)

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
        """Queue an AI reply (or refuse) for a newly posted message.

        Contract for bridges: returns the created ``woow.ai.job`` or False. The
        job kind (``_woow_ai_message_kind``) is computed BEFORE the pause check
        (``_woow_ai_is_paused``): the kind hook sees every allowed message, even
        while the AI is paused (e.g. to act on a handoff keyword), and a paused
        channel then gets no job.
        """
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
        lang = self.env['woow.ai.contact']._installed_lang(message.author_id.sudo().lang)
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
        # one prefetch set for every attachment of the context: the job commits
        # (and clears the cache) between building the history and the request,
        # which then reads all of them in one query instead of one per message
        # (concrete ids: the x2many's own lazy prefetch set is empty once the cache is cleared)
        prefetch = tuple(messages.sudo().attachment_ids.ids)
        for message in messages:
            role = self._woow_ai_author_role(contact, message)
            if not role:
                continue
            role, prefix = role
            text = html_to_text(message.body)
            if len(text) > MAX_MESSAGE_TEXT:
                text = text[:MAX_MESSAGE_TEXT] + TRUNCATED_MARKER
            attachments = message.sudo().attachment_ids.with_prefetch(prefetch)
            if not text and not attachments:
                continue
            history.append({
                'role': role,
                'text': (prefix + text) if text else prefix.strip(),
                'attachments': attachments,
                'message_id': message.id,
            })
        return history
