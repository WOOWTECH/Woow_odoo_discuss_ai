# -*- coding: utf-8 -*-
"""Durable AI reply jobs for any discuss.channel (generalised from im_livechat_ai 1.1.0).

Flow:
  user message_post
    -> discuss.channel._notify_thread creates a woow.ai.job in the SAME
       transaction as the message (state pending, due after the debounce)
    -> the HTTP request only triggers the cron "AI Contacts: Run AI Replies"
       (ir.cron._trigger: a NOTIFY after commit wakes a cron worker at once);
       no thread is started in the web process
    -> the cron callback is a dispatcher (tools/dispatcher.py): it runs the
       due jobs on a bounded pool of threads INSIDE the cron process (one
       thread per conversation, ``woow_ai_contact.max_concurrency``, default
       4, each with its own cursor; shared fairly between AI contacts),
       keeps fetching newly due jobs while replies run (every
       DISPATCH_POLL_SECONDS, one query), stops starting new ones (also
       inside a conversation's chain) when its claiming window is used
       (derived from the cron time limit minus what the pass used, the
       longest AI contact timeout and a margin; short on a shared cron
       worker) or the server is stopping, JOINS its threads and triggers
       itself again when work is left. A cron worker only recycles between
       passes, so no reply is ever cut by a recycling web worker any more.
       The cron process reports its server type and limits (STATE_PARAM),
       so web processes without cron threads pick the mode from them
    -> fallback ("thread" mode, the former design): when no cron runner can
       take the work (max_cron_threads = 0 and no dispatcher pass seen
       recently, --stop-after-init, tests) or the cron time limit is too short
       for the longest timeout, the job runs in a postcommit thread of the
       process that created it (see _job_runner; the mode is logged once and
       shown on the AI contact form)
    -> the job runs under a per-channel PostgreSQL advisory lock, so only
       one job per discuss.channel is processed at a time, across all
       processes; a conversation whose lock is busy is left alone and retried
    -> before calling the backend and again before posting, a job that has a
       newer job in the same channel is marked skipped; the newest job
       answers with the full conversation as context. A job whose channel is
       no longer answered by its AI contact (archived, AI turned off, contact
       changed) is skipped too. The checks before posting run with the
       channel row locked
    -> while the backend answers, the bot member shows "typing" (refreshed
       every TYPING_REFRESH_SECONDS), switched off when the reply is posted
  The same cron (every minute) re-queues jobs left pending/running after a
  crash or restart (a 'running' job whose channel lock is free has lost its
  owner), at most MAX_JOB_RUNS runs per job, older than JOB_MAX_AGE_MINUTES
  given up, and never answers a job whose trigger message was already in the
  context of a finished reply. A job can postpone itself (``_defer``, e.g.
  the Live Chat bridge while im_livechat_ai is still answering): the cron is
  triggered at its retry time.

Bridges (Live Chat) plug in through the discuss.channel ``_woow_ai_*`` hooks
and by overriding ``_process_handoff`` / ``_notify_failure`` here.
"""

import json
import logging
import os
import signal
import threading
import time
import zlib
from collections import Counter, deque
from datetime import timedelta

from psycopg2.extensions import ISOLATION_LEVEL_READ_COMMITTED

from odoo import api, fields, models, SUPERUSER_ID
from odoo.modules.registry import Registry
from odoo.tools import config

from ..tools.dispatcher import BUSY, FAILED, Dispatcher

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
FAILED_RUN_RETRY_SECONDS = 60  # a run that crashed before finishing is retried after this

LOCK_NAMESPACE = zlib.crc32(b'woow_ai_contact.job') & 0x7FFFFFFF

DISPATCH_CRON_XMLID = 'woow_ai_contact.ir_cron_run_ai_jobs'
RUNNER_PARAM = 'woow_ai_contact.job_runner'            # auto (default) | cron | thread
CONCURRENCY_PARAM = 'woow_ai_contact.max_concurrency'  # default 4
DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 16
DISPATCH_POLL_SECONDS = 2.0     # fetch interval while replies run
DISPATCH_IDLE_WAIT_SECONDS = 20.0  # stay in the pass for a job due this soon (debounce, deferred job)
DISPATCH_BUSY_RETRY_SECONDS = 5.0  # a unit that found its conversation taken (race) is retried after this
DISPATCH_MAX_BACKOFF_SECONDS = 60.0  # a unit that keeps failing is retried at most this late (doubling)
MAX_DISPATCH_SECONDS = 300      # longest claiming window of one pass (other crons get their turn)
# shared cron worker (max_cron_threads <= 1, or one cron process for several
# databases): other crons and databases wait for a whole pass, so it claims
# for a short window and does not wait long for jobs due later
SHARED_DISPATCH_SECONDS = 60
SHARED_IDLE_WAIT_SECONDS = 5.0
# margin for downloads, posting and the claim on top of timeout_seconds
WORKER_LIMIT_MARGIN_SECONDS = 60
# prefork: a WorkerCron pings the watchdog BEFORE its idle sleep (SLEEP_INTERVAL
# + pid % 10 seconds, odoo/service/server.py), so up to that much of
# limit_time_real_cron is already used when a pass starts
try:
    from odoo.service.server import SLEEP_INTERVAL as _ODOO_CRON_SLEEP
except ImportError:  # pragma: no cover
    _ODOO_CRON_SLEEP = 60
PREFORK_PASS_RESERVE_SECONDS = _ODOO_CRON_SLEEP + 10
RUNNER_CACHE_SECONDS = 60
# the cron process reports its view (server type, cron time limit, cron
# threads, databases) here at the start of every pass and every
# STATE_REFRESH_SECONDS during a pass (raw SQL: no ormcache invalidation);
# a report younger than HEARTBEAT_MINUTES = a cron runner exists in another
# process, and its view (not this process' config) decides the mode
STATE_PARAM = 'woow_ai_contact.dispatcher_state'
STATE_REFRESH_SECONDS = 60
HEARTBEAT_MINUTES = 10
DB_COUNT_CACHE_SECONDS = 300

_runner_cache = {}   # db name -> (expires monotonic, info)
_runner_logged = {}  # db name -> (mode, reason) last logged
_state_written = {}  # db name -> monotonic time of this process' last report
_db_count_cache = {}  # 'count' -> (expires monotonic, count)

# --- prefork: when did this cron worker last ping its watchdog? ---
# The master kills a WorkerCron limit_time_real_cron seconds after its last
# ping; the ping happens before the idle sleep, and every cron of the same
# database that ran before the dispatcher in this process_work counts too.
# PreforkServer.pipe_ping is wrapped (a pass-through) to note the time.
_MODULE_LOADED_AT = (os.getpid(), time.monotonic())
_watchdog_ping = {'pid': None, 'at': None, 'probe': False}


def _install_watchdog_probe():
    try:
        from odoo.service import server as odoo_server  # noqa: PLC0415
        cls = odoo_server.PreforkServer
        original = cls.pipe_ping
    except Exception:  # noqa: BLE001 - no probe: the fixed reserve is used
        return
    if getattr(original, '_woow_ai_probe', False):
        _watchdog_ping['probe'] = True
        return

    def pipe_ping(self, pipe):
        _watchdog_ping['pid'] = os.getpid()
        _watchdog_ping['at'] = time.monotonic()
        return original(self, pipe)

    pipe_ping._woow_ai_probe = True
    cls.pipe_ping = pipe_ping
    _watchdog_ping['probe'] = True


_install_watchdog_probe()


def _prefork_pass_used():
    """Seconds of limit_time_real_cron this prefork cron worker already used."""
    pid, now = os.getpid(), time.monotonic()
    if _watchdog_ping['pid'] == pid and _watchdog_ping['at'] is not None:
        return max(0.0, now - _watchdog_ping['at'])
    used = float(PREFORK_PASS_RESERVE_SECONDS)
    if _watchdog_ping['probe'] and _MODULE_LOADED_AT[0] == pid:
        # no ping since this module was loaded in this worker (first pass
        # after the registry load): everything since the load counts too
        used += max(0.0, now - _MODULE_LOADED_AT[1])
    return used


def _server_stopping():
    """True when the server of this process received a stop signal: threaded
    (ThreadedServer.quit_signals_received) or a prefork worker (SIGINT from
    the master: Worker.alive is False). The SIGINT handler is a bound method
    of that server/worker object."""
    try:
        owner = getattr(signal.getsignal(signal.SIGINT), '__self__', None)
    except Exception:  # noqa: BLE001
        return False
    if owner is None:
        return False
    received = getattr(owner, 'quit_signals_received', None)
    if isinstance(received, int):
        return received > 0
    alive = getattr(owner, 'alive', None)
    if isinstance(alive, bool):
        return not alive
    return False


def _served_db_count():
    """Databases the cron runner of this process walks through (one pass at
    a time per cron worker): --database list, else the loaded registries
    (threaded cron threads) or every database (prefork WorkerCron)."""
    names = [name for name in (config.get('db_name') or '').split(',') if name.strip()]
    if names:
        return len(names)
    if not config.get('workers'):
        try:
            return max(1, len(Registry.registries.d))
        except Exception:  # noqa: BLE001
            return 1
    cached = _db_count_cache.get('count')
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    try:
        from odoo.service import db as odoo_db  # noqa: PLC0415
        count = max(1, len(odoo_db.list_dbs(True)))
    except Exception:  # noqa: BLE001
        count = 1
    _db_count_cache['count'] = (now + DB_COUNT_CACHE_SECONDS, count)
    return count


def _local_runner_view():
    """This process as a cron runner: server type, cron time limit, cron
    threads, databases served."""
    threaded = not config.get('workers')
    return {'server': 'threaded' if threaded else 'prefork', 'cron_limit': _cron_limit(threaded),
            'cron_threads': int(config.get('max_cron_threads') or 0), 'dbs': _served_db_count()}


def _report_runner_state(cr):
    """Cron process: publish its view for the web processes (see STATE_PARAM).
    Never raises (a failed report must not fail the cron pass)."""
    try:
        with cr.savepoint(flush=False):
            cr.execute("""
                INSERT INTO ir_config_parameter (key, value, create_uid, write_uid, create_date, write_date)
                VALUES (%s, %s, %s, %s, statement_timestamp() AT TIME ZONE 'UTC',
                        statement_timestamp() AT TIME ZONE 'UTC')
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_date = EXCLUDED.write_date
            """, (STATE_PARAM, json.dumps(_local_runner_view(), sort_keys=True), SUPERUSER_ID, SUPERUSER_ID))
    except Exception:  # noqa: BLE001
        _logger.warning("AI contacts: could not report the cron runner state", exc_info=True)
        return
    _state_written[cr.dbname] = time.monotonic()


def _remote_runner_view(cr):
    """The view reported by a cron process in the last HEARTBEAT_MINUTES, or None."""
    cr.execute("""
        SELECT value FROM ir_config_parameter
         WHERE key = %s AND write_date > (statement_timestamp() AT TIME ZONE 'UTC') - make_interval(mins => %s)
    """, (STATE_PARAM, HEARTBEAT_MINUTES))
    row = cr.fetchone()
    if not row:
        return None
    try:
        view = json.loads(row[0])
        return {'server': 'threaded' if view.get('server') == 'threaded' else 'prefork',
                'cron_limit': max(0, int(view.get('cron_limit') or 0)),
                'cron_threads': max(0, int(view.get('cron_threads') or 0)),
                'dbs': max(1, int(view.get('dbs') or 1))}
    except (TypeError, ValueError, AttributeError):
        return None


def _job_cursor(db_name):
    """New cursor for job processing, READ COMMITTED (see im_livechat_ai 1.1.0):
    no serialization failures when the user's request updates the same
    channel/member rows, and every statement sees what other workers committed."""
    cr = Registry(db_name).cursor()
    cr.connection.set_isolation_level(ISOLATION_LEVEL_READ_COMMITTED)
    return cr


def _cron_limit(threaded=None):
    """Real-time limit (seconds, 0 = none) Odoo applies to a cron pass, as in
    odoo/service/server.py: prefork WorkerCron watchdog = limit_time_real_cron
    (-1: limit_time_real, 0: none); threaded cron threads (process_limit) use
    limit_time_real_cron when > 0, else limit_time_real."""
    if threaded is None:
        threaded = not config.get('workers')
    real = config.get('limit_time_real') or 0
    cron = config.get('limit_time_real_cron')
    if cron is None:
        cron = -1
    if threaded:
        limit = cron if cron and cron > 0 else real
    else:
        limit = real if cron == -1 else cron
    return max(0, int(limit or 0))


def _pool_free_connections():
    """Connections still free in this process' pool (odoo.sql_db), or None."""
    try:
        from odoo import sql_db  # noqa: PLC0415
        pool = sql_db._Pool
        if pool is None:
            return None
        used = sum(1 for item in list(pool._connections) if item[1])
        return pool._maxconn - used
    except Exception:  # noqa: BLE001 - private API: only a cap
        return None


def _job_worker(db_name, job_id, wait=True):
    """Run a job and then the channel's newest pending job, in this thread
    ("thread" mode and the recovery of that mode). Never raises."""
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
                    time.sleep(min(delay, DISPATCH_IDLE_WAIT_SECONDS))
            with _job_cursor(db_name) as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                Job = env['woow.ai.job']
                channel_id, ran = Job.browse(job_id)._run_job(commit=True)
                if not ran:
                    return
                next_id = Job._next_pending_job_id(channel_id)
                # a job that postponed itself (_defer) is still pending: wait for it
                deferred = next_id == job_id and Job._seconds_until_due(job_id) > 0
            if not next_id or (next_id == job_id and not deferred):
                return
            job_id, first, wait = next_id, False, True
    except Exception:  # noqa: BLE001 - a background thread must never die loudly
        _logger.exception("AI contact job worker failed (job %s)", job_id)


def _recovery_worker(db_name, job_ids):
    for job_id in job_ids:
        _job_worker(db_name, job_id, wait=False)


def _dispatch_fetch(db_name, grace, exclude, limit, slots=None):
    """Dispatcher ``fetch``: due conversations, on a short-lived cursor; also
    refreshes the runner report every STATE_REFRESH_SECONDS of a long pass."""
    with _job_cursor(db_name) as cr:
        if time.monotonic() - _state_written.get(db_name, 0.0) >= STATE_REFRESH_SECONDS:
            _report_runner_state(cr)
        env = api.Environment(cr, SUPERUSER_ID, {})
        return env['woow.ai.job']._dispatch_candidates(exclude=exclude, limit=limit, grace=grace, slots=slots)


def _channel_worker(db_name, grace, channel_id, may_continue=None):
    """Dispatcher ``run_unit``: run the due jobs of one conversation (the
    newest first: it supersedes the older ones), in this thread with its own
    cursor. A job that became due during the first one is only started while
    ``may_continue()`` is True (claiming window of the pass still open,
    server not stopping); otherwise it is left to the next pass, so a unit
    never calls the backend after the window the time budget is based on.
    Returns BUSY when another process holds the conversation."""
    threading.current_thread().dbname = db_name
    previous = None
    for i in range(CHAIN_LIMIT):
        if i and may_continue is not None and not may_continue():
            return None
        with _job_cursor(db_name) as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            Job = env['woow.ai.job']
            job_id = Job._due_job_id(channel_id, grace)
            if not job_id:
                return None
            if job_id == previous:
                # still due right after its run (it crashed): backed off, retried later
                return FAILED
            _channel, ran = Job.browse(job_id)._run_job(commit=True)
            if not ran:
                return BUSY
            previous = job_id
    return None


class _TypingHub:
    """Re-sends "typing" for the bot member of every reply in progress every
    TYPING_REFRESH_SECONDS (the client clears it after about a minute).

    One daemon thread per process for all replies, one short cursor per
    database and refresh: a running reply holds a single connection (its job
    cursor), so the dispatcher can run more conversations on a small pool.
    ``remove()`` waits for a refresh in progress, so no "typing" is sent for
    a reply after it was removed (its own typing=False comes later)."""

    def __init__(self):
        self._lock = threading.Lock()       # entries and thread
        self._send_lock = threading.Lock()  # one refresh at a time; remove() waits for it
        self._entries = {}                  # (db, channel_id, partner_id) -> next refresh (monotonic)
        self._thread = None
        self._pid = None

    def add(self, db_name, channel_id, partner_id):
        key = (db_name, channel_id, partner_id)
        with self._lock:
            self._entries[key] = time.monotonic() + TYPING_REFRESH_SECONDS
            if self._thread is None or not self._thread.is_alive() or self._pid != os.getpid():
                self._pid = os.getpid()
                self._thread = threading.Thread(target=self._run, name='woow_ai_contact.typing', daemon=True)
                self._thread.start()
        return key

    def remove(self, key):
        with self._send_lock, self._lock:
            self._entries.pop(key, None)

    def _run(self):
        while True:
            with self._lock:
                if not self._entries:
                    self._thread = None
                    return
                now = time.monotonic()
                due = [key for key, at in self._entries.items() if at <= now]
                wait = min(self._entries.values()) - now
            if due:
                self._refresh(due)
            else:
                time.sleep(min(max(wait, 0.05), TYPING_REFRESH_SECONDS))

    def _refresh(self, keys):
        by_db = {}
        for key in keys:
            by_db.setdefault(key[0], []).append(key)
        for db_name, db_keys in by_db.items():
            try:
                with self._send_lock, _job_cursor(db_name) as cr:
                    Member = api.Environment(cr, SUPERUSER_ID, {})['discuss.channel.member']
                    for key in db_keys:
                        with self._lock:
                            if key not in self._entries:
                                continue
                        member = Member.search([('channel_id', '=', key[1]), ('partner_id', '=', key[2])], limit=1)
                        if member:
                            member._notify_typing(True)
            except Exception:  # noqa: BLE001 - typing is cosmetic
                _logger.warning("AI contact: typing refresh failed (%s)", db_name, exc_info=True)
            with self._lock:
                for key in db_keys:
                    if key in self._entries:
                        self._entries[key] = time.monotonic() + TYPING_REFRESH_SECONDS


_typing_hub = _TypingHub()


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
        job._schedule_dispatch()
        return job

    def _schedule_dispatch(self, at=None):
        """Make sure these (pending) jobs get processed, after the commit.

        "cron" mode: trigger the dispatcher cron (``at`` None = now: Odoo
        sends a NOTIFY after the commit that wakes a cron worker at once; the
        dispatcher itself waits for ``run_after``). "thread" mode: start a
        thread in this process after the commit (former design) and trigger
        the cron later as recovery.
        """
        cron = self.env.ref(DISPATCH_CRON_XMLID, raise_if_not_found=False)
        if self._job_runner()['mode'] == 'cron':
            if cron:
                now = fields.Datetime.now()
                cron.sudo()._trigger(at if at and at > now else None)
            return
        if not at:
            self._start_after_commit()
        if cron:
            due = at or max(self.mapped('run_after') or [fields.Datetime.now()])
            cron.sudo()._trigger(due + timedelta(seconds=CRON_TRIGGER_DELAY_SECONDS))

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

    def _defer(self, seconds, reason):
        """Postpone this job (left pending, due in `seconds`): e.g. the Live
        Chat bridge while im_livechat_ai is still answering. The dispatcher
        stays in its pass for a short wait; the cron is triggered at the due
        time as well. The caller commits (commit_step)."""
        self.ensure_one()
        due = fields.Datetime.now() + timedelta(seconds=seconds)
        self.write({'state': 'pending', 'run_after': due, 'error': (reason or '')[:MAX_JOB_ERROR_LENGTH] or False})
        self._schedule_dispatch(at=due)
        return due

    # --- Runner mode ---

    @api.model
    def _job_runner(self, refresh=False):
        """Where replies run: {'mode': 'cron'|'thread', 'reason', 'forced',
        'server', 'cron_limit', 'required', 'max_timeout', 'cron_runner',
        'cron_threads', 'dbs', 'shared'}.

        auto (default, system parameter woow_ai_contact.job_runner): "cron"
        when a cron runner exists (max_cron_threads > 0 in this server and
        not --stop-after-init / tests, or a cron process reported in the
        last HEARTBEAT_MINUTES: crons run in another process, whose server
        type and time limit are then used) and its time limit fits the
        longest AI contact timeout; otherwise "thread". Cached per database
        for RUNNER_CACHE_SECONDS; logged once per change."""
        db_name = self.env.cr.dbname
        cached = _runner_cache.get(db_name)
        # (when crons run in another process, the mode follows the view that
        # process reports, not this process' config: see STATE_PARAM)
        now = time.monotonic()
        if cached and not refresh and cached[0] > now:
            return cached[1]
        info = self._compute_job_runner()
        _runner_cache[db_name] = (now + RUNNER_CACHE_SECONDS, info)
        if _runner_logged.get(db_name) != (info['mode'], info['reason']):
            _runner_logged[db_name] = (info['mode'], info['reason'])
            log = _logger.warning if info['reason'] in ('cron_limit_too_low', 'forced_cron_limit_too_low',
                                                         'no_cron_runner') else _logger.info
            log("AI contacts (%s): replies run in %s mode (%s; cron limit %s s, needed %s s, "
                "longest timeout %s s)", db_name, info['mode'], info['reason'], info['cron_limit'] or 'none',
                info['required'], info['max_timeout'])
        return info

    @api.model
    def _runner_cache_clear(self):
        _runner_cache.pop(self.env.cr.dbname, None)

    @api.model
    def _compute_job_runner(self):
        from odoo.modules import module as odoo_module  # noqa: PLC0415
        cr = self.env.cr
        forced = (self.env['ir.config_parameter'].sudo().get_param(RUNNER_PARAM) or 'auto').strip().lower()
        if forced not in ('cron', 'thread'):
            forced = 'auto'
        cr.execute("SELECT max(timeout_seconds) FROM woow_ai_contact WHERE active")
        max_timeout = (cr.fetchone() or [None])[0] or 300
        local = (config.get('max_cron_threads') or 0) > 0 and not config.get('stop_after_init') \
            and not getattr(odoo_module, 'current_test', False)
        remote = None
        if not local:
            # crons in another process (platform web pod: max_cron_threads = 0):
            # its time limit applies, not this process' (prefork + 70 s here,
            # threaded there, a different limit_time_real_cron, ...)
            cr.execute("""
                SELECT 1 FROM ir_cron c JOIN ir_model_data d ON d.res_id = c.id AND d.model = 'ir.cron'
                 WHERE d.module = 'woow_ai_contact' AND d.name = 'ir_cron_run_ai_jobs' AND c.active""")
            if cr.fetchone():
                remote = _remote_runner_view(cr)
        view = remote or _local_runner_view()
        runner = local or remote is not None
        limit = view['cron_limit']
        reserve = 0 if view['server'] == 'threaded' else PREFORK_PASS_RESERVE_SECONDS
        required = max_timeout + WORKER_LIMIT_MARGIN_SECONDS + reserve
        fits = not limit or limit >= required
        info = {'forced': forced != 'auto', 'server': view['server'],
                'cron_limit': limit, 'required': required, 'max_timeout': max_timeout,
                'cron_runner': 'local' if local else ('other process' if remote else 'none'),
                'cron_threads': view['cron_threads'], 'dbs': view['dbs'],
                'shared': view['cron_threads'] <= 1 or view['dbs'] > 1}
        if forced == 'thread':
            info.update(mode='thread', reason='forced_thread')
        elif forced == 'cron':
            info.update(mode='cron', reason='forced_cron' if fits else 'forced_cron_limit_too_low')
        elif not runner:
            info.update(mode='thread', reason='no_cron_runner')
        elif not fits:
            info.update(mode='thread', reason='cron_limit_too_low')
        else:
            info.update(mode='cron', reason='cron_runner')
        return info

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
                    vals = {'error': ('%s: %s' % (type(e).__name__, e))[:MAX_JOB_ERROR_LENGTH]}
                    if self.state == 'pending':
                        # not claimed yet: do not pick it again at once (a 'running'
                        # one waits STALE_RUNNING_SECONDS anyway)
                        vals['run_after'] = fields.Datetime.now() + timedelta(seconds=FAILED_RUN_RETRY_SECONDS)
                    self.write(vals)
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

        self._set_typing(True)
        commit_step()
        typing = _typing_hub.add(self.env.cr.dbname, channel.id, contact.partner_id.id) if commit else None
        try:
            result = contact._call_backend(channel, history, job=self, before_call=commit_step)
            prepared = []
            if not result['error'] and not result['skip']:
                # downloads happen here: no row lock held, nothing written yet
                prepared = [contact._prepare_reply(reply, channel=channel) for reply in result['replies']]
        finally:
            if typing:
                _typing_hub.remove(typing)
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
        try:
            lang = self.channel_id.sudo()._woow_ai_identity(contact).get('lang')
        except Exception:  # noqa: BLE001 - the error message must always be posted
            _logger.warning("AI job %s: no identity language for the error message", self.id, exc_info=True)
            lang = None
        return self.with_context(lang=contact._installed_lang(lang)).env._(
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

    # --- Dispatcher (cron "AI Contacts: Run AI Replies") ---

    @api.model
    def _dispatch_candidates(self, exclude=(), limit=1, grace=0, slots=None):
        """Conversations with a due job, for the dispatcher.

        One row per conversation: its newest pending job, or its newest job
        left 'running' for STALE_RUNNING_SECONDS (lost owner). A conversation
        is due when that job is due (``run_after`` reached, plus ``grace``
        seconds in "thread" mode, where the creating process runs fresh jobs
        itself) and nobody holds its advisory lock: a live owner (another
        dispatcher, a "thread" mode worker, the bridge) keeps it, so a job is
        never run twice at the same time. A locked conversation is NOT a
        reason to stay in the pass (its owner chains its own jobs; the
        minute cron is the backstop): ``next_due`` only counts jobs whose
        ``run_after`` is not reached yet (debounce, ``_defer``).

        The due conversations are shared out between AI contacts (`exclude`
        = the conversations running in this pass): the contact with the
        fewest running replies goes first, oldest job first within a
        contact; with ``slots`` (the pass concurrency) >= 2 and more than one
        active AI contact, one contact never takes the last free slot (a
        slow backend cannot hold every slot while another contact waits).

        Returns:
            tuple: (list of discuss.channel ids, at most `limit`; seconds
            until the next pending job is due or None)
        """
        self.flush_model()
        cr = self.env.cr
        cr.execute("""
            SELECT DISTINCT ON (channel_id) channel_id, id, state, contact_id,
                   EXTRACT(EPOCH FROM (COALESCE(run_after, create_date) - (statement_timestamp() AT TIME ZONE 'UTC')))
              FROM woow_ai_job
             WHERE state = 'pending'
                OR (state = 'running'
                    AND started_at < (statement_timestamp() AT TIME ZONE 'UTC') - make_interval(secs => %s))
             ORDER BY channel_id, id DESC
        """, (STALE_RUNNING_SECONDS,))
        exclude = set(exclude or ())
        due, next_due = [], None
        for channel_id, job_id, state, contact_id, due_in in cr.fetchall():
            if channel_id in exclude:
                continue
            due_in = float(due_in or 0) + (grace if state == 'pending' else 0)
            if state == 'running' or due_in <= 0:
                due.append((job_id, channel_id, contact_id))
            elif next_due is None or due_in < next_due:
                next_due = due_in
        if not due or limit <= 0:
            return [], next_due
        cr.execute("""
            SELECT objid FROM pg_locks
             WHERE locktype = 'advisory' AND granted
               AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
               AND classid = %s AND objsubid = 2 AND objid = ANY(%s)""",
                   (LOCK_NAMESPACE, [row[1] for row in due]))
        locked = {row[0] for row in cr.fetchall()}
        due = [row for row in due if row[1] not in locked]
        return self._dispatch_share(due, exclude, limit, slots), next_due

    @api.model
    def _dispatch_share(self, due, running, limit, slots):
        """Pick at most `limit` of the `due` rows (job id, channel id, contact
        id), fairly between AI contacts (see _dispatch_candidates)."""
        if not due:
            return []
        cr = self.env.cr
        used = Counter()
        if running:
            cr.execute("""
                SELECT DISTINCT ON (channel_id) channel_id, contact_id FROM woow_ai_job
                 WHERE channel_id = ANY(%s) ORDER BY channel_id, id DESC""", (list(running),))
            used.update(contact_id for _channel, contact_id in cr.fetchall())
        cap = None
        if slots and slots >= 2:
            cr.execute("SELECT count(*) FROM woow_ai_contact WHERE active")
            if cr.fetchone()[0] > 1:
                cap = slots - 1
        queues = {}
        for job_id, channel_id, contact_id in sorted(due):
            queues.setdefault(contact_id, deque()).append((job_id, channel_id))
        picked = []
        while len(picked) < limit:
            choices = [(used[contact], queue[0][0], contact) for contact, queue in queues.items()
                       if queue and (cap is None or used[contact] < cap)]
            if not choices:
                break
            contact = min(choices)[2]
            picked.append(queues[contact].popleft()[1])
            used[contact] += 1
        return picked

    @api.model
    def _due_job_id(self, channel_id, grace=0):
        """Newest job of `channel_id` if it is due (see _dispatch_candidates), else False."""
        self.flush_model()
        self.env.cr.execute("""
            SELECT id, state, EXTRACT(EPOCH FROM (COALESCE(run_after, create_date) - (statement_timestamp() AT TIME ZONE 'UTC')))
              FROM woow_ai_job
             WHERE channel_id = %s
               AND (state = 'pending'
                    OR (state = 'running'
                        AND started_at < (statement_timestamp() AT TIME ZONE 'UTC') - make_interval(secs => %s)))
             ORDER BY id DESC LIMIT 1
        """, (channel_id, STALE_RUNNING_SECONDS))
        row = self.env.cr.fetchone()
        if not row:
            return False
        job_id, state, due_in = row
        if state == 'pending' and float(due_in or 0) + grace > 0:
            return False
        return job_id

    @api.model
    def _dispatch_settings(self, runner):
        """Concurrency and time budget of one dispatcher pass, read once per pass.

        Budget (claiming window): the cron time limit (as Odoo applies it to
        this pass), minus what the pass already used (threaded: since the
        cron thread started this database, earlier crons included; prefork:
        since the worker's last watchdog ping, i.e. its idle sleep and the
        crons that ran before, measured by the pipe_ping probe, else a
        conservative estimate), minus the longest timeout and
        WORKER_LIMIT_MARGIN_SECONDS, at most MAX_DISPATCH_SECONDS
        (SHARED_DISPATCH_SECONDS on a shared cron worker: one cron thread, or
        several databases), at least 0 (start one batch, then stop claiming).
        A pass then lasts at most window + longest timeout + margin, since a
        unit does not chain past the window.
        """
        thread = threading.current_thread()
        threaded = getattr(thread, 'type', None) == 'cron'
        limit = _cron_limit(threaded)
        if threaded:
            start_time = getattr(thread, 'start_time', None)
            used = max(0.0, time.time() - start_time) if start_time else 0.0
        else:
            used = _prefork_pass_used()
        view = _local_runner_view()
        shared = view['cron_threads'] <= 1 or view['dbs'] > 1
        window = SHARED_DISPATCH_SECONDS if shared else MAX_DISPATCH_SECONDS
        if limit:
            budget = limit - used - runner['max_timeout'] - WORKER_LIMIT_MARGIN_SECONDS
        else:
            budget = window
        budget = max(0.0, min(float(budget), window))
        try:
            wanted = int(self.env['ir.config_parameter'].sudo().get_param(CONCURRENCY_PARAM)
                         or DEFAULT_CONCURRENCY)
        except (TypeError, ValueError):
            wanted = DEFAULT_CONCURRENCY
        concurrency = max(1, min(wanted, MAX_CONCURRENCY))
        free = _pool_free_connections()
        if free is not None:
            # each running reply holds one cursor; keep one for the pass'
            # fetches and one for the shared "typing" refresh (small
            # db_maxconn of cron pods: >= concurrency + 2 free connections)
            concurrency = max(1, min(concurrency, free - 2))
        return {'concurrency': concurrency, 'budget': budget, 'cron_limit': limit, 'used': used,
                'shared': shared, 'idle_wait': SHARED_IDLE_WAIT_SECONDS if shared else DISPATCH_IDLE_WAIT_SECONDS}

    @api.model
    def _dispatch_release_cursor(self):
        """Commit the callback's transaction (job_cr) before the pass, so it
        holds no snapshot while replies run on other cursors. Odoo's own
        cron_cr cannot be committed from here: it stays idle in transaction
        with its snapshot and the FOR NO KEY UPDATE lock on the ir_cron row
        until the pass ends (the scheduled action cannot be edited, disabled
        or run manually meanwhile); short passes keep that bounded."""
        self.env.cr.commit()

    @api.model
    def _cron_run_jobs(self):
        """Cron callback: dispatch the due AI replies (see the module docstring).

        Returns the number of conversations handled in this pass."""
        runner = self._job_runner(refresh=True)
        from_cron = bool(self.env.context.get('cron_id'))
        if from_cron:
            # tell the web processes what this cron runner is (see STATE_PARAM)
            _report_runner_state(self.env.cr)
        if runner['mode'] != 'cron':
            # "thread" mode: the creating process runs fresh jobs; recover the lost ones
            return self._cron_recover_jobs()
        if not from_cron:
            # "Run Manually" from the cron form: never wait for replies in a web worker
            cron = self.env.ref(DISPATCH_CRON_XMLID, raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger()
            return 0
        settings = self._dispatch_settings(runner)
        db_name = self.env.cr.dbname
        self._dispatch_release_cursor()
        slots = settings['concurrency']
        dispatcher = Dispatcher(
            fetch=lambda exclude, limit: _dispatch_fetch(db_name, 0, exclude, limit, slots),
            run_unit=lambda channel_id, may_continue: _channel_worker(db_name, 0, channel_id, may_continue),
            concurrency=slots,
            budget=settings['budget'],
            poll_interval=DISPATCH_POLL_SECONDS,
            idle_wait=settings['idle_wait'],
            busy_retry=DISPATCH_BUSY_RETRY_SECONDS,
            max_backoff=DISPATCH_MAX_BACKOFF_SECONDS,
            should_stop=_server_stopping,
            name='woow_ai_contact.dispatch',
        )
        stats = dict({'fetch_errors': 0, 'failed': 0, 'stopped': False}, **dispatcher.run())
        if stats['retrigger_in'] is not None:
            with _job_cursor(db_name) as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                cron = env.ref(DISPATCH_CRON_XMLID, raise_if_not_found=False)
                if cron:
                    at = None
                    if stats['retrigger_in'] > 0:
                        at = fields.Datetime.now() + timedelta(seconds=stats['retrigger_in'])
                    cron._trigger(at)
        if stats['started'] or stats['budget_reached'] or stats['stopped'] or stats['fetch_errors'] \
                or stats['failed']:
            _logger.info("AI contacts: dispatcher pass: %(started)s conversation(s), at most %(max_parallel)s "
                         "at once, %(fetches)s fetches (%(fetch_errors)s failed), %(failed)s unit(s) failed, "
                         "budget reached: %(budget_reached)s, server stopping: %(stopped)s, next run in "
                         "%(retrigger_in)s s; window %(budget).0f s, concurrency %(concurrency)s, cron limit "
                         "%(cron_limit)s s, %(used).0f s used before the pass, shared worker: %(shared)s",
                         dict(stats, **{k: settings[k] for k in ('budget', 'concurrency', 'cron_limit', 'used',
                                                                 'shared')}))
        return stats['started']

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
        """"thread" mode recovery: run jobs whose thread was lost (restart,
        crash, worker recycled), as before the dispatcher existed.

        Channels whose lock is held (a job is being processed) are left alone.
        The work is handed to threads (in this mode the cron may not wait for
        replies: its time limit is too short). Returns the number of jobs
        handed over. ``commit=False`` (tests) runs them in this transaction.
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
