# -*- coding: utf-8 -*-
"""Bounded job dispatcher for the "run AI jobs" cron (no Odoo imports, unit tested).

One ``Dispatcher.run()`` is one cron pass:

* ``fetch(exclude, limit)`` returns the due work units (one per
  conversation) that are not in ``exclude``, at most ``limit``, and the
  seconds until the next known unit becomes due (``None``: nothing waits);
* every unit runs in its own thread (``run_unit(key)``), at most
  ``concurrency`` at a time; a unit returns ``BUSY`` when another process
  holds its conversation, which is then left alone for ``busy_retry``
  seconds;
* while units run, or a unit becomes due within ``idle_wait`` seconds (the
  debounce, a deferred job), the loop keeps going: it wakes up when a unit
  ends or every ``poll_interval`` seconds (never a busy loop: one fetch per
  wake-up);
* new units are only started during the first ``budget`` seconds; after that
  it waits for the running units and returns;
* ``run()`` JOINS every thread it started before returning, so the cron
  process never ends a pass (and never recycles) with a call in flight.

``run()`` returns a dict: ``started``, ``max_parallel``, ``fetches``,
``budget_reached`` and ``retrigger_in`` (seconds after which the cron must
run again: 0 = work is due now, ``None`` = nothing is waiting).
"""

import logging
import threading
import time

_logger = logging.getLogger(__name__)

BUSY = 'busy'


class Dispatcher:

    def __init__(self, fetch, run_unit, concurrency=4, budget=0.0, poll_interval=2.0, idle_wait=20.0,
                 busy_retry=5.0, clock=time.monotonic, name='woow_ai_contact.dispatch'):
        self.fetch = fetch
        self.run_unit = run_unit
        self.concurrency = max(1, int(concurrency))
        self.budget = max(0.0, float(budget))
        self.poll_interval = max(0.05, float(poll_interval))
        self.idle_wait = max(0.0, float(idle_wait))
        self.busy_retry = max(0.05, float(busy_retry))
        self.clock = clock
        self.name = name
        self._wake = threading.Event()
        self._results = {}
        self.stats = {'started': 0, 'max_parallel': 0, 'fetches': 0, 'budget_reached': False,
                      'retrigger_in': None}

    # --- threads ---

    def _target(self, key):
        result = None
        try:
            result = self.run_unit(key)
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
                if self._results.pop(key, None) == BUSY:
                    backoff[key] = self.clock() + self.busy_retry

    # --- loop ---

    def _fetch(self, exclude, limit):
        self.stats['fetches'] += 1
        units, next_due = self.fetch(exclude, limit)
        return list(units or [])[:max(0, limit)], next_due

    @staticmethod
    def _earliest(*values):
        values = [v for v in values if v is not None]
        return min(values) if values else None

    def run(self):
        start = self.clock()
        claim_until = start + self.budget
        running = {}
        backoff = {}
        first = True
        try:
            while True:
                self._reap(running, backoff)
                now = self.clock()
                for key in [k for k, at in backoff.items() if at <= now]:
                    del backoff[key]
                claiming = first or now < claim_until
                first = False
                next_due = None
                if claiming:
                    free = self.concurrency - len(running)
                    if free > 0:
                        units, next_due = self._fetch(set(running) | set(backoff), free)
                        for key in units:
                            self._start(key, running)
                else:
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
