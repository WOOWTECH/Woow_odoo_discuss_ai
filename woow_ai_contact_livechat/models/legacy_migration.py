# -*- coding: utf-8 -*-
"""Staged migration from im_livechat_ai 1.1.0 (docs/LIVECHAT.md "Migration").

1. Prepare (post_init_hook, "Prepare again"): AI contacts are created from the
   1.1.0 settings and every 1.1.0 channel is linked to one of them, AI off,
   state "staged". im_livechat_ai keeps answering: installing is not switching
   (its product tools would disappear at once).
2. Switch (per channel, system administrator, price regression confirmed):
   1.1.0 off, AI contact on (as 1.1.0 was), running pauses and fresh pending
   jobs moved; reversible with "Switch back" while 1.1.0 is installed.
3. Finish: every channel switched, nothing left running in 1.1.0; the shared
   bot partner's xmlid is handed to this module and the 1.1.0 system
   parameters (API key first) are deleted.
4. Uninstall im_livechat_ai.

The 1.1.0 data is read with SQL only (no dependency on its Python code or
field groups), and only from 18.0.1.1.0 on: the 18.0.1.0.x per-channel key
columns held leaked provider keys and are never read.
"""

import logging
import zlib

from odoo import Command, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import parse_version, sql

from odoo.addons.woow_ai_contact.models.ai_contact import endpoint_url_allowed

from .im_livechat_channel import (
    DEFAULT_HANDOFF_KEYWORDS, DEFAULT_HANDOFF_MESSAGE, DEFAULT_PAUSE_MINUTES, LEGACY_DEFAULT_HANDOFF_KEYWORDS,
    MAX_PAUSE_MINUTES, keyword_list,
)

_logger = logging.getLogger(__name__)

MODULE = 'woow_ai_contact_livechat'
LEGACY_MODULE = 'im_livechat_ai'
MIN_LEGACY_VERSION = '18.0.1.1.0'
# 1.1.0 job lock: first key of its two-int advisory lock (second: discuss.channel id)
LEGACY_LOCK_NAMESPACE = zlib.crc32(b'im_livechat_ai.job') & 0x7FFFFFFF
LEGACY_JOB_MAX_AGE_MINUTES = 30
LEGACY_ICP_KEYS = (
    'im_livechat_ai.api_key', 'im_livechat_ai.base_url', 'im_livechat_ai.model_cache',
    'im_livechat_ai.log_payload', 'im_livechat_ai.log_retention_days',
)
LEGACY_DEFAULT_BOT_NAME = 'AI 客服助理'
# Visitors should not wait long; streamed answers keep proxies (Cloudflare 100 s) open.
MIGRATED_TIMEOUT_SECONDS = 120
XMLIDS = (
    ('partner_ai_bot', 'legacy_partner_ai_bot'),
    ('partner_category_ai_bot', 'legacy_partner_category_ai_bot'),
)
# im_livechat_ai 1.1.0 PRODUCT_GUARD_NO_TOOLS, word for word (not imported):
# the migrated contact talks to LiteLLM without tools, so it must not quote
# prices; remove it once the backend looks prices up (Hermes / n8n + Odoo MCP).
PRODUCT_GUARD_NO_TOOLS = (
    '【商品資料規則】\n'
    '你目前無法查詢官網的商品資料。不要回答任何價格、規格或庫存的具體內容，'
    '不可憑記憶、推測或自行估算，絕對不可編造價格；'
    '請訪客留下聯絡方式（姓名、電話或 Email），由客服人員報價或回覆。'
)
_CHANNEL_COLUMNS = (
    'name', 'ai_enabled', 'ai_model', 'ai_system_prompt', 'ai_max_history', 'ai_max_retries',
    'ai_error_message', 'ai_handoff_pause_minutes', 'ai_handoff_keywords', 'ai_handoff_message',
    'ai_bot_partner_id', 'ai_bot_name', 'ai_product_lookup',
)


def normalize_base_url(value):
    url = (value or '').strip().rstrip('/')
    if url.endswith('/chat/completions'):
        url = url[:-len('/chat/completions')].rstrip('/')
    return url


def _clamp(value, low, high, default):
    if value is None:
        return default
    return max(low, min(int(value), high))


class WoowAiLivechatMigration(models.AbstractModel):
    _name = 'woow.ai.livechat.migration'
    _description = 'AI Contacts: migration from im_livechat_ai'

    # --- State of im_livechat_ai ---

    @api.model
    def _legacy_module(self):
        """(state, latest_version) of im_livechat_ai, or (None, None)."""
        self.env.cr.execute("SELECT state, latest_version FROM ir_module_module WHERE name = %s", (LEGACY_MODULE,))
        row = self.env.cr.fetchone()
        return row if row else (None, None)

    @api.model
    def _legacy_installed(self):
        state, _version = self._legacy_module()
        return state == 'installed' and sql.column_exists(self.env.cr, 'im_livechat_channel', 'ai_enabled')

    @api.model
    def _legacy_ready(self):
        """(True, '') when the 1.1.0 data can be migrated, else (False, reason)."""
        state, version = self._legacy_module()
        cr = self.env.cr
        if state != 'installed' or not sql.column_exists(cr, 'im_livechat_channel', 'ai_enabled'):
            return False, 'not_installed'
        if not version or parse_version(version) < parse_version(MIN_LEGACY_VERSION):
            return False, self.env._('im_livechat_ai %(version)s is older than %(min)s: upgrade it first.',
                                     version=version or '?', min=MIN_LEGACY_VERSION)
        if sql.column_exists(cr, 'im_livechat_channel', 'ai_api_key'):
            # 1.1.0's migration drops the leaked per-channel keys; still there: it never ran
            return False, self.env._('im_livechat_ai still has the 18.0.1.0.x per-channel key column: '
                                     'its 18.0.1.1.0 migration did not run. Upgrade it first.')
        missing = [c for c in _CHANNEL_COLUMNS if c not in ('ai_max_retries', 'ai_product_lookup')
                   and not sql.column_exists(cr, 'im_livechat_channel', c)]
        if missing:
            return False, self.env._('im_livechat_ai data is incomplete (missing %(columns)s).',
                                     columns=', '.join(missing))
        return True, ''

    @api.model
    def _legacy_channel_bot_ids(self):
        """Bot partners of the im_livechat_ai channels (while it is installed)."""
        cr = self.env.cr
        if not sql.column_exists(cr, 'im_livechat_channel', 'ai_bot_partner_id'):
            return set()
        cr.execute("SELECT DISTINCT ai_bot_partner_id FROM im_livechat_channel WHERE ai_bot_partner_id IS NOT NULL")
        return {row[0] for row in cr.fetchall()}

    @api.model
    def _legacy_rows(self):
        """1.1.0 channels to migrate: AI on, a prompt or a bot partner (its
        18.0.1.1.0 upgrade turned every channel off, so "on" alone misses the
        configured ones)."""
        cr = self.env.cr
        columns = [c if sql.column_exists(cr, 'im_livechat_channel', c) else 'NULL AS %s' % c
                   for c in _CHANNEL_COLUMNS]
        self.env['im_livechat.channel'].flush_model()
        cr.execute("""
            SELECT id, %s FROM im_livechat_channel
             WHERE ai_enabled IS TRUE OR NULLIF(btrim(ai_system_prompt), '') IS NOT NULL
                OR ai_bot_partner_id IS NOT NULL
          ORDER BY id""" % ', '.join(columns))  # noqa: S608 - fixed column names
        return cr.dictfetchall()

    # --- 1. Prepare ---

    @staticmethod
    def _group_key(row):
        return (
            row['ai_model'] or '',
            (row['ai_system_prompt'] or '').strip(),
            (row['ai_bot_name'] or '').strip() or LEGACY_DEFAULT_BOT_NAME,
            (row['ai_error_message'] or '').strip(),
            _clamp(row['ai_max_history'], 1, 200, 50),
            _clamp(row['ai_max_retries'], 0, 2, 1),
        )

    @api.model
    def _prepare(self, raise_if_unavailable=False):
        """Link every 1.1.0 channel to an AI contact (AI off). Idempotent.

        Returns:
            dict: {im_livechat.channel id: woow.ai.contact id} of the channels prepared now
        """
        ready, reason = self._legacy_ready()
        if not ready:
            if reason != 'not_installed':
                _logger.warning("woow_ai_contact_livechat: im_livechat_ai not migrated: %s", reason)
            if raise_if_unavailable:
                raise UserError(reason if reason != 'not_installed' else self.env._('im_livechat_ai is not installed.'))
            return {}
        ICP = self.env['ir.config_parameter'].sudo()
        base_url = normalize_base_url(ICP.get_param('im_livechat_ai.base_url'))
        if base_url and not endpoint_url_allowed(base_url):
            base_url = ''
        api_key = (ICP.get_param('im_livechat_ai.api_key') or '').strip()
        Channel = self.env['im_livechat.channel'].sudo().with_context(active_test=False)

        groups = {}
        prepared = {}
        for row in self._legacy_rows():
            channel = Channel.browse(row['id'])
            if channel.woow_ai_contact_id:
                if channel.woow_ai_migration_state == 'none':
                    # a Live Chat manager picked an AI contact before prepare could run
                    channel.with_context(woow_ai_migration=True).write(self._adopt_vals(row, channel))
                    prepared[channel.id] = channel.woow_ai_contact_id.id
                continue
            groups.setdefault(self._group_key(row), []).append(row)
        for key, rows in groups.items():
            channels = Channel.browse([row['id'] for row in rows])
            todo = [row for row in rows if not Channel.browse(row['id']).woow_ai_contact_id]
            if not todo:
                continue
            contact = channels.woow_ai_contact_id[:1]
            if not contact:
                contact = self._create_contact(rows[0], key, base_url, api_key)
            for row in todo:
                channel = Channel.browse(row['id'])
                channel.with_context(woow_ai_migration=True).write(
                    self._channel_vals(row, contact, base_url, api_key))
                prepared[channel.id] = contact.id
        if prepared:
            _logger.warning(
                "woow_ai_contact_livechat: prepared %s im_livechat_ai channel(s) %s (AI off; im_livechat_ai keeps "
                "answering until each channel is switched)", len(prepared), sorted(prepared))
        return prepared

    @api.model
    def _adopt_vals(self, row, channel):
        """Migration state of a 1.1.0 channel whose AI contact was set by hand
        (its contact and handoff settings are kept): staged while 1.1.0 still
        answers it, switched when 1.1.0 is off there (nothing left to switch)."""
        legacy_on = bool(row['ai_enabled'])
        note = self.env._('Found on %(date)s (UTC) with the AI contact "%(name)s" already set by hand: kept. '
                          'im_livechat_ai answered this channel: %(answer)s.',
                          date=fields.Datetime.to_string(fields.Datetime.now()),
                          name=channel.woow_ai_contact_id.name,
                          answer=self.env._('yes') if legacy_on else self.env._('no'))
        return {
            'woow_ai_legacy_bot_partner_id': row['ai_bot_partner_id'] or False,
            'woow_ai_legacy_enabled': legacy_on,
            'woow_ai_migration_state': 'staged' if legacy_on else 'switched',
            'woow_ai_migration_note': '\n'.join(filter(None, [channel.woow_ai_migration_note, note])),
        }

    @api.model
    def _create_contact(self, row, key, base_url, api_key):
        model, prompt, name, error_message, max_history, max_retries = key
        vals = {
            'name': name,
            'description': self.env._('Live Chat AI migrated from im_livechat_ai.'),
            'backend_type': 'openai',
            'endpoint_url': base_url or False,
            'model': model or False,
            'system_prompt': '\n\n'.join(part for part in (prompt, PRODUCT_GUARD_NO_TOOLS) if part),
            'max_history': max_history,
            'max_retries': max_retries,
            'timeout_seconds': MIGRATED_TIMEOUT_SECONDS,
            'openai_stream': True,
            'error_message': error_message or False,
            'welcome_message': False,
            'allowed_group_ids': [Command.clear()],  # Live Chat only: no direct chats
            'log_payload': False,
        }
        partner = self._reusable_bot_partner(row['ai_bot_partner_id'])
        if partner:
            vals['partner_id'] = partner.id  # same name and avatar: visitors see the same bot
        contact = self.env['woow.ai.contact'].sudo().create(vals)
        if api_key:
            contact._set_api_key(api_key)  # SQL only, never logged
        contact._sync_partner_active()
        return contact

    @api.model
    def _reusable_bot_partner(self, partner_id):
        if not partner_id:
            return self.env['res.partner']
        partner = self.env['res.partner'].sudo().with_context(active_test=False).browse(partner_id).exists()
        if not partner or partner.user_ids:
            return self.env['res.partner']
        shared = self.env.ref('im_livechat_ai.partner_ai_bot', raise_if_not_found=False)
        if shared and shared == partner:
            return self.env['res.partner']
        if self.env['woow.ai.contact'].sudo().with_context(active_test=False).search_count(
                [('partner_id', '=', partner.id)], limit=1):
            return self.env['res.partner']
        return partner

    @api.model
    def _channel_vals(self, row, contact, base_url, api_key):
        keywords = row['ai_handoff_keywords']
        notes = [self.env._('Prepared from im_livechat_ai on %(date)s (UTC): AI contact "%(name)s" (id %(id)s).',
                            date=fields.Datetime.to_string(fields.Datetime.now()), name=contact.name, id=contact.id)]
        notes.append(self.env._('im_livechat_ai answered this channel: %(answer)s.',
                                answer=self.env._('yes') if row['ai_enabled'] else self.env._('no')))
        if keywords and keyword_list(keywords) == keyword_list(LEGACY_DEFAULT_HANDOFF_KEYWORDS):
            keywords = DEFAULT_HANDOFF_KEYWORDS
            notes.append(self.env._('The 1.1.0 default handoff words (single words such as 客服, 人工) were replaced '
                                    'by phrases: %(keywords)s.', keywords=DEFAULT_HANDOFF_KEYWORDS))
        if row['ai_product_lookup'] or row['ai_product_lookup'] is None:
            notes.append(self.env._('The channel used the 1.1.0 product tools. The new contact has none: its prompt '
                                    'forbids quoting prices until the backend looks them up (Hermes / n8n with Odoo '
                                    'MCP); run the price regression before switching.'))
        notes.append(self.env._('Not migrated: temperature, max tokens (set them in the backend / LiteLLM).'))
        if not base_url:
            notes.append(self.env._('Fill in the endpoint URL of the AI contact (the 1.1.0 URL was empty or not '
                                    'https).'))
        if not api_key:
            notes.append(self.env._('Fill in the API key of the AI contact (1.1.0 had none).'))
        return {
            'woow_ai_contact_id': contact.id,
            'woow_ai_enabled': False,
            'woow_ai_pause_minutes': _clamp(row['ai_handoff_pause_minutes'] or None, 1, MAX_PAUSE_MINUTES,
                                            DEFAULT_PAUSE_MINUTES),
            'woow_ai_handoff_keywords': keywords or False,
            'woow_ai_handoff_message': (row['ai_handoff_message'] or '').strip() or DEFAULT_HANDOFF_MESSAGE,
            'woow_ai_legacy_bot_partner_id': row['ai_bot_partner_id'] or False,
            'woow_ai_legacy_enabled': bool(row['ai_enabled']),
            'woow_ai_migration_state': 'staged',
            'woow_ai_migration_note': '\n'.join(notes),
        }

    # --- 2. Switch (and back) ---

    @api.model
    def _switch(self, channel, price_regression_confirmed=False):
        """Hand one channel from im_livechat_ai to its AI contact (one transaction).

        Returns:
            dict: moved (jobs moved), busy (conversations 1.1.0 was answering: their waiting
                messages are moved too and answered once 1.1.0 is done, see _move_legacy_jobs)
        """
        channel = channel.sudo()
        channel.ensure_one()
        if channel.woow_ai_migration_state != 'staged':
            raise UserError(self.env._('Only a prepared channel can be switched (%(name)s).', name=channel.name))
        if not price_regression_confirmed:
            raise UserError(self.env._('Confirm that the price regression passed on the new backend first.'))
        contact = channel.woow_ai_contact_id
        if not contact or not contact.active:
            raise UserError(self.env._('The channel has no active AI contact.'))
        if not (contact.endpoint_url or '').strip() or (contact.backend_type == 'openai' and not contact.api_key_set):
            raise UserError(self.env._('Fill in the endpoint URL and the API key of the AI contact "%(name)s" first.',
                                       name=contact.name))
        cr = self.env.cr
        self.env.flush_all()
        if sql.column_exists(cr, 'im_livechat_channel', 'ai_enabled'):
            cr.execute("UPDATE im_livechat_channel SET ai_enabled = FALSE WHERE id = %s", (channel.id,))
            self.env['im_livechat.channel'].invalidate_model()
        channel.with_context(woow_ai_migration=True).write({
            # on when 1.1.0 answered, and kept on when a Live Chat manager already turned it on
            'woow_ai_enabled': bool(channel.woow_ai_enabled or channel.woow_ai_legacy_enabled),
            'woow_ai_migration_state': 'switched',
        })
        self.env.flush_all()
        if sql.column_exists(cr, 'discuss_channel', 'ai_paused_until'):
            cr.execute("""
                UPDATE discuss_channel
                   SET woow_ai_paused_until = GREATEST(woow_ai_paused_until, ai_paused_until)
                 WHERE livechat_channel_id = %s AND ai_paused_until > (now() AT TIME ZONE 'UTC')""", (channel.id,))
            self.env['discuss.channel'].invalidate_model(['woow_ai_paused_until'])
        moved, busy = self._move_legacy_jobs(channel, contact)
        note = self.env._('Switched on %(date)s (UTC) by %(user)s: AI %(state)s, %(moved)s job(s) moved '
                          '(%(busy)s conversation(s) answered by the AI contact once im_livechat_ai is done).',
                          date=fields.Datetime.to_string(fields.Datetime.now()), user=self.env.user.name,
                          state=self.env._('on') if channel.woow_ai_enabled else self.env._('off'),
                          moved=moved, busy=busy)
        channel.with_context(woow_ai_migration=True).write({
            'woow_ai_migration_note': '\n'.join(filter(None, [channel.woow_ai_migration_note, note]))})
        _logger.info("woow_ai_contact_livechat: channel %s switched from im_livechat_ai by user %s "
                     "(%s jobs moved, %s busy)", channel.id, self.env.uid, moved, busy)
        return {'moved': moved, 'busy': busy}

    @api.model
    def _move_legacy_jobs(self, channel, contact):
        """Move the unfinished 1.1.0 jobs of the channel (< 30 minutes old).

        Once the channel is switched, 1.1.0 skips every job it has not
        answered yet (its ``_skip_reply`` checks ``ai_enabled`` first), so all
        of them become woow.ai.job: pending ones, and running ones left by a
        crash (their session lock is free). A conversation 1.1.0 is answering
        right now (its session lock is held) cannot be taken over: its running
        job is left alone, but it will be skipped anyway when a newer 1.1.0
        job exists (``_has_newer_job`` counts every state), so the waiting
        jobs are moved too and the AI contact answers them after 1.1.0 is
        done (woow.ai.job waits while the 1.1.0 lock is held and does not
        repeat what a 1.1.0 reply already covered, see the bridge's
        woow.ai.job).

        Returns:
            tuple: (jobs moved, conversations 1.1.0 was answering)
        """
        cr = self.env.cr
        if not sql.table_exists(cr, 'im_livechat_ai_job'):
            return 0, 0
        cr.execute("""
            SELECT id, discuss_channel_id, kind, message_id, state FROM im_livechat_ai_job
             WHERE livechat_channel_id = %s AND state IN ('pending', 'running')
               AND create_date > (now() AT TIME ZONE 'UTC') - interval '%s minutes'
          ORDER BY id""", (channel.id, LEGACY_JOB_MAX_AGE_MINUTES))
        by_session = {}
        for job_id, session_id, kind, message_id, state in cr.fetchall():
            by_session.setdefault(session_id, []).append((job_id, kind, message_id, state))
        Session = self.env['discuss.channel'].sudo()
        Message = self.env['mail.message'].sudo()
        Job = self.env['woow.ai.job'].sudo()
        moved = busy = 0
        for session_id, jobs in by_session.items():
            # transaction lock: excludes 1.1.0's session lock (same key) until commit
            cr.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", (LEGACY_LOCK_NAMESPACE, session_id))
            if not cr.fetchone()[0]:
                busy += 1  # 1.1.0 is answering: its running job finishes there
                jobs = [job for job in jobs if job[3] == 'pending']
                if not jobs:
                    continue
            session = Session.browse(session_id).exists()
            for _job_id, kind, message_id, _state in jobs:
                message = Message.browse(message_id).exists() if message_id else Message
                if session and message:
                    Job._create_for_message(session, contact, message,
                                            kind=kind if kind in ('reply', 'handoff') else 'reply')
                    moved += 1
            cr.execute("""
                UPDATE im_livechat_ai_job SET state = 'skipped', error = %s, finished_at = (now() AT TIME ZONE 'UTC')
                 WHERE id IN %s""", ('moved to woow_ai_contact', tuple(j[0] for j in jobs)))
        return moved, busy

    @api.model
    def _switch_back(self, channel):
        """Give a switched channel back to im_livechat_ai (while it is installed)."""
        channel = channel.sudo()
        channel.ensure_one()
        if channel.woow_ai_migration_state != 'switched':
            raise UserError(self.env._('Only a switched channel can be switched back (%(name)s).', name=channel.name))
        if not self._legacy_installed():
            raise UserError(self.env._('im_livechat_ai is not installed any more.'))
        channel.with_context(woow_ai_migration=True).write({'woow_ai_enabled': False, 'woow_ai_migration_state': 'staged'})
        self.env.flush_all()
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_enabled = %s WHERE id = %s",
                            (bool(channel.woow_ai_legacy_enabled), channel.id))
        self.env['im_livechat.channel'].invalidate_model()
        _logger.info("woow_ai_contact_livechat: channel %s switched back to im_livechat_ai by user %s",
                     channel.id, self.env.uid)

    # --- 3. Finish ---

    @api.model
    def _finish(self):
        """Close the migration before im_livechat_ai is uninstalled."""
        cr = self.env.cr
        Channel = self.env['im_livechat.channel'].sudo().with_context(active_test=False)
        problems = []
        installed = self._legacy_installed()
        if installed and self._legacy_ready()[0]:
            for row in self._legacy_rows():
                channel = Channel.browse(row['id'])
                if channel.woow_ai_migration_state not in ('switched', 'finalized'):
                    problems.append(self.env._('Channel "%(name)s" is not switched yet.', name=row['name']))
        staged = Channel.search([('woow_ai_migration_state', '=', 'staged')])
        for channel in staged:
            message = self.env._('Channel "%(name)s" is not switched yet.', name=channel.name)
            if message not in problems:
                problems.append(message)
        if installed:
            cr.execute("SELECT name FROM im_livechat_channel WHERE ai_enabled IS TRUE ORDER BY id")
            for (name,) in cr.fetchall():
                problems.append(self.env._('im_livechat_ai still answers channel "%(name)s".',
                                           name=self._sql_name(name)))
        if sql.table_exists(cr, 'im_livechat_ai_job'):
            cr.execute("""SELECT count(*) FROM im_livechat_ai_job WHERE state IN ('pending', 'running')
                           AND create_date > (now() AT TIME ZONE 'UTC') - interval '%s minutes'""",
                       (LEGACY_JOB_MAX_AGE_MINUTES,))
            count = cr.fetchone()[0]
            if count:
                problems.append(self.env._('im_livechat_ai still has %(count)s unfinished job(s); wait a few minutes.',
                                           count=count))
        if problems:
            raise UserError('\n'.join(problems))
        switched = Channel.search([('woow_ai_migration_state', '=', 'switched')])
        switched.with_context(woow_ai_migration=True).write({'woow_ai_migration_state': 'finalized'})
        self._take_legacy_xmlids()
        params = self.env['ir.config_parameter'].sudo().search([('key', 'in', list(LEGACY_ICP_KEYS))])
        removed = sorted(params.mapped('key'))
        params.unlink()
        _logger.warning("woow_ai_contact_livechat: im_livechat_ai migration finished by user %s (%s channel(s); "
                        "system parameters removed: %s). im_livechat_ai can be uninstalled now.",
                        self.env.uid, len(switched), ', '.join(removed) or '-')
        return {'channels': len(switched), 'parameters': removed}

    @staticmethod
    def _sql_name(name):
        if isinstance(name, dict):  # translatable name stored as jsonb
            return name.get('en_US') or next(iter(name.values()), '')
        return name or ''

    @api.model
    def _take_legacy_xmlids(self):
        """The shared 1.1.0 bot (and its tag) must survive the uninstall of
        im_livechat_ai: their xmlids move to this module (noupdate, never
        deleted by an update of this module)."""
        cr = self.env.cr
        for old, new in XMLIDS:
            cr.execute("SELECT 1 FROM ir_model_data WHERE module = %s AND name = %s", (MODULE, new))
            if cr.fetchone():
                continue
            cr.execute("UPDATE ir_model_data SET module = %s, name = %s, noupdate = TRUE "
                       "WHERE module = %s AND name = %s", (MODULE, new, LEGACY_MODULE, old))
        self.env['ir.model.data'].invalidate_model()
        self.env.registry.clear_cache()

    @api.model
    def _release_legacy_xmlids(self):
        """Uninstall of this module: keep the old bot (records under __export__
        belong to no module)."""
        cr = self.env.cr
        for _old, new in XMLIDS:
            cr.execute("UPDATE ir_model_data SET module = '__export__', name = %s, noupdate = TRUE "
                       "WHERE module = %s AND name = %s", ('%s_%s' % (MODULE, new), MODULE, new))
        self.env['ir.model.data'].invalidate_model()
        self.env.registry.clear_cache()
