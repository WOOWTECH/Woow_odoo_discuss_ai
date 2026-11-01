# -*- coding: utf-8 -*-
"""Live Chat channel: which AI contact answers its visitors, and the handoff settings.

Every field is prefixed ``woow_ai_``: during the migration im_livechat_ai
1.1.0 is installed too and owns ``ai_enabled`` / ``ai_paused_until``; sharing
those columns would make both modules answer.
"""

import re
from urllib.parse import urlsplit

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import sql

MANAGER_GROUP = 'im_livechat.im_livechat_group_manager'

DEFAULT_PAUSE_MINUTES = 30
MAX_PAUSE_MINUTES = 1440

# Owner decision 40: the AI backend decides when to hand a visitor to a human
# (n8n "handoff": true, the "[[handoff]]" marker); keywords are only an optional
# pre-filter of the administrator, empty by default (QA D11/D12: 「可以轉接專人嗎」
# 「我要跟人講話」 missed every keyword list). Channels created before 18.0.1.2.0
# keep the phrases they had.
DEFAULT_HANDOFF_KEYWORDS = ''
# Former default (18.0.1.0.0-18.0.1.1.0), phrases only (L1 finding: the 1.1.0 single
# words 「客服」「人工」「agent」 turned 「客服電話幾號」 into a 30 minute silence).
PREVIOUS_DEFAULT_HANDOFF_KEYWORDS = ('找真人,真人客服,轉真人,真人服務,轉人工,人工客服,找客服,專人服務,'
                                     'talk to a human,human agent,real person,live agent')
# im_livechat_ai 1.1.0 default: removed (backend decides) when migrated.
LEGACY_DEFAULT_HANDOFF_KEYWORDS = '真人,客服,人工,真人客服,human,agent,operator'
DEFAULT_HANDOFF_MESSAGE = '已幫您通知客服，請稍候。'
DEFAULT_PAUSED_MESSAGE = '真人客服會盡快回覆您，請稍候。在那之前 AI 助理暫停回覆。'

_KEYWORD_SPLIT_RE = re.compile(r'[,，、\n]')
# Keywords made only of these characters match as whole words ("agent" never
# matches "reagent"); others (Chinese) match anywhere in the text.
_WORD_CHARS_RE = re.compile(r'^[0-9a-z_ ]+$')
_SPACES_RE = re.compile(r'\s+')

MIGRATION_FIELDS = {
    'woow_ai_legacy_bot_partner_id', 'woow_ai_legacy_enabled', 'woow_ai_migration_state',
    'woow_ai_migration_note',
}


def keyword_list(raw):
    """Handoff keywords: split on , ， 、 and new lines, trimmed, lower case."""
    return [_SPACES_RE.sub(' ', k.strip().lower()) for k in _KEYWORD_SPLIT_RE.split(raw or '') if k.strip()]


def is_handoff_request(text, raw_keywords):
    """True when the visitor text contains one of the handoff keywords."""
    text = _SPACES_RE.sub(' ', (text or '').lower())
    if not text.strip():
        return False
    for keyword in keyword_list(raw_keywords):
        if _WORD_CHARS_RE.match(keyword):
            if re.search(r'(?<![0-9a-z_])' + re.escape(keyword) + r'(?![0-9a-z_])', text):
                return True
        elif keyword in text:
            return True
    return False


def url_host(value):
    value = (value or '').strip()
    if not value:
        return ''
    if '://' not in value:
        value = 'https://' + value
    try:
        return (urlsplit(value).hostname or '').lower().rstrip('.')
    except ValueError:
        return ''


class ImLivechatChannel(models.Model):
    _inherit = 'im_livechat.channel'

    woow_ai_enabled = fields.Boolean(
        string='AI Replies', copy=False, groups=MANAGER_GROUP,
        help='Visitor messages of this channel are answered by the AI contact.')
    woow_ai_contact_id = fields.Many2one(
        'woow.ai.contact', string='AI Contact', ondelete='restrict', groups=MANAGER_GROUP,
        domain=[('active', '=', True)],
        help='The AI contact that answers the visitors. Only system administrators see and change its '
             'backend. The backend should only use public data tools for Live Chat (channel type '
             '"livechat", guest visitors).')
    woow_ai_pause_minutes = fields.Integer(
        string='AI Pause After a Human Reply (minutes)', default=DEFAULT_PAUSE_MINUTES, groups=MANAGER_GROUP,
        help='When an operator writes in a conversation, or the visitor asks for a human, the AI stays '
             'silent in that conversation for this many minutes (1-1440). Operators resume it at once '
             'with the /ai on command.')
    woow_ai_handoff_keywords = fields.Char(
        string='AI Handoff Phrases', groups=MANAGER_GROUP,
        help='Optional. The AI backend decides when the visitor wants a human and hands over by itself '
             '(it ends its reply with [[handoff]], or n8n answers "handoff": true). Phrases here are an '
             'extra pre-filter: a visitor message containing one of them is handed over at once, without '
             'calling the backend. Separated by commas or new lines; Latin words match whole words only, '
             'case insensitive. Prefer phrases ("找真人") over single words ("客服"), which also match '
             'ordinary questions. Empty (default): the backend decides.')
    woow_ai_handoff_message = fields.Text(
        string='AI Handoff Message', default=DEFAULT_HANDOFF_MESSAGE, groups=MANAGER_GROUP,
        help='Posted by the AI when the visitor is handed over to a human: on a handoff phrase, or when '
             'the backend hands over without writing a reply of its own.')
    woow_ai_paused_message = fields.Text(
        string='AI Paused Notice', default=DEFAULT_PAUSED_MESSAGE, groups=MANAGER_GROUP,
        help='Posted once per pause when the visitor writes while the AI is paused and no operator wrote '
             'in the last few minutes; the operators are alerted again. Empty: no notice.')

    # --- Migration from im_livechat_ai 1.1.0 (written by the migration only) ---
    woow_ai_legacy_bot_partner_id = fields.Many2one(
        'res.partner', string='Previous AI Bot', readonly=True, copy=False, ondelete='set null',
        groups='base.group_system',
        help='Bot partner of im_livechat_ai on this channel: its old messages stay AI messages.')
    woow_ai_legacy_enabled = fields.Boolean(
        string='AI Was On In im_livechat_ai', readonly=True, copy=False, groups='base.group_system')
    woow_ai_migration_state = fields.Selection(
        [('none', 'Not migrated'), ('staged', 'Prepared'), ('switched', 'Switched'), ('finalized', 'Finished')],
        string='Migration', default='none', readonly=True, copy=False, groups='base.group_system')
    woow_ai_migration_note = fields.Text(string='Migration Report', readonly=True, copy=False,
                                         groups='base.group_system')
    woow_ai_legacy_available = fields.Boolean(string='im_livechat_ai Installed', compute='_compute_woow_ai_legacy_available',
                                              groups='base.group_system')

    def _compute_woow_ai_legacy_available(self):
        available = self.env['woow.ai.livechat.migration']._legacy_installed()
        for channel in self:
            channel.woow_ai_legacy_available = available

    # --- Constraints and guards ---

    @api.constrains('woow_ai_enabled', 'woow_ai_contact_id')
    def _check_woow_ai_contact(self):
        for channel in self.sudo():
            if not channel.woow_ai_enabled:
                continue
            if not channel.woow_ai_contact_id:
                raise ValidationError(self.env._('Choose the AI contact that answers this channel.'))
            if not channel.woow_ai_contact_id.active:
                raise ValidationError(self.env._('The AI contact of this channel is archived.'))
            if channel._woow_ai_legacy_enabled_now():
                raise ValidationError(self.env._(
                    'im_livechat_ai still answers this channel. Use "Switch from im_livechat_ai" (system '
                    'administrators) so that visitors are never answered twice.'))

    @api.constrains('woow_ai_pause_minutes')
    def _check_woow_ai_pause_minutes(self):
        # always checked, whatever the AI state (L1: 1.1.0 checked only while enabled)
        for channel in self.sudo():
            if not 1 <= (channel.woow_ai_pause_minutes or 0) <= MAX_PAUSE_MINUTES:
                raise ValidationError(self.env._('The pause must be between 1 and 1440 minutes.'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._woow_ai_check_migration_write(vals)
        return super().create(vals_list)

    def write(self, vals):
        self._woow_ai_check_migration_write(vals)
        if vals.get('ai_enabled') and 'ai_enabled' in self._fields:
            # im_livechat_ai turned on where an AI contact answers: refuse (never two answers)
            for channel in self.sudo():
                if channel.woow_ai_enabled:
                    raise ValidationError(self.env._(
                        'An AI contact answers this channel (AI Contact tab). Turn it off before turning on '
                        'im_livechat_ai.'))
        return super().write(vals)

    def _woow_ai_check_migration_write(self, vals):
        if MIGRATION_FIELDS & set(vals) and not self.env.context.get('woow_ai_migration'):
            raise UserError(self.env._('The migration fields are written by the migration actions only.'))

    # --- Runtime helpers (sudo) ---

    def _woow_ai_legacy_enabled_now(self):
        """True while im_livechat_ai 1.1.0 is installed and on for this channel
        (read with SQL: committed value, no dependency on its Python code)."""
        self.ensure_one()
        if not self.id or not sql.column_exists(self.env.cr, 'im_livechat_channel', 'ai_enabled'):
            return False
        if 'ai_enabled' in self._fields:
            self.flush_recordset(['ai_enabled'])
        self.env.cr.execute("SELECT ai_enabled FROM im_livechat_channel WHERE id = %s", (self.id,))
        row = self.env.cr.fetchone()
        return bool(row and row[0])

    def _woow_ai_answering_contact(self):
        """AI contact answering this channel's visitors, or an empty recordset."""
        self.ensure_one()
        channel = self.sudo()
        contact = channel.woow_ai_contact_id
        if not channel.woow_ai_enabled or not contact or not contact.active:
            return self.env['woow.ai.contact'].sudo()
        if channel._woow_ai_legacy_enabled_now():
            return self.env['woow.ai.contact'].sudo()
        return contact

    def _woow_ai_pause_minutes_value(self):
        self.ensure_one()
        minutes = self.sudo().woow_ai_pause_minutes or DEFAULT_PAUSE_MINUTES
        return max(1, min(minutes, MAX_PAUSE_MINUTES))

    def _woow_ai_has_operators(self):
        """True when this channel has an operator to hand a visitor to (an
        internal user among its operators)."""
        self.ensure_one()
        return any(not user.share for user in self.sudo().user_ids)

    def _woow_ai_is_handoff_request(self, text):
        self.ensure_one()
        return is_handoff_request(text, self.sudo().woow_ai_handoff_keywords)

    @api.model
    def _woow_ai_site_hosts(self):
        """Hosts whose links stay clickable in AI replies to visitors: web.base.url
        and the domains of the websites (prompt-injected phishing links are not)."""
        values = [self.env['ir.config_parameter'].sudo().get_param('web.base.url') or '']
        if 'website' in self.env:
            values += self.env['website'].sudo().search([]).mapped('domain')
        return sorted({host for host in map(url_host, values) if host})

    # --- Migration actions (system administrators) ---

    def _woow_ai_check_system(self):
        if not self.env.su and not self.env.user.has_group('base.group_system'):
            raise AccessError(self.env._('Only system administrators can run the im_livechat_ai migration.'))

    def action_woow_ai_legacy_prepare(self):
        self._woow_ai_check_system()
        self.env['woow.ai.livechat.migration']._prepare(raise_if_unavailable=True)
        return True

    def action_woow_ai_legacy_switch_wizard(self):
        self.ensure_one()
        self._woow_ai_check_system()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Switch from im_livechat_ai'),
            'res_model': 'woow.ai.livechat.switch.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_livechat_channel_id': self.id},
        }

    def action_woow_ai_legacy_switch_back(self):
        self._woow_ai_check_system()
        migration = self.env['woow.ai.livechat.migration']
        for channel in self:
            migration._switch_back(channel)
        return True

    def action_woow_ai_legacy_finish(self):
        self._woow_ai_check_system()
        self.env['woow.ai.livechat.migration']._finish()
        return True
