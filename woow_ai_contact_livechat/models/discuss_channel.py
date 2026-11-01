# -*- coding: utf-8 -*-
"""Live Chat conversations answered by an AI contact (core ``_woow_ai_*`` hooks).

Reproduces the human handoff of im_livechat_ai 1.1.0 (package D) and fixes
what the L1 field test measured:

* an operator's comment pauses the AI for the conversation (only extended,
  never shortened; reread from the database so a running job sees it);
* a visitor phrase asking for a human queues a handoff job (fixed notice,
  operator alert, no backend call) and pauses the AI;
* a visitor who writes while the AI is paused and no operator wrote in the
  last PAUSE_NOTICE_QUIET_MINUTES gets one notice per pause and the
  operators are alerted again (1.1.0 stayed silent for up to 30 minutes);
* operators resume (or pause) the AI at once: ``/ai on`` / ``/ai off`` in the
  conversation, or the button of the session form (1.1.0 had no way);
* the handoff and paused notices, pauses and resumes are conversation events
  (``woow.ai.livechat.event``): the notices are left out of the history and
  the model gets the events as facts in the system message, so it never
  takes "已幫您通知客服" for its own words and denies it (real-LLM acceptance);
  operator messages are sent as labelled user turns, never as the AI's.

Visitors: guests, authors without partner, portal / public users and
partners without user; an employee who is the conversation's visitor member
(chatting on the website) too. Operators: internal users. Bots: every AI
contact partner, the im_livechat_ai bots, OdooBot and a session operator
without internal user (an Odoo chatbot).
"""

import logging
import re
from datetime import timedelta

import pytz
from markupsafe import Markup

from odoo import SUPERUSER_ID, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.service.model import PG_CONCURRENCY_EXCEPTIONS_TO_RETRY

from odoo.addons.woow_ai_contact.models.discuss_channel import (
    AUTHOR_IGNORE, AUTHOR_OK, html_to_text,
)

_logger = logging.getLogger(__name__)

AUTHOR_VISITOR = 'visitor'
AUTHOR_OPERATOR = 'operator'
AUTHOR_BOT = 'bot'

# History prefix of an operator's message (a user turn), and the note added to the system prompt.
OPERATOR_PREFIX = '[真人客服 %s] '
OPERATOR_SYSTEM_NOTE = (
    '對話中以「[真人客服 姓名]」開頭的訊息是真人客服同事對訪客說的話：不是訪客說的，也不是你（AI）說的。'
    '不要推翻、否認或更改同事的說法與承諾，必要時請訪客等候同事處理。'
)
# Owner decision 40: the backend decides when to hand the visitor to a human.
# openai backends (LiteLLM, Hermes) end their reply with this marker; n8n answers
# "handoff": true or uses the marker in "output" (Chat Trigger workflows).
HANDOFF_MARKER = '[[handoff]]'
# case insensitive, spaces inside allowed, also escaped (\[\[handoff\]\]) or in backticks
HANDOFF_MARKER_RE = re.compile(r'`?\\?\[\\?\[\s*handoff\s*\\?\]\\?\]`?', re.IGNORECASE)
# The only prompt text Odoo adds for the handoff (openai backends, Live Chat channels with operators).
HANDOFF_SYSTEM_LINE = (
    '訪客要求真人客服、想跟真人說話時，在回覆最後單獨一行寫 [[handoff]]，系統就會轉接真人客服（不要說無法轉接；'
    '只問電話或聯絡方式不算）。When the visitor asks for a human, end your reply with a line [[handoff]] '
    'and Odoo hands the conversation to a human operator.'
)
EVENT_QUOTE_CHARS = 120  # characters of a quoted message in an event
EVENT_MAX = 20  # most recent events given to the model
# No paused notice while an operator wrote less than this many minutes ago (they are chatting).
PAUSE_NOTICE_QUIET_MINUTES = 5

LEGACY_BOT_XMLIDS = (
    'woow_ai_contact_livechat.legacy_partner_ai_bot',
    'im_livechat_ai.partner_ai_bot',
    '__export__.woow_ai_contact_livechat_legacy_partner_ai_bot',
)
LEGACY_CATEGORY_XMLIDS = (
    'woow_ai_contact_livechat.legacy_partner_category_ai_bot',
    'im_livechat_ai.partner_category_ai_bot',
    '__export__.woow_ai_contact_livechat_legacy_partner_category_ai_bot',
)

def strip_handoff_marker(text):
    """(text without the [[handoff]] markers, whether one was found)."""
    if not text or not HANDOFF_MARKER_RE.search(text):
        return text, False
    text = HANDOFF_MARKER_RE.sub('', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip(), True


def format_time(value, tz=None):
    """UTC datetime -> 'YYYY-MM-DD HH:MM' in `tz` (with the zone name), UTC by default."""
    if not value:
        return ''
    zone = 'UTC'
    if tz:
        try:
            zone = pytz.timezone(tz).zone
        except pytz.UnknownTimeZoneError:
            zone = 'UTC'
    local = pytz.utc.localize(value).astimezone(pytz.timezone(zone))
    return '%s (%s)' % (local.strftime('%Y-%m-%d %H:%M'), zone)


_RESUME_WORDS = {'on', 'resume', 'start', '恢復', '開'}
_PAUSE_WORDS = {'off', 'pause', 'stop', '暫停', '關'}


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    woow_ai_paused_until = fields.Datetime(
        string='AI Contact Paused Until', readonly=True, copy=False,
        help='The AI contact does not answer this Live Chat conversation before this time: an operator '
             'wrote, or the visitor asked for a human. "/ai on" resumes it at once.')
    woow_ai_pause_notice_sent = fields.Boolean(
        string='Paused Notice Sent', readonly=True, copy=False,
        help='The visitor already got the paused notice for the current pause.')
    woow_ai_livechat_enabled = fields.Boolean(
        string='AI Contact Answers', compute='_compute_woow_ai_livechat_enabled')

    def _compute_woow_ai_livechat_enabled(self):
        for channel in self:
            channel.woow_ai_livechat_enabled = bool(
                channel.channel_type == 'livechat' and channel.sudo()._woow_ai_channel_contact())

    def _woow_ai_is_livechat(self):
        self.ensure_one()
        return self.channel_type == 'livechat' and bool(self.sudo().livechat_channel_id)

    # --- Core hooks ---

    def _woow_ai_channel_contact(self):
        if self.channel_type != 'livechat':
            return super()._woow_ai_channel_contact()
        self.ensure_one()
        livechat_channel = self.sudo().livechat_channel_id
        if not livechat_channel:
            return self.env['woow.ai.contact'].sudo()
        # empty while im_livechat_ai still answers this channel (coexistence guard)
        return livechat_channel._woow_ai_answering_contact()

    def _woow_ai_check_author(self, contact, message):
        if not self._woow_ai_is_livechat():
            return super()._woow_ai_check_author(contact, message)
        kind = self._woow_ai_author_kind(message)
        if kind == AUTHOR_OPERATOR:
            # the core only hands comments to this hook
            self._woow_ai_pause()
            return AUTHOR_IGNORE
        if kind == AUTHOR_BOT:
            return AUTHOR_IGNORE
        # visitors are never refused: no allowed_group_ids check (guests have no user)
        return AUTHOR_OK

    def _woow_ai_message_kind(self, contact, message):
        if not self._woow_ai_is_livechat():
            return super()._woow_ai_message_kind(contact, message)
        if self.sudo().livechat_channel_id._woow_ai_is_handoff_request(html_to_text(message.body)):
            return 'handoff'
        return 'reply'

    def _woow_ai_is_paused(self):
        if self.channel_type != 'livechat':
            return super()._woow_ai_is_paused()
        self.ensure_one()
        # reread: a running job must see an operator's post committed meanwhile
        self.invalidate_recordset(['woow_ai_paused_until'])
        paused_until = self.sudo().woow_ai_paused_until
        return bool(paused_until and paused_until > fields.Datetime.now())

    def _woow_ai_handle_new_message(self, message):
        job = super()._woow_ai_handle_new_message(message)
        if self.channel_type != 'livechat':
            return job
        if job and job.kind == 'handoff':
            # after super(): pausing in the kind hook would stop the core from creating the job
            self._woow_ai_pause()
            # the handoff message tells the visitor; no paused notice on top of it
            self.sudo().woow_ai_pause_notice_sent = True
        elif not job:
            self._woow_ai_maybe_paused_notice(message)
        return job

    def _woow_ai_author_role(self, contact, message):
        if not self._woow_ai_is_livechat():
            return super()._woow_ai_author_role(contact, message)
        kind = self._woow_ai_author_kind(message)
        if kind == AUTHOR_VISITOR:
            return 'user', ''
        if kind == AUTHOR_OPERATOR:
            # a person, not the AI: a user turn with the operator's label (OPERATOR_SYSTEM_NOTE)
            return 'user', OPERATOR_PREFIX % self._woow_ai_operator_name(message.author_id)
        if message.id in self._woow_ai_notice_message_ids():
            return None  # a handoff / paused notice: a conversation event, not the AI's words
        return 'assistant', ''

    def _woow_ai_history(self, contact, messages):
        if not self._woow_ai_is_livechat():
            return super()._woow_ai_history(contact, messages)
        # compute the visitor member and the bots once for all messages
        cached = self.with_context(woow_ai_lc_cache={
            'bots': self._woow_ai_bot_partner_ids(),
            'visitor': self._woow_ai_visitor_member().partner_id.id,
            'notices': self._woow_ai_notice_message_ids(),
        })
        history = super(DiscussChannel, cached)._woow_ai_history(contact, messages)
        operator_ids = {
            message.id for message in messages
            if cached._woow_ai_author_kind(message) == AUTHOR_OPERATOR
        }
        for entry in history:
            entry['woow_ai_operator'] = entry.get('message_id') in operator_ids
        return history

    def _woow_ai_system_prompt(self, contact, history):
        prompt = super()._woow_ai_system_prompt(contact, history)
        if self.channel_type != 'livechat' or not any(e.get('woow_ai_operator') for e in history or []):
            return prompt
        return '\n\n'.join(part for part in (prompt, OPERATOR_SYSTEM_NOTE) if part)

    def _woow_ai_review_answer(self, contact, result):
        """Live Chat: the backend decides the handoff (owner decision 40). The
        n8n ``handoff`` flag or a ``[[handoff]]`` marker in any reply text
        (openai and n8n ``output``) hands the visitor to a human; markers are
        always removed from what the visitor sees, replies left empty dropped."""
        if not self._woow_ai_is_livechat():
            return super()._woow_ai_review_answer(contact, result)
        handoff = bool(result.get('handoff'))
        replies = []
        for reply in result.get('replies') or []:
            text, found = strip_handoff_marker(reply.get('text') or '')
            handoff = handoff or found
            if text or reply.get('attachments'):
                replies.append(dict(reply, text=text))
        result['replies'] = replies
        return handoff

    def _woow_ai_handoff_available(self):
        """True when the visitor can be handed to a human: the Live Chat
        channel has operators (the system line and n8n ``handoff_available``)."""
        self.ensure_one()
        livechat_channel = self.sudo().livechat_channel_id
        return bool(self._woow_ai_is_livechat() and livechat_channel._woow_ai_has_operators())

    def _woow_ai_conversation_events(self, contact, history):
        """Handoff, paused notice, pause and resume events since the oldest
        message of the context (at most EVENT_MAX, oldest first)."""
        if not self._woow_ai_is_livechat():
            return super()._woow_ai_conversation_events(contact, history)
        self.ensure_one()
        domain = [('channel_id', '=', self.id)]
        message_ids = [e['message_id'] for e in history or [] if e.get('message_id')]
        if message_ids:
            oldest = self.env['mail.message'].sudo().browse(min(message_ids))
            domain.append(('create_date', '>=', oldest.create_date))
        events = self.env['woow.ai.livechat.event'].sudo().search(domain, order='id desc', limit=EVENT_MAX)
        return [{'date': event.create_date, 'kind': event.kind, 'text': self._woow_ai_event_text(event)}
                for event in events.sorted('id')]

    def _woow_ai_event_text(self, event):
        """One sentence for the model (Traditional Chinese, like OPERATOR_SYSTEM_NOTE)."""

        def quote(message, default=''):
            text = html_to_text(message.body) if message else ''
            text = ' '.join((text or default).split())
            return text[:EVENT_QUOTE_CHARS] + ('…' if len(text) > EVENT_QUOTE_CHARS else '')

        operator = self._woow_ai_operator_name(event.user_id.partner_id) if event.user_id else ''
        operator = ('客服人員「%s」' % operator) if operator else '客服人員'
        if event.kind == 'handoff' and event.source == 'backend':
            asked = quote(event.trigger_message_id)
            asked = '（訪客說：「%s」）' % asked if asked else ''
            minutes = ' %s 分鐘' % event.minutes if event.minutes else ''
            if event.message_id:
                done = '系統（不是你）已發出轉接通知「%s」' % quote(event.message_id, '已通知客服')
            else:
                done = '你（AI）在回覆中請求轉接'
            return ('AI 判斷訪客要找真人客服%s，%s，並已通知真人客服人員（客服已收到通知）；AI 暫停回覆%s。'
                    '這是事實：不要否認或更正這次轉接。') % (asked, done, minutes)
        if event.kind == 'handoff':
            asked = quote(event.trigger_message_id)
            return ('訪客要求真人客服%s。系統（不是你）已發出轉接通知「%s」，並已通知真人客服人員（客服已收到通知）；'
                    'AI 暫停回覆%s。這是事實：不要否認、更正或為這則通知道歉。') % (
                '（訪客說：「%s」）' % asked if asked else '', quote(event.message_id, '已通知客服'),
                ' %s 分鐘' % event.minutes if event.minutes else '')
        if event.kind == 'paused_notice':
            return 'AI 暫停期間訪客又傳訊息，系統（不是你）已發出等候提示「%s」，並再次通知真人客服人員。' % quote(
                event.message_id, '請稍候')
        if event.kind == 'pause':
            return '%s暫停了 AI 回覆%s。' % (operator, ' %s 分鐘' % event.minutes if event.minutes else '')
        return ('%s恢復了 AI 回覆：從這之後由你（AI）接手回答訪客；暫停期間沒有人回覆的訪客問題，請直接回答。'
                '先前的轉接與通知都是真的。') % operator

    def _woow_ai_notice_message_ids(self):
        """Ids of the notices the AI posted for an event in this conversation."""
        cache = self.env.context.get('woow_ai_lc_cache')
        if cache and 'notices' in cache:
            return cache['notices']
        self.ensure_one()
        events = self.env['woow.ai.livechat.event'].sudo().search(
            [('channel_id', '=', self.id), ('message_id', '!=', False)])
        return set(events.message_id.ids)

    def _woow_ai_log_event(self, kind, message=None, trigger=None, minutes=0, source=False, reason=False):
        """Record a conversation event (never fails the caller)."""
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                return self.env['woow.ai.livechat.event'].sudo().create({
                    'channel_id': self.id,
                    'kind': kind,
                    'message_id': message.id if message else False,
                    'trigger_message_id': trigger.id if trigger else False,
                    'user_id': self.env.uid if kind in ('pause', 'resume') and not self.env.user._is_superuser()
                    else False,
                    'minutes': minutes or 0,
                    'source': source or False,
                    'reason': reason or False,
                })
        except PG_CONCURRENCY_EXCEPTIONS_TO_RETRY:
            raise  # aborted transaction (concurrent update): Odoo retries the request
        except Exception:  # noqa: BLE001
            _logger.exception("AI Live Chat %s: could not record the %s event", self.id, kind)
            return self.env['woow.ai.livechat.event']

    def _woow_ai_requester(self, contact):
        if not self._woow_ai_is_livechat():
            return super()._woow_ai_requester(contact)
        # the core default (first non-bot member) would be the operator here
        return self._woow_ai_visitor_member().partner_id

    def _woow_ai_identity(self, contact):
        """Identity of the visitor (docs/PROTOCOL.md "identity", Live Chat
        rows): never the IP address, visited pages, website.visitor tracking,
        phone or address."""
        if not self._woow_ai_is_livechat():
            return super()._woow_ai_identity(contact)
        self.ensure_one()
        channel = self.sudo()
        member = channel._woow_ai_visitor_member()
        partner = member.partner_id
        guest = member.guest_id
        fallback_lang = self._woow_ai_installed_lang(channel._woow_ai_session_lang(),
                                                     channel.livechat_operator_id.lang)
        if partner:
            users = partner.with_context(active_test=False).user_ids
            if users and all(user._is_public() for user in users):
                # the public user's partner is shared by the whole site: no identity
                identity = self._woow_ai_anonymous_identity(fallback_lang)
                identity['visitor_key'] = 'public'
                return identity
            identity = contact._identity(partner)
            identity.update({'is_guest': False, 'guest_id': None, 'visitor_key': 'partner:%s' % partner.id,
                             'lang': self._woow_ai_installed_lang(identity.get('lang'), fallback_lang)})
            return identity
        identity = self._woow_ai_anonymous_identity(fallback_lang)
        if guest:
            identity.update({
                'guest_id': guest.id,
                'name': guest.name or channel.anonymous_name or '',
                'lang': self._woow_ai_installed_lang(guest.lang, fallback_lang),
                'tz': guest.timezone or '',
                'visitor_key': 'guest:%s' % guest.id,
            })
        else:
            identity['name'] = channel.anonymous_name or ''
        return identity

    def _woow_ai_installed_lang(self, *codes):
        """First of `codes` that is an installed language, else ''. The identity
        language also translates the default messages (error, notes): an
        uninstalled code there raises "Invalid language code" (a guest may
        carry any code)."""
        installed = {code for code, _name in self.env['res.lang'].get_installed()}
        return next((code for code in codes if code and code in installed), '')

    def _woow_ai_link_hosts(self, contact):
        if not self._woow_ai_is_livechat():
            return super()._woow_ai_link_hosts(contact)
        return self.env['im_livechat.channel']._woow_ai_site_hosts()

    def _woow_ai_channel_info(self, contact):
        info = super()._woow_ai_channel_info(contact)
        if not self._woow_ai_is_livechat():
            return info
        channel = self.sudo()
        livechat_channel = channel.livechat_channel_id
        operator = channel.livechat_operator_id
        info['livechat'] = {
            'livechat_channel_id': livechat_channel.id,
            'livechat_channel_name': livechat_channel.name or '',
            'operator_name': self._woow_ai_operator_name(operator) if operator else '',
            'country_code': (channel.country_id.code or '') if channel.country_id else '',
            'visitor_kind': channel._woow_ai_visitor_kind(),
            # the backend may answer "handoff": true or end "output" with [[handoff]]
            'handoff_available': self._woow_ai_handoff_available(),
        }
        return info

    # --- Who wrote a message ---

    def _woow_ai_bot_partner_ids(self):
        """Partner ids that are bots: every AI contact, the im_livechat_ai bots
        (this channel's previous bot, the shared default bot, partners tagged
        "AI 機器人", still installed channel bots) and OdooBot."""
        cache = self.env.context.get('woow_ai_lc_cache')
        if cache:
            return cache['bots']
        self.ensure_one()
        ids = set(self.env['woow.ai.contact'].sudo().with_context(active_test=False).search([]).partner_id.ids)
        livechat_channel = self.sudo().livechat_channel_id
        if livechat_channel.woow_ai_legacy_bot_partner_id:
            ids.add(livechat_channel.woow_ai_legacy_bot_partner_id.id)
        for xmlid in LEGACY_BOT_XMLIDS + ('base.partner_root',):
            record = self.env.ref(xmlid, raise_if_not_found=False)
            if record:
                ids.add(record.id)
        categories = [self.env.ref(x, raise_if_not_found=False) for x in LEGACY_CATEGORY_XMLIDS]
        category_ids = [c.id for c in categories if c]
        if category_ids:
            ids.update(self.env['res.partner'].sudo().with_context(active_test=False).search(
                [('category_id', 'in', category_ids)]).ids)
        ids.update(self.env['woow.ai.livechat.migration']._legacy_channel_bot_ids())
        return ids

    def _woow_ai_visitor_member(self):
        """The conversation's visitor member (partner or guest), or an empty recordset.

        Odoo creates the operator's member and the visitor's in the same
        transaction as the conversation; operators invited later are created
        later. So the visitor is the first member created with the
        conversation that is neither the session operator nor a bot (1.1.0).
        """
        self.ensure_one()
        channel = self.sudo()
        operator = channel.livechat_operator_id
        bot_ids = self._woow_ai_bot_partner_ids()
        for member in channel.channel_member_ids.sorted('id'):
            if member.create_date and channel.create_date and member.create_date > channel.create_date:
                break  # joined later: an invited operator, never the visitor
            partner = member.partner_id
            if partner and (partner == operator or partner.id in bot_ids):
                continue
            return member
        return self.env['discuss.channel.member']

    def _woow_ai_author_kind(self, message):
        """visitor / operator / bot (bots first: never answer a bot)."""
        self.ensure_one()
        author = message.author_id
        if author and author.id in self._woow_ai_bot_partner_ids():
            return AUTHOR_BOT
        if message.author_guest_id or not author:
            return AUTHOR_VISITOR
        channel = self.sudo()
        users = author.sudo().with_context(active_test=False).user_ids
        if any(not user.share for user in users):
            if author == channel.livechat_operator_id:
                return AUTHOR_OPERATOR
            cache = self.env.context.get('woow_ai_lc_cache')
            visitor_id = cache['visitor'] if cache else self._woow_ai_visitor_member().partner_id.id
            if visitor_id and author.id == visitor_id:
                return AUTHOR_VISITOR  # an employee chatting on the website
            return AUTHOR_OPERATOR
        if author == channel.livechat_operator_id:
            return AUTHOR_BOT  # a session operator without internal user: an Odoo chatbot
        return AUTHOR_VISITOR  # portal user, public user, partner without user

    def _woow_ai_visitor_kind(self):
        self.ensure_one()
        member = self._woow_ai_visitor_member()
        partner = member.partner_id
        if not partner:
            return 'guest'
        users = partner.with_context(active_test=False).user_ids
        if not users:
            return 'partner'
        if all(user._is_public() for user in users):
            return 'public'
        if any(not user.share for user in users):
            return 'employee'
        return 'portal'

    @staticmethod
    def _woow_ai_operator_name(author):
        """Name visitors see for an operator: the Live Chat username when set."""
        author = author.sudo()
        user = author.with_context(active_test=False).user_ids.filtered(lambda u: not u.share)[:1]
        return (user.livechat_username if user else '') or author.name or ''

    def _woow_ai_session_lang(self):
        """Language of the website conversation (website_livechat), else the current
        user's, else the session operator's."""
        self.ensure_one()
        channel = self.sudo()
        if 'livechat_visitor_id' in channel._fields and channel.livechat_visitor_id.lang_id:
            return channel.livechat_visitor_id.lang_id.code
        # job threads have no lang: the operator's language is the site's language
        return self.env.lang or channel.livechat_operator_id.lang or ''

    @staticmethod
    def _woow_ai_anonymous_identity(lang):
        return {
            'user_id': None, 'partner_id': None, 'name': '', 'email': '', 'lang': lang or '', 'tz': '',
            'is_portal': False, 'company_id': None, 'is_guest': True, 'guest_id': None, 'visitor_key': '',
        }

    # --- Pause and resume ---

    def _woow_ai_pause(self, minutes=None):
        """Pause the AI for this conversation (extends, never shortens, a pause)."""
        self.ensure_one()
        channel = self.sudo()
        if minutes is None:
            minutes = channel.livechat_channel_id._woow_ai_pause_minutes_value()
        now = fields.Datetime.now()
        until = now + timedelta(minutes=minutes)
        current = channel.woow_ai_paused_until
        if not current or current < until:
            vals = {'woow_ai_paused_until': until}
            if not current or current <= now:
                vals['woow_ai_pause_notice_sent'] = False  # a new pause starts
            channel.write(vals)
        return until

    def _woow_ai_resume(self):
        """Resume the AI now; answer the visitor's last message if no person answered it."""
        self.ensure_one()
        channel = self.sudo()
        was_paused = channel._woow_ai_is_paused()
        channel.write({'woow_ai_paused_until': False, 'woow_ai_pause_notice_sent': False})
        if was_paused:
            self._woow_ai_log_event('resume')
        contact = channel._woow_ai_channel_contact()
        if not contact:
            return False
        recent = self.env['mail.message'].sudo().search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', channel.id), ('message_type', '=', 'comment'),
        ], order='id desc', limit=20)
        # the newest message of a person (bot notices skipped): the visitor's means nobody answered it
        last = next((m for m in recent if channel._woow_ai_author_kind(m) != AUTHOR_BOT), None)
        if last and channel._woow_ai_author_kind(last) == AUTHOR_VISITOR:
            return self.env['woow.ai.job']._create_for_message(channel, contact, last, kind='reply')
        return False

    def _woow_ai_operator_pause(self):
        """Pause asked by an operator (button, ``/ai off``): recorded as an event."""
        self.ensure_one()
        until = self._woow_ai_pause()
        minutes = self.sudo().livechat_channel_id._woow_ai_pause_minutes_value()
        self._woow_ai_log_event('pause', minutes=minutes)
        return until

    def _woow_ai_maybe_paused_notice(self, message):
        """Visitor message blocked by the pause: one notice per pause, unless an
        operator is chatting right now; the operators are alerted again."""
        self.ensure_one()
        channel = self.sudo()
        if (message.message_type != 'comment' or channel.woow_ai_pause_notice_sent
                or not channel._woow_ai_is_paused()):
            return
        contact = channel._woow_ai_channel_contact()
        livechat_channel = channel.livechat_channel_id
        text = (livechat_channel.woow_ai_paused_message or '').strip() if livechat_channel else ''
        if not contact or not text or channel._woow_ai_author_kind(message) != AUTHOR_VISITOR:
            return
        if not html_to_text(message.body) and not message.attachment_ids:
            return
        if channel._woow_ai_operator_wrote_recently():
            return
        channel.woow_ai_pause_notice_sent = True
        notice = contact._post_text(channel, text)
        channel._woow_ai_log_event('paused_notice', message=notice, trigger=message)
        until = channel.woow_ai_paused_until
        channel._woow_ai_alert_operators('waiting', lambda ch, tz: ch.env._(
            'A visitor is waiting in "%(name)s": the AI is paused until %(time)s. '
            'Type /ai on in the conversation to resume the AI now.',
            name=ch.display_name, time=format_time(until, tz)))

    def _woow_ai_operator_wrote_recently(self):
        self.ensure_one()
        since = fields.Datetime.now() - timedelta(minutes=PAUSE_NOTICE_QUIET_MINUTES)
        recent = self.env['mail.message'].sudo().search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', self.id), ('message_type', '=', 'comment'),
            ('date', '>=', since), ('author_id', '!=', False),
        ], order='id desc', limit=20)
        return any(self._woow_ai_author_kind(m) == AUTHOR_OPERATOR for m in recent)

    def _woow_ai_check_operator(self):
        """Pausing / resuming is for internal users of the conversation or Live Chat users."""
        self.ensure_one()
        user = self.env.user
        if self.env.su:
            return
        if user.share or not (user.has_group('im_livechat.im_livechat_group_user')
                              or user.partner_id in self.sudo().channel_member_ids.partner_id):
            raise AccessError(self.env._('Only Live Chat operators can pause or resume the AI.'))

    def action_woow_ai_resume(self):
        for channel in self:
            channel._woow_ai_check_operator()
            if channel.channel_type != 'livechat':
                raise UserError(self.env._('Only Live Chat conversations have an AI pause.'))
            channel._woow_ai_resume()
        return True

    def action_woow_ai_pause(self):
        for channel in self:
            channel._woow_ai_check_operator()
            if channel.channel_type != 'livechat':
                raise UserError(self.env._('Only Live Chat conversations have an AI pause.'))
            channel._woow_ai_operator_pause()
        return True

    def execute_command_woow_ai(self, body='', **kwargs):
        """``/ai`` (status), ``/ai on`` (resume now), ``/ai off`` (pause) in a Live Chat conversation."""
        self.ensure_one()
        self._woow_ai_check_operator()
        words = (body or '').strip().split()
        argument = words[1].lower() if len(words) > 1 else ''
        channel = self.sudo()
        if channel.channel_type != 'livechat' or not channel._woow_ai_channel_contact():
            msg = self.env._('No AI contact answers this conversation.')
        elif argument in _RESUME_WORDS:
            channel._woow_ai_resume()
            msg = self.env._('The AI answers this conversation again.')
        elif argument in _PAUSE_WORDS:
            until = self._woow_ai_operator_pause()
            msg = self.env._('The AI is paused until %(time)s.', time=format_time(until, self.env.user.tz))
        elif channel._woow_ai_is_paused():
            msg = self.env._('The AI is paused until %(time)s. Type /ai on to resume it now.',
                             time=format_time(channel.woow_ai_paused_until, self.env.user.tz))
        else:
            msg = self.env._('The AI answers this conversation. Type /ai off to pause it.')
        self.env.user._bus_send_transient_message(self, msg)

    # --- Operator alerts ---

    def _woow_ai_alert_recipients(self):
        """Internal users to alert: the session operator and internal members,
        else the channel operators, else the Live Chat managers (never bots)."""
        self.ensure_one()
        channel = self.sudo()
        excluded = self._woow_ai_bot_partner_ids()

        def internal(partners):
            return partners.filtered(lambda p: p.id not in excluded and any(not u.share for u in p.user_ids))

        partners = internal(channel.livechat_operator_id | channel.channel_member_ids.partner_id)
        if not partners:
            partners = internal(channel.livechat_channel_id.user_ids.partner_id)
        if not partners:
            group = self.env.ref('im_livechat.im_livechat_group_manager', raise_if_not_found=False)
            if group:
                partners = internal(group.users.partner_id)
        return partners

    def _woow_ai_alert_operators(self, kind, text_fn):
        """OdooBot direct message (persistent, unread counter, seen offline too)
        and a sticky bus notification (online users), each in the recipient's
        language; never raises.

        Args:
            kind (str): 'handoff' (warning), 'failure' (danger) or 'waiting' (warning)
            text_fn (callable): (channel with the recipient's lang, recipient's tz) -> text
        """
        self.ensure_one()
        partners = self._woow_ai_alert_recipients()
        if not partners:
            _logger.warning("AI Live Chat %s: no operator to alert (%s)", self.id, kind)
            return
        url = '/odoo/discuss?active_id=discuss.channel_%s' % self.id
        odoobot = self.env.ref('base.partner_root')
        # as the superuser (OdooBot's user): channel_get makes the OdooBot <-> partner chat
        Channel = self.env['discuss.channel'].with_user(SUPERUSER_ID).sudo()
        for partner in partners:
            channel = self.with_context(lang=partner.lang or self.env.lang or 'en_US')
            env = channel.env
            titles = {
                'handoff': env._('AI Live Chat: visitor asks for a human'),
                'failure': env._('AI Live Chat: no answer'),
                'waiting': env._('AI Live Chat: visitor waiting'),
            }
            try:
                text = text_fn(channel, partner.tz)
                body = Markup('%s<br/><a href="%s">%s</a>') % (text, url, env._('Open the conversation'))
                with self.env.cr.savepoint():
                    chat = Channel.channel_get([partner.id], pin=False)
                    chat.message_post(body=body, author_id=odoobot.id, message_type='comment',
                                      subtype_xmlid='mail.mt_comment')
                partner._bus_send('simple_notification', {
                    'title': titles.get(kind) or titles['failure'], 'message': text, 'sticky': True,
                    'type': 'danger' if kind == 'failure' else 'warning',
                })
            except Exception:  # noqa: BLE001 - an alert must never undo the job result
                _logger.exception("AI Live Chat %s: could not alert partner %s", self.id, partner.id)
