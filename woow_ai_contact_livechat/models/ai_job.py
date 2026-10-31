# -*- coding: utf-8 -*-
"""Live Chat handoff jobs and operator alerts (core ``woow.ai.job`` hooks),
and the hand-over from im_livechat_ai 1.1.0 for conversations it was
answering when the channel was switched."""

from datetime import timedelta

from odoo import fields, models
from odoo.tools import sql

from odoo.addons.woow_ai_contact.models.ai_job import JOB_MAX_AGE_MINUTES

from .im_livechat_channel import DEFAULT_HANDOFF_MESSAGE
from .legacy_migration import LEGACY_LOCK_NAMESPACE

# a reply job of a conversation im_livechat_ai is still answering is retried after this (_defer)
LEGACY_WAIT_SECONDS = 15


class WoowAiJob(models.Model):
    _inherit = 'woow.ai.job'

    def _woow_ai_is_livechat_job(self):
        self.ensure_one()
        return self.channel_id.sudo()._woow_ai_is_livechat()

    # --- im_livechat_ai 1.1.0 still answering (switch of a busy channel) ---

    def _woow_ai_legacy_busy(self):
        """True while im_livechat_ai 1.1.0 holds its session lock on this job's
        conversation (it is calling its LLM and may still post). Locks of this
        connection (the switch's own transaction lock) do not count."""
        self.ensure_one()
        self.env.cr.execute("""
            SELECT 1 FROM pg_locks
             WHERE locktype = 'advisory' AND granted AND pid <> pg_backend_pid()
               AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
               AND classid = %s AND objid = %s AND objsubid = 2
             LIMIT 1""", (LEGACY_LOCK_NAMESPACE, self.channel_id.id))
        return bool(self.env.cr.fetchone())

    def _woow_ai_legacy_answered(self):
        """True when a finished 1.1.0 reply had this job's trigger message in
        its LLM context (it answered it before or while the channel was switched)."""
        self.ensure_one()
        cr = self.env.cr
        if not self.message_id or not sql.table_exists(cr, 'im_livechat_ai_job') \
                or not sql.column_exists(cr, 'im_livechat_ai_job', 'context_message_id'):
            return False
        cr.execute("""
            SELECT 1 FROM im_livechat_ai_job
             WHERE discuss_channel_id = %s AND state = 'done' AND reply_message_id IS NOT NULL
               AND context_message_id >= %s
             LIMIT 1""", (self.channel_id.id, self.message_id.id))
        return bool(cr.fetchone())

    def _process_locked(self, commit_step, commit=True):
        """A reply job waits (stays pending) while im_livechat_ai is still
        answering its conversation: answering now could repeat or contradict
        the 1.1.0 reply that is about to be posted."""
        self.ensure_one()
        if (self.exists() and self.state in ('pending', 'running') and self.kind == 'reply'
                and self._woow_ai_is_livechat_job() and self._woow_ai_legacy_busy()):
            now = fields.Datetime.now()
            if not self.create_date or self.create_date >= now - timedelta(minutes=JOB_MAX_AGE_MINUTES):
                # core _defer: the dispatcher retries it after LEGACY_WAIT_SECONDS
                # (it stays in its pass; the cron is triggered at that time too)
                self._defer(LEGACY_WAIT_SECONDS, 'waiting: im_livechat_ai is still answering this conversation')
                commit_step()
                return
        return super()._process_locked(commit_step, commit=commit)

    def _already_answered(self):
        if super()._already_answered():
            return True
        return self._woow_ai_is_livechat_job() and self._woow_ai_legacy_answered()

    def _process_handoff(self, commit_step):
        """Visitor asked for a human: post the handoff message once, then alert
        the operators (the conversation was paused by the visitor's post).

        The notice and the done state are committed together: a job that is not
        done never posted its notice, and the recovery cron never repeats it.
        No backend call (works without a configured backend)."""
        if not self._woow_ai_is_livechat_job():
            return super()._process_handoff(commit_step)
        channel = self.channel_id.sudo()
        contact = self.contact_id.sudo()
        if not self._contact_still_answers():
            self._finish('skipped', 'AI turned off or contact changed')
            commit_step()
            return
        livechat_channel = channel.livechat_channel_id
        text = (livechat_channel.woow_ai_handoff_message or '').strip() or DEFAULT_HANDOFF_MESSAGE
        message = contact._post_text(channel, text)
        minutes = livechat_channel._woow_ai_pause_minutes_value()
        # the notice is a conversation event (a fact for the model), not the AI's words
        channel._woow_ai_log_event('handoff', message=message, trigger=self.message_id, minutes=minutes)
        self._finish('done', False, reply_message=message or None)
        self._supersede_older()
        commit_step()
        channel._woow_ai_alert_operators('handoff', lambda ch, tz: ch.env._(
            'The visitor in "%(name)s" asks for a human. The AI is paused for %(minutes)s minutes: please '
            'take over. Type /ai on in the conversation to resume the AI now.',
            name=ch.display_name, minutes=minutes))
        commit_step()

    def _notify_failure(self, reason, visitor_told=True):
        super()._notify_failure(reason, visitor_told=visitor_told)
        if not self._woow_ai_is_livechat_job():
            return
        channel = self.channel_id.sudo()

        def text(ch, _tz):
            if visitor_told:
                return ch.env._('The AI could not answer "%(name)s" (%(reason)s). The visitor got the error '
                                'message: please take over.', name=ch.display_name, reason=reason)
            return ch.env._('The AI could not answer "%(name)s" (%(reason)s): please take over.',
                            name=ch.display_name, reason=reason)

        channel._woow_ai_alert_operators('failure', text)
