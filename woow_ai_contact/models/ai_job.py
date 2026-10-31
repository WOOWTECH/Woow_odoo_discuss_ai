# -*- coding: utf-8 -*-
"""Durable AI reply jobs for any discuss.channel (generalised from im_livechat_ai 1.1.0).

Flow:
  user message_post
    -> discuss.channel._notify_thread creates a woow.ai.job in the SAME
       transaction as the message (state pending)
    -> after COMMIT (cr.postcommit) a worker thread starts, waits the
       debounce delay and runs the job with its own cursor (not a daemon
       thread: a prefork worker that recycles itself (memory or request
       limit) waits for the reply before it exits; the recovery cron is
       triggered too, for the case the process dies). While it waits, the
       recycling worker no longer pings the master, which kills it after
       limit_time_real: limit_time_real must be larger than timeout_seconds
       plus the file downloads, or a long reply is cut and run again by the
       recovery cron (a warning is logged, see _warn_worker_limits)
    -> the job runs under a per-channel PostgreSQL advisory lock, so only
       one job per discuss.channel is processed at a time, across all
       workers and the cron process
    -> before calling the backend and again before posting, a job that has a
       newer job in the same channel is marked skipped; the newest job
       answers with the full conversation as context. A job whose channel is
       no longer answered by its AI contact (archived, AI turned off, contact
       changed) is skipped too. The checks before posting run with the
       channel row locked
    -> while the backend answers, the bot member shows "typing" (refreshed
       every TYPING_REFRESH_SECONDS), switched off when the reply is posted
    -> when the lock holder finishes it runs the newest pending job of the
       channel (the jobs whose own thread found the lock busy)
  A cron (every 2 minutes) hands jobs left pending/running after a crash or
  restart to recovery threads (one per channel), at most MAX_JOB_RUNS runs
  per job, older than JOB_MAX_AGE_MINUTES given up, and never answers a job
  whose trigger message was already in the context of a finished reply.

Bridges (Live Chat) plug in through the discuss.channel ``_woow_ai_*`` hooks
and by overriding ``_process_handoff`` / ``_notify_failure`` here.
"""

import logging
import threading
import time
import zlib
from datetime import timedelta

from psycopg2.extensions import ISOLATION_LEVEL_READ_COMMITTED

from odoo import api, fields, models, SUPERUSER_ID
from odoo.modules.registry import Registry
from odoo.tools import config

_logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 1.5
MAX_JOB_RUNS = 3
STALE_PENDING_SECONDS = 30
STALE_RUNNING_SECONDS = 60
JOB_MAX_AGE_MINUTES = 30
CRON_SEARCH_LIMIT = 200
CRON_BATCH_LIMIT = 10
CHAIN_LIMIT = 20
MAX_JOB_ERROR_LENGTH = 300
CHANNEL_ROW_LOCK_TIMEOUT = '15s'
CRON_TRIGGER_DELAY_SECONDS = STALE_PENDING_SECONDS + 5
TYPING_REFRESH_SECONDS = 20

LOCK_NAMESPACE = zlib.crc32(b'woow_ai_contact.job') & 0x7FFFFFFF


# margin for downloads and posting on top of timeout_seconds
WORKER_LIMIT_MARGIN_SECONDS = 60
_worker_limit_warned = set()


def _warn_worker_limits(contact_id, timeout_seconds):
    """Log once per AI contact and process when a prefork worker that recycles
    itself during a reply would be killed before the reply ends."""
    limit = config.get('limit_time_real') or 0
    if not config.get('workers') or limit <= 0 or contact_id in _worker_limit_warned:
        return False
    if limit >= (timeout_seconds or 0) + WORKER_LIMIT_MARGIN_SECONDS:
        return False
    _worker_limit_warned.add(contact_id)
    _logger.warning(
        "AI contact %s: timeout %s s is too close to limit_time_real %s s: a worker that recycles itself "
        "during a reply is killed by the watchdog and the reply runs again. Raise limit_time_real above "
        "%s s or lower the timeout.", contact_id, timeout_seconds, limit,
        (timeout_seconds or 0) + WORKER_LIMIT_MARGIN_SECONDS)
    return True


def _job_cursor(db_name):
    """New cursor for job processing, READ COMMITTED (see im_livechat_ai 1.1.0):
    no serialization failures when the user's request updates the same
    channel/member rows, and every statement sees what other workers committed."""
    cr = Registry(db_name).cursor()
    cr.connection.set_isolation_level(ISOLATION_LEVEL_READ_COMMITTED)
    return cr


def _job_worker(db_name, job_id, wait=True):
    """Run a job and then the channel's newest pending job, in this thread. Never raises."""
    threading.current_thread().dbname = db_name
    first = True
    try:
        for _i in range(CHAIN_LIMIT):
            if wait:
                if first:
                    delay = DEBOUNCE_SECONDS
                else:
                    with _job_cursor(db_name) as cr:
                        env = api.Environment(cr, SUPERUSER_ID, {})
                        delay = env['woow.ai.job']._seconds_until_due(job_id)
                if delay > 0:
                    time.sleep(min(delay, DEBOUNCE_SECONDS))
            with _job_cursor(db_name) as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                Job = env['woow.ai.job']
                channel_id, ran = Job.browse(job_id)._run_job(commit=True)
                if not ran:
                    return
                next_id = Job._next_pending_job_id(channel_id)
            if not next_id or next_id == job_id:
                return
            job_id, first, wait = next_id, False, True
    except Exception:  # noqa: BLE001 - a background thread must never die loudly
        _logger.exception("AI contact job worker failed (job %s)", job_id)


def _recovery_worker(db_name, job_ids):
    for job_id in job_ids:
        _job_worker(db_name, job_id, wait=False)


class _TypingRefresher(threading.Thread):
    """Re-sends "typing" for the bot member every TYPING_REFRESH_SECONDS while
    the backend answers (the client clears it after about a minute)."""

    def __init__(self, db_name, channel_id, partner_id):
        super().__init__(name='woow_ai_contact.typing.%s' % channel_id, daemon=True)
        self.db_name, self.channel_id, self.partner_id = db_name, channel_id, partner_id
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.wait(TYPING_REFRESH_SECONDS):
            try:
                with _job_cursor(self.db_name) as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    member = env['discuss.channel.member'].search([
                        ('channel_id', '=', self.channel_id), ('partner_id', '=', self.partner_id)], limit=1)
                    if member and not self.stop_event.is_set():
                        member._notify_typing(True)
            except Exception:  # noqa: BLE001
                _logger.warning("AI contact: typing refresh failed for channel %s", self.channel_id, exc_info=True)


class WoowAiJob(models.Model):
    """One AI reply to produce for one message (the reply queue)."""
    _name = 'woow.ai.job'
    _description = 'AI Contact Reply Job'
    _order = 'id desc'

    channel_id = fields.Many2one('discuss.channel', string='Conversation', required=True,
                                 ondelete='cascade', index=True, readonly=True)
    contact_id = fields.Many2one('woow.ai.contact', string='AI Contact', required=True,
                                 ondelete='cascade', index=True, readonly=True)
    kind = fields.Selection([('reply', 'AI Reply'), ('handoff', 'Human Handoff')], string='Type',
                            default='reply', required=True, readonly=True)
    message_id = fields.Many2one('mail.message', string='Trigger Message', ondelete='set null', readonly=True)
    reply_message_id = fields.Many2one('mail.message', string='Reply Message', ondelete='set null', readonly=True)
    state = fields.Selection([
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('failed', 'Failed'),
        ('skipped', 'Skipped'),
    ], string='Status', default='pending', required=True, index=True, readonly=True)
    run_count = fields.Integer(string='Runs', default=0, readonly=True,
                               help='How many times processing of this job started (at most 3)')
    run_after = fields.Datetime(string='Run After', readonly=True)
    started_at = fields.Datetime(string='Started', readonly=True)
    finished_at = fields.Datetime(string='Finished', readonly=True)
    error = fields.Char(string='Error', readonly=True)
    context_message_id = fields.Integer(string='Last Message In Context', readonly=True,
                                        help='Highest message id sent to the backend as conversation')

    @api.depends('channel_id')
    def _compute_display_name(self):
        for job in self:
            job.display_name = '%s #%s' % (job.channel_id.display_name or '?', job.id)

    # --- Creation (user transaction) ---

    @api.model
    def _create_for_message(self, channel, contact, message, kind='reply'):
        job = self.sudo().create({
            'channel_id': channel.id,
            'contact_id': contact.id,
            'message_id': message.id,
            'kind': kind,
            'run_after': fields.Datetime.now() + timedelta(seconds=DEBOUNCE_SECONDS),
        })
        job._start_after_commit()
        cron = self.env.ref('woow_ai_contact.ir_cron_recover_ai_jobs', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger(job.run_after + timedelta(seconds=CRON_TRIGGER_DELAY_SECONDS))
        return job

    def _start_after_commit(self):
        db_name = self.env.cr.dbname
        job_ids = list(self.ids)

        def start_ai_job_threads():
            for job_id in job_ids:
                try:
                    threading.Thread(target=_job_worker, args=(db_name, job_id),
                                     name='woow_ai_contact.job.%s' % job_id, daemon=False).start()
                except Exception:  # noqa: BLE001 - the cron recovers the job
                    _logger.exception("Could not start AI contact job %s", job_id)

        start_ai_job_threads.woow_ai_job_ids = job_ids
        self.env.cr.postcommit.add(start_ai_job_threads)

    # --- Per-channel lock ---

    @api.model
    def _try_lock_channel(self, channel_id):
        self.env.cr.execute("SELECT pg_try_advisory_lock(%s, %s)", (LOCK_NAMESPACE, channel_id))
        return self.env.cr.fetchone()[0]

    @api.model
    def _unlock_channel(self, channel_id):
        self.env.cr.execute("SELECT pg_advisory_unlock(%s, %s)", (LOCK_NAMESPACE, channel_id))

    @api.model
    def _channel_lock_held(self, channel_id):
        self.env.cr.execute("""
            SELECT 1 FROM pg_locks
             WHERE locktype = 'advisory' AND granted
               AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
               AND classid = %s AND objid = %s AND objsubid = 2
             LIMIT 1""", (LOCK_NAMESPACE, channel_id))
        return bool(self.env.cr.fetchone())

    def _lock_channel_row(self):
        """Lock the channel row until the next commit, before the final checks
        and the post (a concurrent post that already updated the row is waited
        for, so the checks below see its job)."""
        cr = self.env.cr
        cr.execute("SET LOCAL lock_timeout = %s", (CHANNEL_ROW_LOCK_TIMEOUT,))
        cr.execute("SELECT id FROM discuss_channel WHERE id = %s FOR NO KEY UPDATE", (self.channel_id.id,))
        cr.execute("SET LOCAL lock_timeout = DEFAULT")

    # --- Processing ---

    @api.model
    def _seconds_until_due(self, job_id):
        job = self.browse(job_id).exists()
        if not job or not job.run_after:
            return 0
        return (job.run_after - fields.Datetime.now()).total_seconds()

    @api.model
    def _next_pending_job_id(self, channel_id):
        return self.search([('channel_id', '=', channel_id), ('state', '=', 'pending')],
                           order='id desc', limit=1).id

    def _run_job(self, commit=True):
        """Process this job if no other job of its channel is being processed.

        Args:
            commit (bool): commit after each step (worker thread / cron, on a
                cursor from _job_cursor). False in tests: steps only flush.

        Returns:
            tuple: (discuss.channel id or None, whether the lock was obtained)
        """
        self.ensure_one()
        cr = self.env.cr
        cr.execute("SELECT channel_id FROM woow_ai_job WHERE id = %s", (self.id,))
        row = cr.fetchone()
        if not row:
            return None, False
        channel_id = row[0]
        if not self._try_lock_channel(channel_id):
            _logger.info("AI job %s: channel %s is busy, job left pending", self.id, channel_id)
            return channel_id, False

        def commit_step():
            if commit:
                cr.commit()
            else:
                self.env.flush_all()
            self.env.invalidate_all()

        try:
            self.env.invalidate_all()
            self._process_locked(commit_step, commit=commit)
        except Exception as e:
            if not commit:
                raise
            _logger.exception("AI job %s failed unexpectedly; the cron will retry it", self.id)
            cr.rollback()
            self.env.invalidate_all()
            try:
                if self.exists() and self.state in ('pending', 'running'):
                    self.write({'error': ('%s: %s' % (type(e).__name__, e))[:MAX_JOB_ERROR_LENGTH]})
                    cr.commit()
            except Exception:  # noqa: BLE001
                cr.rollback()
        finally:
            try:
                self._unlock_channel(channel_id)
                if commit:
                    cr.commit()
            except Exception:  # noqa: BLE001 - never return a pooled connection holding the lock
                _logger.exception("AI job %s: could not release the channel lock", self.id)
                cr.rollback()
                cr.execute("SELECT pg_advisory_unlock_all()")
        return channel_id, True

    def _process_locked(self, commit_step, commit=True):
        """Body of a job run; the caller holds the channel lock."""
        self.ensure_one()
        if not self.exists() or self.state not in ('pending', 'running'):
            return
        now = fields.Datetime.now()
        if self.create_date and self.create_date < now - timedelta(minutes=JOB_MAX_AGE_MINUTES):
            self._finish('failed', 'not processed within %s minutes, given up' % JOB_MAX_AGE_MINUTES)
            self._notify_failure('job too old', visitor_told=False)
            commit_step()
            return
        if self.run_count >= MAX_JOB_RUNS:
            self._finish_exhausted(commit_step)
            return

        # Claim: the run counter is durable before any backend call.
        self.write({'state': 'running', 'run_count': self.run_count + 1, 'started_at': now, 'error': False})
        commit_step()

        if self.kind == 'handoff':
            self._process_handoff(commit_step)
            return
        if self._skip_reply(commit_step):
            return

        contact = self.contact_id.sudo()
        channel = self.channel_id.sudo()
        context = channel._woow_ai_context_messages(contact)
        history = channel._woow_ai_history(contact, context)
        if not any(entry['role'] == 'user' for entry in history):
            self._finish('skipped', 'no user message in the conversation')
            commit_step()
            return
        self.write({'context_message_id': max(context.ids)})

        _warn_worker_limits(contact.id, contact.timeout_seconds)
        self._set_typing(True)
        commit_step()
        refresher = None
        if commit:
            refresher = _TypingRefresher(self.env.cr.dbname, channel.id, contact.partner_id.id)
            refresher.start()
        try:
            result = contact._call_backend(channel, history, job=self, before_call=commit_step)
            prepared = []
            if not result['error'] and not result['skip']:
                # downloads happen here: no row lock held, nothing written yet
                prepared = [contact._prepare_reply(reply, channel=channel) for reply in result['replies']]
        finally:
            if refresher:
                refresher.stop_event.set()
        warnings = [w for p in prepared for w in p.get('warnings') or []]
        if warnings and result.get('call_log'):
            log = result['call_log']
            log.write({'error': '; '.join(filter(None, [log.error] + warnings))[:MAX_JOB_ERROR_LENGTH]})
        commit_step()  # the call log

        self._lock_channel_row()
        self._set_typing(False)
        if self._has_newer_job():
            self._finish('skipped', 'a newer message arrived; the newest job answers')
            commit_step()
            return
        if not self._contact_still_answers():
            self._finish('skipped', 'AI turned off or contact changed while the backend answered')
            commit_step()
            return
        if channel._woow_ai_is_paused():
            self._finish('skipped', 'the AI was paused while the backend answered')
            self._supersede_older()
            commit_step()
            return
        if result['error']:
            self._give_up(result['error'])
        elif result['skip']:
            self._finish('done', 'the backend chose not to reply')
        else:
            messages = contact._post_replies(channel, prepared)
            if messages:
                self._finish('done', False, reply_message=messages.sorted('id')[-1:])
            else:
                self._give_up('empty reply')
        self._supersede_older()
        commit_step()

    def _set_typing(self, is_typing):
        self.ensure_one()
        try:
            member = self.env['discuss.channel.member'].sudo().search([
                ('channel_id', '=', self.channel_id.id),
                ('partner_id', '=', self.contact_id.sudo().partner_id.id),
            ], limit=1)
            if member:
                member._notify_typing(is_typing)
        except Exception:  # noqa: BLE001 - typing is cosmetic
            _logger.warning("AI job %s: could not send typing=%s", self.id, is_typing, exc_info=True)

    def _skip_reply(self, commit_step):
        """Finish a reply job without calling the backend when it must not answer."""
        contact = self.contact_id.sudo()
        if not contact.active:
            self._finish('skipped', 'AI contact archived')
        elif not self._contact_still_answers():
            self._finish('skipped', 'AI turned off or contact changed')
        elif self._has_newer_job():
            self._finish('skipped', 'a newer message arrived; the newest job answers')
        elif self._already_answered():
            self._finish('done', 'already answered by a previous reply')
            self._supersede_older()
        elif self.channel_id._woow_ai_is_paused():
            self._finish('skipped', 'the AI is paused for this conversation')
            self._supersede_older()
        else:
            return False
        commit_step()
        return True

    def _contact_still_answers(self):
        """True while the job's AI contact is still the one answering its channel."""
        self.ensure_one()
        return self.channel_id.sudo()._woow_ai_channel_contact() == self.contact_id.sudo()

    def _finish_exhausted(self, commit_step):
        if self.kind == 'handoff':
            self._process_handoff(commit_step)
            return
        if self._skip_reply(commit_step):
            return
        self._give_up('tried %s times without finishing' % self.run_count)
        commit_step()

    def _process_handoff(self, commit_step):
        """Hook for bridges (Live Chat: post the handoff notice, alert operators)."""
        self._finish('skipped', 'handoff is not supported in this channel')
        commit_step()

    def _finish(self, state, error, reply_message=None):
        vals = {
            'state': state,
            'finished_at': fields.Datetime.now(),
            'error': (error or '')[:MAX_JOB_ERROR_LENGTH] or False,
        }
        if reply_message:
            vals['reply_message_id'] = reply_message.id
        self.write(vals)

    def _error_text(self):
        contact = self.contact_id.sudo()
        if (contact.error_message or '').strip():
            return contact.error_message.strip()
        identity = self.channel_id.sudo()._woow_ai_identity(contact)
        lang = identity.get('lang') or self.env.lang
        return self.with_context(lang=lang).env._(
            'Sorry, I cannot answer right now. Please try again later.')

    def _give_up(self, error):
        """Final failure: the user gets the error message."""
        contact = self.contact_id.sudo()
        message = contact._post_text(self.channel_id.sudo(), self._error_text())
        self._finish('failed', error, reply_message=message or None)
        self._notify_failure(error, visitor_told=True)

    def _notify_failure(self, reason, visitor_told=True):
        """Hook for bridges (Live Chat alerts the operators). Core: log only.

        Args:
            reason (str): short technical reason
            visitor_told (bool): True when the error message was posted in the
                conversation (``_give_up``); False when the job was dropped
                silently (too old)
        """
        _logger.warning("AI job %s (channel %s) failed (user told: %s): %s",
                        self.id, self.channel_id.id, visitor_told, reason)

    def _has_newer_job(self):
        return bool(self.search_count([('channel_id', '=', self.channel_id.id), ('id', '>', self.id)], limit=1))

    def _already_answered(self):
        if not self.message_id:
            return False
        return bool(self.search_count([
            ('channel_id', '=', self.channel_id.id),
            ('id', '!=', self.id),
            ('kind', '=', 'reply'),
            ('state', '=', 'done'),
            ('reply_message_id', '!=', False),
            ('context_message_id', '>=', self.message_id.id),
        ], limit=1))

    def _supersede_older(self):
        older = self.search([
            ('channel_id', '=', self.channel_id.id),
            ('id', '<', self.id),
            ('state', 'in', ('pending', 'running')),
        ])
        if older:
            older.write({'state': 'skipped', 'finished_at': fields.Datetime.now(),
                         'error': 'answered by a newer job'})

    # --- Recovery cron ---

    @api.model
    def _recovery_domain(self):
        now = fields.Datetime.now()
        return [
            '|',
            '&', ('state', '=', 'pending'), ('run_after', '<', now - timedelta(seconds=STALE_PENDING_SECONDS)),
            '&', ('state', '=', 'running'), ('started_at', '<', now - timedelta(seconds=STALE_RUNNING_SECONDS)),
        ]

    @api.model
    def _cron_recover_jobs(self, commit=True):
        """Run jobs whose thread was lost (restart, crash, worker recycled).

        Channels whose lock is held (a job is being processed) are left alone.
        The work is handed to threads (a reply can take longer than the cron
        watchdog). Returns the number of jobs handed over.
        """
        jobs = self.search(self._recovery_domain(), order='id', limit=CRON_SEARCH_LIMIT)
        if not jobs:
            return 0
        by_channel = {}
        for job in jobs:
            by_channel.setdefault(job.channel_id.id, []).append(job)
        batch = []
        for channel_id, channel_jobs in by_channel.items():
            if self._channel_lock_held(channel_id):
                continue
            batch.append(channel_jobs)
            if len(batch) >= CRON_BATCH_LIMIT:
                break
        for channel_jobs in batch:
            job_ids = [job.id for job in channel_jobs]
            _logger.info("Recovering AI contact jobs %s", job_ids)
            if commit:
                threading.Thread(target=_recovery_worker, args=(self.env.cr.dbname, job_ids),
                                 name='woow_ai_contact.recover.%s' % job_ids[0], daemon=False).start()
            else:
                for job_id in job_ids:
                    self._run_chain_in_transaction(job_id)
        return sum(len(channel_jobs) for channel_jobs in batch)

    @api.model
    def _run_chain_in_transaction(self, job_id):
        """Test counterpart of _job_worker(wait=False): same chaining, no commit."""
        for _i in range(CHAIN_LIMIT):
            channel_id, ran = self.browse(job_id)._run_job(commit=False)
            if not ran:
                return
            next_id = self._next_pending_job_id(channel_id)
            if not next_id or next_id == job_id:
                return
            job_id = next_id

    @api.model
    def _cleanup_old_jobs(self, days=30):
        cutoff = fields.Datetime.now() - timedelta(days=days)
        old = self.search([('state', 'in', ('done', 'failed', 'skipped')), ('create_date', '<', cutoff)])
        count = len(old)
        if count:
            old.unlink()
        return count
