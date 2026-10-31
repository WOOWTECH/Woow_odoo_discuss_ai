# -*- coding: utf-8 -*-
"""Bounded job dispatcher for the "run AI jobs" cron (no Odoo imports, unit tested).

One ``Dispatcher.run()`` is one cron pass:

* ``fetch(exclude, limit)`` returns the due work units (one per
  conversation) that are not in ``exclude``, at most ``limit``, and the
  seconds until the next known unit becomes due (``None``: nothing waits).
  A fetch that raises is logged and counts as "nothing due" (a database
  hiccup must not fail the cron and push it behind every other cron);
* every unit runs in its own thread (``run_unit(key, may_continue)``), at
  most ``concurrency`` at a time. A unit may chain several jobs of its
  conversation, but only starts another one while ``may_continue()`` is
  True (the claiming window is open and the server is not stopping);
* a unit returns ``BUSY`` when another process holds its conversation, which
  is then left alone for ``busy_retry`` seconds; a unit that raises (or
  returns ``FAILED``) is backed off the same way, doubling on every
  consecutive failure up to ``max_backoff`` (never a busy loop);
* while units run, or a unit becomes due within ``idle_wait`` seconds (the
  debounce, a deferred job), the loop keeps going: it wakes up when a unit
  ends or every ``poll_interval`` seconds (one fetch per wake-up);
* new units are only started during the first ``budget`` seconds and while
  ``should_stop()`` is False (server shutdown); after that it waits for the
  running units and returns;
* ``run()`` JOINS every thread it started before returning, so the cron
  process never ends a pass (and never recycles) with a call in flight.

``run()`` returns a dict: ``started``, ``max_parallel``, ``fetches``,
``fetch_errors``, ``failed``, ``budget_reached``, ``stopped`` and
``retrigger_in`` (seconds after which the cron must run again: 0 = work is
due now, ``None`` = nothing is waiting).
"""

import logging
import threading
import time

_logger = logging.getLogger(__name__)

BUSY = 'busy'
FAILED = 'failed'


class Dispatcher:

    def __init__(self, fetch, run_unit, concurrency=4, budget=0.0, poll_interval=2.0, idle_wait=20.0,
                 busy_retry=5.0, max_backoff=60.0, should_stop=None, clock=time.monotonic,
                 name='woow_ai_contact.dispatch'):
        self.fetch = fetch
        self.run_unit = run_unit
        self.concurrency = max(1, int(concurrency))
        self.budget = max(0.0, float(budget))
        self.poll_interval = max(0.05, float(poll_interval))
        self.idle_wait = max(0.0, float(idle_wait))
        self.busy_retry = max(0.05, float(busy_retry))
        self.max_backoff = max(self.busy_retry, float(max_backoff))
        self.should_stop = should_stop
        self.clock = clock
        self.name = name
        self._wake = threading.Event()
        self._results = {}
        self._failures = {}
        self._claim_until = None
        self._stopped = False
        self.stats = {'started': 0, 'max_parallel': 0, 'fetches': 0, 'fetch_errors': 0, 'failed': 0,
                      'budget_reached': False, 'stopped': False, 'retrigger_in': None}

    # --- claiming window ---

    def stopping(self):
        """True once ``should_stop()`` said so (sticky; a failing check is ignored)."""
        if not self._stopped and self.should_stop is not None:
            try:
                self._stopped = bool(self.should_stop())
            except Exception:  # noqa: BLE001 - a broken check must not stop the replies
                _logger.warning("%s: stop check failed", self.name, exc_info=True)
            if self._stopped:
                self.stats['stopped'] = True
                _logger.info("%s: server is stopping, no new replies are started", self.name)
        return self._stopped

    def may_continue(self):
        """Given to every unit: True while it may start another job."""
        return self._claim_until is not None and self.clock() < self._claim_until and not self.stopping()

    # --- threads ---

    def _target(self, key):
        result = FAILED
        try:
            result = self.run_unit(key, self.may_continue)
        except Exception:  # noqa: BLE001 - one conversation must not stop the others
            _logger.exception("%s: unit %s failed", self.name, key)
        finally:
            self._results[key] = result
            self._wake.set()

    def _start(self, key, running):
        thread = threading.Thread(target=self._target, args=(key,), name='%s.%s' % (self.name, key),
                                  daemon=False)
        running[key] = thread
        thread.start()
        self.stats['started'] += 1
        self.stats['max_parallel'] = max(self.stats['max_parallel'], len(running))

    def _reap(self, running, backoff):
        for key, thread in list(running.items()):
            if not thread.is_alive():
                thread.join()
                del running[key]
                result = self._results.pop(key, None)
                if result == BUSY:
                    backoff[key] = self.clock() + self.busy_retry
                elif result == FAILED:
                    self.stats['failed'] += 1
                    failures = self._failures.get(key, 0) + 1
                    self._failures[key] = failures
                    delay = min(self.busy_retry * (2 ** (failures - 1)), self.max_backoff)
                    backoff[key] = self.clock() + delay
                else:
                    self._failures.pop(key, None)

    # --- loop ---

    def _fetch(self, exclude, limit):
        self.stats['fetches'] += 1
        try:
            units, next_due = self.fetch(exclude, limit)
        except Exception:  # noqa: BLE001 - logged; the next wake-up (or the next pass) fetches again
            self.stats['fetch_errors'] += 1
            _logger.exception("%s: fetching due work failed", self.name)
            return [], None
        return list(units or [])[:max(0, limit)], next_due

    @staticmethod
    def _earliest(*values):
        values = [v for v in values if v is not None]
        return min(values) if values else None

    def run(self):
        start = self.clock()
        self._claim_until = claim_until = start + self.budget
        running = {}
        backoff = {}
        first = True
        try:
            while True:
                self._reap(running, backoff)
                now = self.clock()
                for key in [k for k, at in backoff.items() if at <= now]:
                    del backoff[key]
                claiming = (first or now < claim_until) and not self.stopping()
                first = False
                next_due = None
                if claiming:
                    free = self.concurrency - len(running)
                    if free > 0:
                        units, next_due = self._fetch(set(running) | set(backoff), free)
                        for key in units:
                            self._start(key, running)
                elif not self._stopped:
                    self.stats['budget_reached'] = True
                backoff_due = min(backoff.values()) - now if backoff else None
                waiting = self._earliest(next_due, backoff_due)
                if not running:
                    if not claiming or waiting is None or waiting > self.idle_wait:
                        break
                    timeout = min(max(waiting, 0.05), self.poll_interval)
                else:
                    timeout = self.poll_interval
                    if waiting is not None and claiming:
                        timeout = min(timeout, max(waiting, 0.05))
                self._wake.wait(timeout)
                self._wake.clear()
        finally:
            for thread in list(running.values()):
                thread.join()
            self._reap(running, backoff)
        # work that came in while the last units ran, or that waits longer
        # than idle_wait: the caller triggers the cron again
        now = self.clock()
        units, next_due = self._fetch(set(k for k, at in backoff.items() if at > now), 1)
        backoff_due = min(backoff.values()) - now if backoff else None
        self.stats['retrigger_in'] = 0.0 if units else self._earliest(
            max(next_due, 0.0) if next_due is not None else None,
            max(backoff_due, 0.0) if backoff_due is not None else None)
        return dict(self.stats)
