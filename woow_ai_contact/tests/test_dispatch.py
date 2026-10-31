# -*- coding: utf-8 -*-
"""Dispatcher: replies run in the cron process (owner decision 29)."""

import threading
import time
from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.modules import module as odoo_module
from odoo.sql_db import db_connect
from odoo.tests.common import BaseCase, tagged
from odoo.tools import config, mute_logger

from odoo.addons.woow_ai_contact.models import ai_job
from odoo.addons.woow_ai_contact.models.ai_job import LOCK_NAMESPACE
from odoo.addons.woow_ai_contact.tools.dispatcher import BUSY, Dispatcher

from .common import BACKEND_POST, AiContactCase, openai_ok, refused

CRON_INFO = {'mode': 'cron', 'reason': 'cron_runner', 'forced': False, 'server': 'prefork', 'cron_limit': 900,
             'required': 430, 'max_timeout': 300, 'cron_runner': 'local'}
THREAD_INFO = dict(CRON_INFO, mode='thread', reason='no_cron_runner', cron_runner='none')


class Units:
    """Fake fetch / run_unit pair: `keys` become due at `due_at` (monotonic)."""

    def __init__(self, keys, seconds=0.2, due_at=None, results=None):
        self.pending = list(keys)
        self.seconds = seconds
        self.due_at = due_at or {}
        self.results = dict(results or {})
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.started = []
        self.ended = []
        self.fetches = []

    def fetch(self, exclude, limit):
        now = time.monotonic()
        with self.lock:
            self.fetches.append(now)
            due = [k for k in self.pending if k not in exclude and self.due_at.get(k, 0) <= now]
            later = [self.due_at[k] - now for k in self.pending if k not in exclude and self.due_at.get(k, 0) > now]
        return due[:limit], (min(later) if later else None)

    def run(self, key):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.append(key)
        time.sleep(self.seconds)
        with self.lock:
            self.active -= 1
            result = self.results.pop(key, None)
            if result != BUSY and key in self.pending:
                self.pending.remove(key)
            self.ended.append(key)
        return result


def alive_unit_threads(name):
    return [t for t in threading.enumerate() if t.name.startswith(name + '.')]


@tagged('woow_ai_contact')
class TestDispatcherLoop(BaseCase):
    """tools/dispatcher.py without a database."""

    def dispatcher(self, units, **kw):
        kw.setdefault('name', 'test.dispatch.%s' % id(units))
        kw.setdefault('poll_interval', 0.05)
        kw.setdefault('busy_retry', 0.2)
        return Dispatcher(units.fetch, units.run, **kw)

    def test_concurrency_limit_and_join_before_return(self):
        units = Units(range(10), seconds=0.2)
        d = self.dispatcher(units, concurrency=3, budget=30)
        stats = d.run()
        self.assertEqual(stats['started'], 10)
        self.assertEqual(units.max_active, 3)
        self.assertEqual(stats['max_parallel'], 3)
        # every unit thread ended before run() returned
        self.assertEqual(sorted(units.ended), list(range(10)))
        self.assertFalse(alive_unit_threads(d.name))
        self.assertIsNone(stats['retrigger_in'])

    def test_budget_stops_claiming_waits_and_asks_for_a_new_pass(self):
        units = Units(range(100), seconds=0.3)
        d = self.dispatcher(units, concurrency=2, budget=0.4)
        began = time.monotonic()
        stats = d.run()
        elapsed = time.monotonic() - began
        self.assertTrue(stats['budget_reached'])
        self.assertLess(stats['started'], 10)
        self.assertEqual(len(units.ended), stats['started'])  # joined
        self.assertFalse(alive_unit_threads(d.name))
        self.assertEqual(stats['retrigger_in'], 0.0)  # work left: run again now
        self.assertLess(elapsed, 2.0)

    def test_zero_budget_runs_one_batch(self):
        units = Units(range(5), seconds=0.1)
        stats = self.dispatcher(units, concurrency=2, budget=0).run()
        self.assertEqual(stats['started'], 2)
        self.assertEqual(stats['retrigger_in'], 0.0)

    def test_unit_due_soon_is_waited_for(self):
        now = time.monotonic()
        units = Units(['a', 'b'], seconds=0.05, due_at={'b': now + 0.4})
        stats = self.dispatcher(units, concurrency=4, budget=30, idle_wait=2, poll_interval=0.2).run()
        self.assertEqual(units.started, ['a', 'b'])
        self.assertIsNone(stats['retrigger_in'])
        # not a busy loop: one fetch per poll interval (0.4 s / 0.2 s), plus the first and the last
        self.assertLessEqual(len(units.fetches), 6)

    def test_unit_due_later_is_left_to_a_trigger(self):
        units = Units(['late'], due_at={'late': time.monotonic() + 30})
        began = time.monotonic()
        stats = self.dispatcher(units, budget=30, idle_wait=2).run()
        self.assertLess(time.monotonic() - began, 1)
        self.assertEqual(stats['started'], 0)
        self.assertGreater(stats['retrigger_in'], 25)

    def test_busy_unit_is_backed_off_then_retried(self):
        units = Units(['x'], seconds=0.01, results={'x': BUSY})
        stats = self.dispatcher(units, budget=30, idle_wait=2, busy_retry=0.3, poll_interval=0.2).run()
        self.assertEqual(units.started, ['x', 'x'])
        gap = units.fetches[-1] - units.fetches[0]
        self.assertGreater(gap, 0.25)
        self.assertLessEqual(len(units.fetches), 6)
        self.assertIsNone(stats['retrigger_in'])

    def test_polling_while_units_run_is_bounded(self):
        units = Units(['slow'], seconds=1.0)
        self.dispatcher(units, concurrency=4, budget=30, poll_interval=0.25).run()
        # 1 s of reply at a 0.25 s poll: about 4 fetches, plus the first and the final one
        self.assertLessEqual(len(units.fetches), 8)

    @mute_logger('odoo.addons.woow_ai_contact.tools.dispatcher')
    def test_unit_exception_does_not_stop_the_others(self):
        units = Units(['bad', 'good'], seconds=0.05)
        original = units.run

        def run(key):
            if key == 'bad':
                units.pending.remove('bad')
                raise RuntimeError('boom')
            return original(key)

        stats = Dispatcher(units.fetch, run, concurrency=2, budget=30, poll_interval=0.05).run()
        self.assertEqual(stats['started'], 2)
        self.assertIn('good', units.ended)


@tagged('woow_ai_contact')
class TestDispatchModel(AiContactCase):

    def runner(self, info):
        return patch.object(type(self.Job), '_job_runner', return_value=info)

    def dispatcher_triggers(self):
        cron = self.env.ref('woow_ai_contact.ir_cron_run_ai_jobs')
        return self.env['ir.cron.trigger'].search([('cron_id', '=', cron.id)], order='id')

    @staticmethod
    def thread_callbacks(cr):
        return [f for f in cr.postcommit._funcs if getattr(f, 'woow_ai_job_ids', None)]

    def test_cron_record(self):
        cron = self.env.ref('woow_ai_contact.ir_cron_run_ai_jobs')
        self.assertTrue(cron.active)
        self.assertEqual(cron.code.strip(), 'model._cron_run_jobs()')
        self.assertEqual((cron.interval_number, cron.interval_type), (1, 'minutes'))
        self.assertFalse(self.env.ref('woow_ai_contact.ir_cron_recover_ai_jobs', raise_if_not_found=False))
        self.assertFalse(self.env['ir.cron'].search([('code', '=', 'model._cron_recover_jobs()')]))

    def test_http_path_starts_no_thread_when_a_cron_runner_exists(self):
        chat = self.chat(self.internal)
        before = self.dispatcher_triggers()
        with self.runner(CRON_INFO):
            self.post(chat, self.internal, 'hello')
        self.assertEqual(len(self.jobs(chat)), 1)
        self.assertFalse(self.thread_callbacks(self.env.cr))
        triggers = self.dispatcher_triggers() - before
        self.assertEqual(len(triggers), 1)
        # due now: Odoo sends the NOTIFY after the commit (the debounce is the job's run_after)
        self.assertLessEqual(triggers.call_at, fields.Datetime.now())

    def test_fallback_mode_starts_a_thread_after_commit(self):
        chat = self.chat(self.internal)
        before = self.dispatcher_triggers()
        with self.runner(THREAD_INFO):
            self.post(chat, self.internal, 'hello')
        job = self.jobs(chat)
        callbacks = self.thread_callbacks(self.env.cr)
        self.assertEqual([f.woow_ai_job_ids for f in callbacks], [job.ids])
        # recovery trigger later, as before
        triggers = self.dispatcher_triggers() - before
        self.assertGreater(triggers.call_at, job.run_after)

    def test_runner_mode(self):
        Job = self.Job
        cron = self.env.ref('woow_ai_contact.ir_cron_run_ai_jobs')
        ICP = self.env['ir.config_parameter'].sudo()
        base = {'workers': 2, 'max_cron_threads': 1, 'stop_after_init': False,
                'limit_time_real': 120, 'limit_time_real_cron': 900}
        self.contact.timeout_seconds = 300
        self.n8n.timeout_seconds = 200
        with patch.object(odoo_module, 'current_test', None), patch.dict(config.options, base):
            info = Job._compute_job_runner()
            self.assertEqual((info['mode'], info['reason'], info['cron_runner']), ('cron', 'cron_runner', 'local'))
            self.assertEqual(info['max_timeout'], 300)
            self.assertEqual(info['required'], 300 + 60 + 70)  # prefork: the worker's idle sleep counts
            with patch.dict(config.options, {'limit_time_real_cron': -1}):  # -1: limit_time_real (120)
                info = Job._compute_job_runner()
                self.assertEqual((info['mode'], info['reason'], info['cron_limit']),
                                 ('thread', 'cron_limit_too_low', 120))
                ICP.set_param('woow_ai_contact.job_runner', 'cron')
                self.assertEqual(Job._compute_job_runner()['reason'], 'forced_cron_limit_too_low')
                ICP.set_param('woow_ai_contact.job_runner', 'auto')
            with patch.dict(config.options, {'limit_time_real_cron': 0}):  # prefork 0: no watchdog
                self.assertEqual(Job._compute_job_runner()['mode'], 'cron')
            with patch.dict(config.options, {'workers': 0, 'limit_time_real_cron': 0, 'limit_time_real': 400}):
                # threaded: 0 falls back to limit_time_real, and no idle sleep to reserve
                info = Job._compute_job_runner()
                self.assertEqual((info['cron_limit'], info['required'], info['mode']), (400, 360, 'cron'))
            # no cron thread here: fallback, unless another process ran the dispatcher recently
            with patch.dict(config.options, {'max_cron_threads': 0}):
                cron.write({'lastcall': fields.Datetime.now() - timedelta(hours=1)})
                self.env.flush_all()
                info = Job._compute_job_runner()
                self.assertEqual((info['mode'], info['reason']), ('thread', 'no_cron_runner'))
                cron.write({'lastcall': fields.Datetime.now() - timedelta(minutes=2)})
                self.env.flush_all()
                info = Job._compute_job_runner()
                self.assertEqual((info['mode'], info['cron_runner']), ('cron', 'other process'))
            with patch.dict(config.options, {'stop_after_init': True}):
                cron.write({'lastcall': False})
                self.env.flush_all()
                self.assertEqual(Job._compute_job_runner()['reason'], 'no_cron_runner')
            ICP.set_param('woow_ai_contact.job_runner', 'thread')
            self.assertEqual(Job._compute_job_runner()['reason'], 'forced_thread')
        # tests themselves: no cron runner
        ICP.set_param('woow_ai_contact.job_runner', 'auto')
        with patch.dict(config.options, base):
            self.assertEqual(Job._compute_job_runner()['mode'], 'thread')

    def test_runner_is_cached_and_logged_once(self):
        db = self.env.cr.dbname
        ai_job._runner_cache.pop(db, None)
        ai_job._runner_logged.pop(db, None)
        with patch.object(type(self.Job), '_compute_job_runner', return_value=CRON_INFO) as compute, \
                patch.object(ai_job, '_logger') as logger:
            for _i in range(5):
                self.Job._job_runner()
            self.Job._job_runner(refresh=True)
        self.assertEqual(compute.call_count, 2)
        self.assertEqual(logger.info.call_count, 1)  # same mode: logged once
        # a timeout change is seen at once
        self.contact.timeout_seconds = 120
        self.assertNotIn(db, ai_job._runner_cache)

    def test_runner_shown_on_the_form(self):
        with self.runner(THREAD_INFO):
            self.contact.invalidate_recordset(['job_runner_info', 'job_runner_warning'])
            self.assertTrue(self.contact.job_runner_warning)
            self.assertIn('fallback mode', self.contact.job_runner_info)
        with self.runner(CRON_INFO):
            self.contact.invalidate_recordset(['job_runner_info', 'job_runner_warning'])
            self.assertFalse(self.contact.job_runner_warning)
            self.assertIn('AI Contacts: Run AI Replies', self.contact.job_runner_info)
            self.assertIn('900', self.contact.job_runner_info)

    # --- candidates, locks, no double run ---

    def due(self, job, seconds_ago=5):
        job.write({'run_after': fields.Datetime.now() - timedelta(seconds=seconds_ago)})
        self.env.flush_all()

    def hold_channel_lock(self, channel_id):
        """Another process (a second dispatcher) runs this channel: its advisory
        lock, held by a real other connection."""
        other = db_connect(self.env.cr.dbname).cursor()
        other.execute("SELECT pg_advisory_lock(%s, %s)", (LOCK_NAMESPACE, channel_id))

        def release():
            other.execute("SELECT pg_advisory_unlock(%s, %s)", (LOCK_NAMESPACE, channel_id))
            other.rollback()
            other.close()
        return release

    def test_candidates(self):
        chat1, chat2 = self.chat(self.internal), self.chat(self.portal)
        self.post(chat2, self.portal, 'first')
        self.post(chat1, self.internal, 'second')
        self.post(chat1, self.internal, 'third')
        j2 = self.jobs(chat2)
        j1a, j1b = self.jobs(chat1)
        # debounce: nothing due yet
        channels, next_due = self.Job._dispatch_candidates(limit=10)
        self.assertEqual(channels, [])
        self.assertTrue(0 < next_due <= 2)
        for job in (j2, j1a, j1b):
            self.due(job)
        channels, next_due = self.Job._dispatch_candidates(limit=10)
        self.assertEqual(channels, [chat2.id, chat1.id])  # oldest newest-job first
        self.assertIsNone(next_due)
        self.assertEqual(self.Job._due_job_id(chat1.id), j1b.id)  # the newest answers
        self.assertEqual(self.Job._dispatch_candidates(limit=1)[0], [chat2.id])
        self.assertEqual(self.Job._dispatch_candidates(exclude={chat2.id}, limit=10)[0], [chat1.id])
        # "thread" mode grace: only jobs stale for 30 s
        self.assertEqual(self.Job._dispatch_candidates(limit=10, grace=30)[0], [])
        self.assertFalse(self.Job._due_job_id(chat1.id, grace=30))
        # a job running with a live owner is left alone; with a dead owner it is re-queued
        (j1a | j1b).write({'state': 'skipped'})
        j2.write({'state': 'running', 'started_at': fields.Datetime.now() - timedelta(minutes=5)})
        self.env.flush_all()
        self.assertEqual(self.Job._dispatch_candidates(limit=10)[0], [chat2.id])
        release = self.hold_channel_lock(chat2.id)
        try:
            channels, next_due = self.Job._dispatch_candidates(limit=10)
            self.assertEqual(channels, [])
            self.assertEqual(next_due, ai_job.DISPATCH_BUSY_RETRY_SECONDS)
        finally:
            release()

    def test_no_double_run_across_two_dispatchers(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'once')
        job = self.jobs(chat)
        self.due(job)
        release = self.hold_channel_lock(chat.id)
        try:
            # dispatcher B while dispatcher A holds the conversation: no claim, no call
            self.assertEqual(self.Job._dispatch_candidates(limit=10)[0], [])
            with patch(BACKEND_POST) as post:
                _channel, ran = job._run_job(commit=False)
            self.assertFalse(ran)
            post.assert_not_called()
            self.assertEqual((job.state, job.run_count), ('pending', 0))
        finally:
            release()
        # A answered it: B finds nothing to do any more
        with patch(BACKEND_POST, return_value=openai_ok('one answer')) as post:
            job._run_job(commit=False)
            job._run_job(commit=False)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.Job._dispatch_candidates(limit=10), ([], None))
        self.assertEqual(len(self.bot_messages(chat)), 1)

    def test_defer_retriggers_the_dispatcher(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'wait')
        job = self.jobs(chat)
        before = self.dispatcher_triggers()
        callbacks = len(self.thread_callbacks(self.env.cr))
        with self.runner(CRON_INFO):
            due = job._defer(15, 'waiting for something')
        self.assertEqual((job.state, job.error), ('pending', 'waiting for something'))
        self.assertEqual(job.run_after, due)
        trigger = self.dispatcher_triggers() - before
        self.assertEqual(trigger.call_at, due)
        self.assertEqual(len(self.thread_callbacks(self.env.cr)), callbacks)  # no thread
        channels, next_due = self.Job._dispatch_candidates(limit=10)
        self.assertEqual(channels, [])
        self.assertTrue(10 < next_due <= 15)  # within the dispatcher's idle wait: same pass

    @mute_logger('odoo.addons.woow_ai_contact.models.ai_job')
    def test_crash_before_claim_is_not_picked_again_at_once(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'x')
        job = self.jobs(chat)
        self.due(job)
        cr = self.env.cr
        with patch.object(type(self.Job), '_process_locked', side_effect=RuntimeError('boom')), \
                patch.object(cr, 'commit'), patch.object(cr, 'rollback'):
            job._run_job(commit=True)
        self.assertEqual(job.state, 'pending')
        self.assertIn('boom', job.error)
        self.assertGreater(job.run_after, fields.Datetime.now() + timedelta(seconds=30))
        self.env.flush_all()
        self.assertFalse(self.Job._due_job_id(chat.id))

    # --- the cron entry ---

    def test_settings(self):
        Job = self.Job
        self.contact.timeout_seconds = 300
        self.n8n.timeout_seconds = 300
        info = dict(CRON_INFO, max_timeout=300)
        thread = threading.current_thread()
        with patch.dict(config.options, {'workers': 2, 'limit_time_real': 120, 'limit_time_real_cron': 900}), \
                patch.object(ai_job, '_pool_free_connections', return_value=60):
            s = Job._dispatch_settings(info)
            # prefork: 900 - 70 (idle sleep) - 300 - 60
            self.assertEqual((s['budget'], s['concurrency']), (min(470, ai_job.MAX_DISPATCH_SECONDS), 4))
            with patch.dict(config.options, {'limit_time_real_cron': 400}):
                self.assertEqual(Job._dispatch_settings(info)['budget'], 0)
            with patch.dict(config.options, {'limit_time_real_cron': 0}):
                self.assertEqual(Job._dispatch_settings(info)['budget'], ai_job.MAX_DISPATCH_SECONDS)
            self.env['ir.config_parameter'].sudo().set_param('woow_ai_contact.max_concurrency', '50')
            self.assertEqual(Job._dispatch_settings(info)['concurrency'], ai_job.MAX_CONCURRENCY)
            self.env['ir.config_parameter'].sudo().set_param('woow_ai_contact.max_concurrency', 'x')
            self.assertEqual(Job._dispatch_settings(info)['concurrency'], 4)
        # threaded cron thread: what the pass already used counts
        with patch.dict(config.options, {'workers': 0, 'limit_time_real': 120, 'limit_time_real_cron': 1800}), \
                patch.object(ai_job, '_pool_free_connections', return_value=6), \
                patch.object(thread, 'type', 'cron', create=True), \
                patch.object(thread, 'start_time', time.time() - 100, create=True):
            s = Job._dispatch_settings(info)
            self.assertAlmostEqual(s['budget'], ai_job.MAX_DISPATCH_SECONDS, delta=1)
            self.assertEqual(s['concurrency'], 2)  # db_maxconn 6 of a cron pod: (6 - 2) // 2
            with patch.dict(config.options, {'limit_time_real_cron': 600}):
                self.assertAlmostEqual(Job._dispatch_settings(info)['budget'], 600 - 100 - 300 - 60, delta=2)

    def test_manual_run_never_waits_in_a_web_worker(self):
        before = self.dispatcher_triggers()
        with self.runner(CRON_INFO), patch.object(ai_job, 'Dispatcher') as dispatcher:
            self.assertEqual(self.Job._cron_run_jobs(), 0)
        dispatcher.assert_not_called()
        self.assertEqual(len(self.dispatcher_triggers() - before), 1)

    def test_cron_pass(self):
        before = self.dispatcher_triggers()
        stats = {'started': 2, 'max_parallel': 2, 'fetches': 3, 'budget_reached': True, 'retrigger_in': 0.0}
        with self.runner(CRON_INFO), patch.object(ai_job, 'Dispatcher') as dispatcher, \
                patch.object(type(self.Job), '_dispatch_release_cursor') as release, \
                patch.object(ai_job, '_job_cursor', side_effect=lambda db: nullcontext(self.env.cr)):
            dispatcher.return_value.run.return_value = stats
            self.assertEqual(self.Job.with_context(cron_id=1)._cron_run_jobs(), 2)
        release.assert_called_once()
        kwargs = dispatcher.call_args.kwargs
        self.assertGreaterEqual(kwargs['concurrency'], 1)
        self.assertEqual(kwargs['poll_interval'], ai_job.DISPATCH_POLL_SECONDS)
        self.assertEqual(len(self.dispatcher_triggers() - before), 1)  # budget reached: next pass now

    def test_thread_mode_cron_recovers(self):
        with self.runner(THREAD_INFO), patch.object(type(self.Job), '_cron_recover_jobs', return_value=3) as rec, \
                patch.object(ai_job, 'Dispatcher') as dispatcher:
            self.assertEqual(self.Job.with_context(cron_id=1)._cron_run_jobs(), 3)
        rec.assert_called_once()
        dispatcher.assert_not_called()

    # --- language of the default messages ---

    def test_uninstalled_identity_language_falls_back(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'x')
        job = self.jobs(chat)
        identity = dict(self.contact._identity(self.internal.partner_id), lang='fr_BE')
        self.assertFalse(self.env['res.lang']._get_data(code='fr_BE'))
        with patch.object(type(chat), '_woow_ai_identity', return_value=identity), \
                patch(BACKEND_POST, side_effect=[refused(), refused()]), \
                patch('odoo.addons.woow_ai_contact.models.ai_contact_backend.time.sleep'):
            job._run_job(commit=False)
            note = self.contact._reply_note(chat, 'media')
        self.assertEqual(job.state, 'failed', job.error)  # _give_up did not raise
        self.assertIn('Sorry, I cannot answer right now', job.reply_message_id.body)
        self.assertIn('could not be delivered', note)
        self.assertEqual(self.contact._installed_lang('fr_BE', 'en_US'), 'en_US')
        self.assertEqual(self.contact._installed_lang(None), 'en_US')


@tagged('woow_ai_contact')
class TestJobQueries(AiContactCase):
    """The job path costs the same number of queries whatever the history size
    (measured in the sandbox: 83 -> 76 queries for 4 messages, 122 -> 75 for 20)."""

    def queries(self, fn):
        self.env.flush_all()
        self.env.invalidate_all()
        before = self.env.cr.sql_log_count
        fn()
        self.env.flush_all()
        return self.env.cr.sql_log_count - before

    def fill(self, chat, count):
        for i in range(count):
            atts = self.attachment(chat, self.internal, name='f%s.pdf' % i, raw=b'%PDF-1.4 x' + bytes([65 + i]))
            atts |= self.attachment(chat, self.internal, name='g%s.pdf' % i, raw=b'%PDF-1.4 y' + bytes([65 + i]))
            self.post(chat, self.internal, 'message %s' % i, attachments=atts)

    def job_queries(self, contact, count):
        chat = self.chat(self.internal, contact)
        self.fill(chat, count)
        job = self.jobs(chat)[-1]
        reply = openai_ok('ok') if contact.backend_type == 'openai' else None
        if reply is None:
            from .common import http_response
            reply = http_response(200, {'reply': {'text': 'ok'}})
        with patch(BACKEND_POST, return_value=reply):
            n = self.queries(lambda: job._run_job(commit=False))
        self.assertEqual(job.state, 'done', job.error)
        return n

    def test_history_attachments_no_n_plus_one(self):
        for contact in (self.contact, self.n8n):
            small = self.job_queries(contact, 2)
            other = self.env['woow.ai.contact'].create({
                'name': 'Big %s' % contact.backend_type, 'backend_type': contact.backend_type,
                'endpoint_url': contact.endpoint_url, 'api_key': 'k', 'openai_stream': False})
            big = self.job_queries(other, 12)
            self.assertLessEqual(big - small, 2, '%s: %s queries for 2 messages, %s for 12' % (
                contact.backend_type, small, big))

    def test_post_reply_files_batched(self):
        from .common import TINY_PNG
        chat = self.chat(self.internal)

        def prepared(n):
            return {'html': '<p>x</p>', 'files': [{'name': 'a%s.png' % k, 'raw': TINY_PNG,
                                                   'mimetype': 'image/png'} for k in range(n)]}
        one = self.queries(lambda: self.contact._post_replies(chat, [prepared(1)]))
        three = self.queries(lambda: self.contact._post_replies(chat, [prepared(3)]))
        self.assertLessEqual(three - one, 1)
