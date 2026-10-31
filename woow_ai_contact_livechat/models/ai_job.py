# -*- coding: utf-8 -*-
"""Live Chat handoff jobs and operator alerts (core ``woow.ai.job`` hooks)."""

from odoo import models

from .im_livechat_channel import DEFAULT_HANDOFF_MESSAGE


class WoowAiJob(models.Model):
    _inherit = 'woow.ai.job'

    def _woow_ai_is_livechat_job(self):
        self.ensure_one()
        return self.channel_id.sudo()._woow_ai_is_livechat()

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
        self._finish('done', False, reply_message=message or None)
        self._supersede_older()
        commit_step()
        minutes = livechat_channel._woow_ai_pause_minutes_value()
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
