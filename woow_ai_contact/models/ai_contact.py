# -*- coding: utf-8 -*-
"""AI contacts: a bot partner people chat with in Discuss, and its backend.

Backend connection fields are restricted to system administrators. The API
key is write-only: it lives in an SQL-only column (``api_key_secret``, not an
ORM field), so no read, search, export or form ever returns it. The push key
is stored as a SHA-256 hash only and shown once when generated.
"""

import hashlib
import hmac
import logging
import secrets
from urllib.parse import urlsplit

from odoo import api, fields, models, SUPERUSER_ID
from odoo.exceptions import AccessError, ValidationError

from ..tools import media

_logger = logging.getLogger(__name__)

DEFAULT_MAX_HISTORY = 50
MAX_MAX_HISTORY = 200
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 3600
DEFAULT_MAX_DOWNLOAD_MB = 20
MAX_DOWNLOAD_MB = 100
DEFAULT_MAX_RETRIES = 1
MAX_MAX_RETRIES = 2
PUSH_KEY_PREFIX = 'wpk_'
BOT_CATEGORY_XMLID = 'woow_ai_contact.partner_category_ai_contact'


def hash_push_key(key):
    return hashlib.sha256((key or '').encode()).hexdigest()


def endpoint_url_allowed(url):
    """https, or http to localhost / an in-cluster *.svc.cluster.local host."""
    try:
        parts = urlsplit((url or '').strip())
    except ValueError:
        return False
    host = (parts.hostname or '').lower()
    if not host:
        return False
    if parts.scheme == 'https':
        return True
    if parts.scheme == 'http':
        return media.is_internal_host(host)
    return False


class WoowAiContact(models.Model):
    _name = 'woow.ai.contact'
    _description = 'AI Contact'
    _inherit = ['image.mixin']
    _order = 'sequence, id'

    name = fields.Char(string='Name', required=True, translate=False)
    description = fields.Text(string='Description', help='Shown to users in the AI contact lists.')
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    partner_id = fields.Many2one(
        'res.partner', string='Bot Partner', readonly=True, copy=False,
        ondelete='restrict', index=True,
        help='Partner the AI posts as. Created automatically; name and avatar follow the AI contact.',
    )
    allowed_group_ids = fields.Many2many(
        'res.groups', 'woow_ai_contact_res_groups_rel', 'contact_id', 'group_id',
        string='Allowed Groups', groups='base.group_system',
        default=lambda self: self._default_allowed_groups(),
        help='Users in one of these groups can chat with this AI contact. Empty: nobody can open a '
             'direct chat (the AI contact is then only used by bridges such as Live Chat) and its bot '
             'partner stays archived.',
    )

    # --- Backend (system administrators only) ---
    backend_type = fields.Selection(
        [('openai', 'OpenAI compatible (Hermes, LiteLLM)'), ('n8n', 'n8n webhook')],
        string='Backend', required=True, default='openai', groups='base.group_system',
    )
    endpoint_url = fields.Char(
        string='Endpoint URL', groups='base.group_system',
        help='OpenAI compatible: the base URL ending in /v1 (…/chat/completions is added). '
             'n8n: the full webhook URL. Must be https, except localhost and *.svc.cluster.local.',
    )
    api_key = fields.Char(
        string='API Key', compute='_compute_api_key', inverse='_inverse_api_key',
        store=False, groups='base.group_system', copy=False,
        help='OpenAI compatible: sent as "Authorization: Bearer". n8n: the signing secret of the '
             'X-Woow-Signature HMAC (never sent). Leave empty to keep the current key; the saved key '
             'is never shown again.',
    )
    api_key_set = fields.Boolean(string='API Key Configured', compute='_compute_api_key',
                                 groups='base.group_system')
    model = fields.Char(string='Model', groups='base.group_system',
                        help='OpenAI compatible backends only. Leave empty to use the backend default.')
    system_prompt = fields.Text(string='System Prompt', groups='base.group_system',
                                help='Optional. Leave empty to let the backend decide the role.')
    max_history = fields.Integer(string='History Messages', default=DEFAULT_MAX_HISTORY,
                                 groups='base.group_system',
                                 help='Most recent messages sent as context (1-200).')
    timeout_seconds = fields.Integer(string='Timeout (seconds)', default=DEFAULT_TIMEOUT,
                                     groups='base.group_system',
                                     help='Total time allowed for one answer (5-3600 seconds).')
    openai_stream = fields.Boolean(
        string='Stream Replies', default=True, groups='base.group_system',
        help='OpenAI compatible backends only: ask for a streamed (SSE) answer. Bytes keep flowing while '
             'the backend works, so proxies such as Cloudflare (about 100 seconds without data) do not '
             'cut long answers. The call fails when no data arrives for 30 seconds.')
    max_retries = fields.Integer(
        string='Retries', default=DEFAULT_MAX_RETRIES, groups='base.group_system',
        help='Retries (0-2) when the connection fails or the backend answers HTTP 429 or 503, after 3 '
             'and 10 seconds. Read timeouts and other HTTP errors are never retried: the backend may '
             'already be running tools.')
    welcome_message = fields.Text(string='Welcome Message', groups='base.group_system',
                                  help='Posted by the AI when a user opens the conversation for the first time.')
    error_message = fields.Text(string='Error Message', groups='base.group_system',
                                help='Posted when the backend fails. Empty: a default message.')
    download_domains = fields.Text(
        string='Download Domains', groups='base.group_system',
        help='Files linked in AI replies are downloaded and attached only from these hosts. '
             'One per line: an exact host name or *.example.com.',
    )
    max_download_mb = fields.Integer(string='Max File Size (MB)', default=DEFAULT_MAX_DOWNLOAD_MB,
                                     groups='base.group_system')
    push_key_hash = fields.Char(string='Push Key Hash', readonly=True, copy=False, index=True,
                                groups='base.group_system')
    push_key_hint = fields.Char(string='Push Key', readonly=True, copy=False,
                                groups='base.group_system')
    log_payload = fields.Boolean(
        string='Log Request Content', groups='base.group_system',
        help='Debugging only: store the request and response content in the call logs. '
             'They contain private conversations. Turning it off erases stored content.',
    )
    call_log_count = fields.Integer(compute='_compute_call_log_count', groups='base.group_system')

    _sql_constraints = [
        ('partner_uniq', 'unique(partner_id)', 'Each bot partner can belong to one AI contact only.'),
    ]

    def init(self):
        # SQL-only column: never an ORM field, so never returned by any read.
        self.env.cr.execute("ALTER TABLE woow_ai_contact ADD COLUMN IF NOT EXISTS api_key_secret VARCHAR")

    @api.model
    def _default_allowed_groups(self):
        groups = self.env['res.groups']
        for xmlid in ('base.group_user', 'base.group_portal'):
            group = self.env.ref(xmlid, raise_if_not_found=False)
            if group:
                groups |= group
        return groups

    # --- API key (write-only) ---

    def _compute_api_key(self):
        keys = self._get_api_keys()
        for contact in self:
            contact.api_key = False
            contact.api_key_set = bool(keys.get(contact.id))

    def _inverse_api_key(self):
        for contact in self:
            new_key = (contact.api_key or '').strip()
            if new_key:
                contact._check_admin()
                contact._set_api_key(new_key)
        self.invalidate_recordset(['api_key', 'api_key_set'])

    def _set_api_key(self, key):
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute("UPDATE woow_ai_contact SET api_key_secret = %s WHERE id = %s",
                            (key or None, self.id))
        _logger.info("AI contact %s: API key updated by user %s", self.id, self.env.uid)

    def _get_api_keys(self):
        ids = [i for i in self.ids if isinstance(i, int)]
        if not ids:
            return {}
        self.env.cr.execute("SELECT id, api_key_secret FROM woow_ai_contact WHERE id IN %s", (tuple(ids),))
        return dict(self.env.cr.fetchall())

    def _get_api_key(self):
        self.ensure_one()
        return self._get_api_keys().get(self.id) or ''

    def action_clear_api_key(self):
        self._check_admin()
        for contact in self:
            contact._set_api_key(False)
        self.invalidate_recordset(['api_key_set'])

    def _check_admin(self):
        if not self.env.su and not self.env.user.has_group('base.group_system'):
            raise AccessError(self.env._('Only system administrators can change AI contact connections.'))

    # --- Constraints ---

    @api.constrains('endpoint_url')
    def _check_endpoint_url(self):
        for contact in self.sudo():
            url = (contact.endpoint_url or '').strip()
            if url and not endpoint_url_allowed(url):
                raise ValidationError(self.env._(
                    'The endpoint URL must use https (plain http is only allowed for localhost '
                    'and *.svc.cluster.local).'))

    @api.constrains('max_history', 'timeout_seconds', 'max_download_mb', 'max_retries')
    def _check_limits(self):
        for contact in self.sudo():
            if not 1 <= contact.max_history <= MAX_MAX_HISTORY:
                raise ValidationError(self.env._('History messages must be between 1 and 200.'))
            if not 5 <= contact.timeout_seconds <= MAX_TIMEOUT:
                raise ValidationError(self.env._('The timeout must be between 5 and 3600 seconds.'))
            if not 1 <= contact.max_download_mb <= MAX_DOWNLOAD_MB:
                raise ValidationError(self.env._('The maximum file size must be between 1 and 100 MB.'))
            if not 0 <= contact.max_retries <= MAX_MAX_RETRIES:
                raise ValidationError(self.env._('Retries must be between 0 and 2.'))

    @api.constrains('partner_id')
    def _check_partner(self):
        for contact in self.sudo():
            if contact.partner_id and contact.partner_id.with_context(active_test=False).user_ids:
                raise ValidationError(self.env._('The bot partner of an AI contact cannot be a user.'))

    # --- Bot partner ---

    def _partner_vals(self):
        self.ensure_one()
        category = self.env.ref(BOT_CATEGORY_XMLID, raise_if_not_found=False)
        vals = {
            'name': self.name,
            'active': self._partner_should_be_active(),
            'is_company': False,
            'type': 'contact',
        }
        if self.image_1920:
            vals['image_1920'] = self.image_1920
        if category:
            vals['category_id'] = [(4, category.id)]
        return vals

    def _partner_should_be_active(self):
        """The bot partner is active only while people can chat with it directly:
        an archived AI contact, or one without allowed groups (only used by
        bridges such as Live Chat), keeps it archived (no chat search hit)."""
        self.ensure_one()
        contact = self.sudo()
        return bool(contact.active and contact.allowed_group_ids)

    def _sync_partner_active(self):
        for contact in self.sudo():
            partner = contact.partner_id.with_context(active_test=False)
            should = contact._partner_should_be_active()
            if partner and partner.active != should:
                partner.active = should

    @api.model_create_multi
    def create(self, vals_list):
        contacts = super().create(vals_list)
        for contact in contacts:
            if not contact.partner_id:
                partner = self.env['res.partner'].sudo().create(contact._partner_vals())
                contact.sudo().partner_id = partner
        return contacts

    def write(self, vals):
        res = super().write(vals)
        if {'name', 'image_1920'} & set(vals):
            for contact in self.sudo():
                if contact.partner_id:
                    partner_vals = {'name': contact.name}
                    if 'image_1920' in vals:
                        partner_vals['image_1920'] = contact.image_1920 or False
                    contact.partner_id.write(partner_vals)
        if 'log_payload' in vals and not vals['log_payload']:
            self.env['woow.ai.call.log'].sudo()._erase_payloads(self.ids)
        if {'active', 'allowed_group_ids'} & set(vals):
            self._sync_partner_active()
        return res

    def copy_data(self, default=None):
        vals_list = super().copy_data(default=default)
        for contact, vals in zip(self, vals_list):
            vals['name'] = self.env._('%s (copy)', contact.name)
            vals.pop('partner_id', None)
        return vals_list

    # --- Push key ---

    def action_generate_push_key(self):
        """Generate a new push key, store its hash, show it once."""
        self.ensure_one()
        self._check_admin()
        key = PUSH_KEY_PREFIX + secrets.token_urlsafe(32)
        self.sudo().write({
            'push_key_hash': hash_push_key(key),
            'push_key_hint': key[:8] + '…' + key[-4:],
        })
        _logger.info("AI contact %s: push key regenerated by user %s", self.id, self.env.uid)
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Push Key'),
            'res_model': 'woow.ai.push.key.wizard',
            'view_mode': 'form',
            'target': 'new',
            # The key travels only in this response: the wizard is never saved.
            'context': {'default_contact_id': self.id, 'default_push_key': key},
        }

    def action_revoke_push_key(self):
        self._check_admin()
        self.sudo().write({'push_key_hash': False, 'push_key_hint': False})

    @api.model
    def _find_by_push_key(self, key):
        """Active AI contact whose push key is `key`, else an empty recordset."""
        if not key or not key.startswith(PUSH_KEY_PREFIX):
            return self.browse()
        digest = hash_push_key(key)
        contact = self.sudo().search([('push_key_hash', '=', digest)], limit=1)
        if contact and hmac.compare_digest(contact.push_key_hash or '', digest):
            return contact
        return self.browse()

    # --- Access ---

    def _user_allowed(self, user):
        """True when `user` (res.users) may chat with this AI contact."""
        self.ensure_one()
        if not user or not user.active or user._is_public():
            return False
        groups = self.sudo().allowed_group_ids
        return bool(groups & user.sudo().groups_id)

    @api.model
    def _contacts_for_user(self, user):
        contacts = self.sudo().search([])
        return contacts.filtered(lambda c: c._user_allowed(user))

    # --- Direct chat ---

    def _find_chat(self, partner):
        """Existing chat between `partner` and this AI contact, or empty."""
        self.ensure_one()
        Channel = self.env['discuss.channel'].sudo()
        bot = self.sudo().partner_id
        if not bot or not partner:
            return Channel
        Channel.flush_model()
        self.env['discuss.channel.member'].flush_model()
        pids = sorted({bot.id, partner.id})
        self.env.cr.execute("""
            SELECT M.channel_id
              FROM discuss_channel C
              JOIN discuss_channel_member M ON M.channel_id = C.id
             WHERE C.channel_type = 'chat' AND M.partner_id IN %s
               AND NOT EXISTS (SELECT 1 FROM discuss_channel_member M2
                                WHERE M2.channel_id = C.id AND
                                      (M2.partner_id IS NULL OR M2.partner_id NOT IN %s))
          GROUP BY M.channel_id
            HAVING ARRAY_AGG(DISTINCT M.partner_id ORDER BY M.partner_id) = %s
          ORDER BY M.channel_id
             LIMIT 1
        """, (tuple(pids), tuple(pids), pids))
        row = self.env.cr.fetchone()
        return Channel.browse(row[0]) if row else Channel

    def _get_or_create_chat(self, user):
        """Find or create the direct chat of `user` with this AI contact.

        The chat is pinned for the user. On creation the welcome message (if
        any) is posted by the AI. Access (allowed groups) is checked by the
        caller.

        Returns:
            tuple: (discuss.channel (sudo), created: bool)
        """
        self.ensure_one()
        contact = self.sudo()
        chat = contact._find_chat(user.partner_id)
        created = False
        if chat:
            member = chat.channel_member_ids.filtered(lambda m: m.partner_id == user.partner_id)
            if member and member.unpin_dt:
                member.write({'unpin_dt': False, 'last_interest_dt': fields.Datetime.now()})
        else:
            chat = self.env['discuss.channel'].with_user(user).sudo().channel_get(
                [contact.partner_id.id], pin=True)
            chat = chat.with_user(SUPERUSER_ID).sudo()
            created = True
        if not chat.allow_public_upload:
            # portal users may only upload files in channels that allow it
            # (mail/controllers/attachment.py); the chat has only two members
            chat.allow_public_upload = True
        if created:
            welcome = (contact.welcome_message or '').strip()
            if welcome:
                contact._post_text(chat, welcome)
        return chat, created

    def _post_text(self, channel, text):
        """Post a markdown text as the AI (no attachments)."""
        self.ensure_one()
        prepared = self._prepare_reply({'text': text}, channel=channel)
        return self._post_prepared(channel, prepared)

    # --- Call logs ---

    def _compute_call_log_count(self):
        data = self.env['woow.ai.call.log'].sudo()._read_group(
            [('contact_id', 'in', self.ids)], ['contact_id'], ['__count'])
        counts = {contact.id: count for contact, count in data}
        for contact in self:
            contact.call_log_count = counts.get(contact.id, 0)

    def action_view_call_logs(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('woow_ai_contact.action_woow_ai_call_log')
        action['domain'] = [('contact_id', '=', self.id)]
        action['context'] = {'default_contact_id': self.id}
        return action

    def action_open_bot_partner(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'res.partner',
            'res_id': self.partner_id.id,
            'view_mode': 'form',
        }
