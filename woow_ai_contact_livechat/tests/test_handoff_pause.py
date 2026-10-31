# -*- coding: utf-8 -*-
"""Triggers, pause, human handoff, paused notice, resume, operator alerts
(im_livechat_ai 1.1.0 package D behaviour, with the L1 fixes)."""

from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import tagged

from odoo.addons.woow_ai_contact_livechat.models.im_livechat_channel import (
    DEFAULT_HANDOFF_KEYWORDS, DEFAULT_HANDOFF_MESSAGE, DEFAULT_PAUSED_MESSAGE, is_handoff_request,
)

from .common import BACKEND_POST, LivechatCase, http_response, openai_ok


@tagged('woow_ai_contact_livechat', 'post_install', '-at_install')
class TestTrigger(LivechatCase):

    def test_visitor_partner_gets_a_reply(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '請問營業時間？')
        job = self.jobs(session)
        self.assertEqual((len(job), job.kind, job.contact_id), (1, 'reply', self.contact))
        with patch(BACKEND_POST, return_value=openai_ok('我們 9 點開門。')) as post:
            self.run_job(job)
        self.assertEqual(job.state, 'done', job.error)
        self.assertEqual(job.reply_message_id.author_id, self.bot)
        body = self.request_body(post)
        self.assertEqual(body['messages'][-1], {'role': 'user', 'content': '請問營業時間？'})
        self.assertTrue(body['messages'][0]['content'].startswith('You are the shop assistant.'))

    def test_portal_user_gets_a_reply(self):
        session = self.new_session(visitor=self.portal)
        self.post(session, self.portal)
        self.assertEqual(len(self.jobs(session)), 1)

    def test_guest_gets_a_reply(self):
        guest = self.new_guest()
        session = self.new_session(guest=guest)
        self.post_guest(session, guest)
        job = self.jobs(session)
        self.assertEqual(len(job), 1)
        with patch(BACKEND_POST, return_value=openai_ok('您好')):
            self.run_job(job)
        self.assertEqual(job.state, 'done', job.error)

    def test_operator_comment_pauses_and_creates_no_job(self):
        session = self.new_session(visitor=self.visitor)
        before = fields.Datetime.now()
        self.post(session, self.operator, '我來幫您')
        self.assertFalse(self.jobs(session))
        self.assertTrue(session._woow_ai_is_paused())
        self.assertGreaterEqual(session.woow_ai_paused_until, before + timedelta(minutes=29))
        self.assertLessEqual(session.woow_ai_paused_until, fields.Datetime.now() + timedelta(minutes=30))
        self.post(session, self.visitor, '好的')
        self.assertFalse(self.jobs(session))

    def test_operator_joining_does_not_pause(self):
        session = self.new_session(visitor=self.visitor)
        self.env['discuss.channel.member'].create({'channel_id': session.id, 'partner_id': self.manager_user.partner_id.id})
        self.assertFalse(session._woow_ai_is_paused())

    def test_no_pause_when_ai_is_off(self):
        self.livechat.woow_ai_enabled = False
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.operator, '我來幫您')
        self.assertFalse(session.woow_ai_paused_until)
        self.post(session, self.visitor)
        self.assertFalse(self.jobs(session))

    def test_bots_neither_trigger_nor_pause(self):
        other = self.env['woow.ai.contact'].create({'name': 'Other AI', 'endpoint_url': 'https://x.example.com/v1'})
        session = self.new_session(visitor=self.visitor)
        for author in (self.bot, other.partner_id, self.env.ref('base.partner_root')):
            self.post(session, author, '我是機器人')
        self.assertFalse(self.jobs(session))
        self.assertFalse(session.woow_ai_paused_until)

    def test_chatbot_operator_is_a_bot(self):
        chatbot = self.env['res.partner'].create({'name': 'Chatbot'})
        session = self.new_session(visitor=self.visitor, operator=chatbot)
        self.post(session, chatbot, '歡迎')
        self.assertFalse(self.jobs(session))
        self.assertFalse(session.woow_ai_paused_until)

    def test_notifications_do_not_trigger(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '我要找真人客服，都沒人回', message_type='notification')
        self.assertFalse(self.jobs(session))
        self.assertFalse(session.woow_ai_paused_until)

    def test_im_livechat_off_channel_without_contact_is_ignored(self):
        self.livechat.write({'woow_ai_enabled': False})
        self.contact.active = False
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor)
        self.assertFalse(self.jobs(session))

    def test_turning_ai_off_skips_queued_jobs(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor)
        job = self.jobs(session)
        self.livechat.woow_ai_enabled = False
        with patch(BACKEND_POST) as post:
            self.run_job(job)
        post.assert_not_called()
        self.assertEqual(job.state, 'skipped')


@tagged('woow_ai_contact_livechat', 'post_install', '-at_install')
class TestPause(LivechatCase):

    def test_pause_is_extended_never_shortened(self):
        session = self.new_session(visitor=self.visitor)
        long_until = session._woow_ai_pause(60)
        session._woow_ai_pause(1)
        self.assertEqual(session.woow_ai_paused_until, long_until)

    def test_pause_minutes_setting_and_clamp(self):
        self.livechat.woow_ai_pause_minutes = 5
        session = self.new_session(visitor=self.visitor)
        until = session._woow_ai_pause()
        self.assertAlmostEqual((until - fields.Datetime.now()).total_seconds(), 300, delta=5)

    def test_ai_resumes_after_the_pause(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.operator, '我來處理')
        session.woow_ai_paused_until = fields.Datetime.now() - timedelta(seconds=1)
        self.post(session, self.visitor, '還在嗎？')
        self.assertEqual(len(self.jobs(session)), 1)

    def test_other_session_not_paused(self):
        first = self.new_session(visitor=self.visitor)
        second = self.new_session(visitor=self.portal, name='Other')
        self.post(first, self.operator, '我來處理')
        self.post(second, self.portal)
        self.assertEqual(len(self.jobs(second)), 1)

    def test_pending_job_skipped_after_operator_post(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor)
        job = self.jobs(session)
        self.post(session, self.operator, '我來回答')
        with patch(BACKEND_POST) as post:
            self.run_job(job)
        post.assert_not_called()
        self.assertEqual(job.state, 'skipped')

    def test_operator_post_during_backend_call_drops_the_answer(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor)
        job = self.jobs(session)

        def answer(*args, **kwargs):
            self.post(session, self.operator, '我來回答')
            return openai_ok('AI 的答案')

        with patch(BACKEND_POST, side_effect=answer):
            self.run_job(job)
        self.assertEqual(job.state, 'skipped')
        self.assertFalse(self.bot_messages(session))


@tagged('woow_ai_contact_livechat', 'post_install', '-at_install')
class TestHandoff(LivechatCase):

    def test_default_phrases(self):
        self.assertEqual(self.livechat.woow_ai_handoff_keywords, DEFAULT_HANDOFF_KEYWORDS)
        hit = self.livechat._woow_ai_is_handoff_request
        for text in ('我要找真人客服', '請幫我轉人工', '可以找客服嗎', '人工客服在嗎', 'I want to TALK TO A  HUMAN',
                     'live agent please', 'a real person?'):
            self.assertTrue(hit(text), text)
        # L1: 「客服電話幾號」 was a false handoff with the 1.1.0 single words
        for text in ('客服電話幾號', '人工智慧燈泡多少錢', 'reagent', 'agents', 'an agent', 'operator', '請問價格', ''):
            self.assertFalse(hit(text), text)

    def test_custom_and_empty_keywords(self):
        self.assertTrue(is_handoff_request('請找主管', '專人，主管\nmanager'))
        self.assertTrue(is_handoff_request('your Manager', '專人，主管\nmanager'))
        self.assertFalse(is_handoff_request('managers', '專人，主管\nmanager'))
        self.livechat.woow_ai_handoff_keywords = False
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '我要找真人')
        self.assertEqual(self.jobs(session).kind, 'reply')
        self.assertFalse(session.woow_ai_paused_until)

    def test_keyword_in_html_body(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '<p>麻煩<b>轉人工</b>回覆</p>')
        self.assertEqual(self.jobs(session).kind, 'handoff')

    def test_keyword_handoff_flow(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '我要找真人客服')
        job = self.jobs(session)
        self.assertEqual(job.kind, 'handoff')
        self.assertTrue(session._woow_ai_is_paused())
        self.assertTrue(session.woow_ai_pause_notice_sent)
        with patch(BACKEND_POST) as post, \
                patch.object(type(self.env['res.partner']), '_bus_send') as bus:
            self.run_job(job)
        post.assert_not_called()
        self.assertEqual(job.state, 'done', job.error)
        bot_messages = self.bot_messages(session)
        self.assertEqual(len(bot_messages), 1)
        self.assertIn(DEFAULT_HANDOFF_MESSAGE, bot_messages.body)
        alerts = self.odoobot_messages(self.operator)
        self.assertEqual(len(alerts), 1)
        self.assertIn('/ai on', alerts.body)
        self.assertIn('discuss.channel_%s' % session.id, alerts.body)
        notifications = self.notifications(bus)
        self.assertEqual(len(notifications), 1)
        self.assertEqual((notifications[0]['sticky'], notifications[0]['type']), (True, 'warning'))
        # still paused: asking again sends neither a second handoff nor a paused notice
        self.post(session, self.visitor, '真人客服在嗎')
        self.assertEqual(len(self.jobs(session)), 1)
        self.assertEqual(len(self.bot_messages(session)), 1)

    def test_guest_handoff(self):
        guest = self.new_guest()
        session = self.new_session(guest=guest)
        self.post_guest(session, guest, '請幫我轉人工')
        job = self.jobs(session)
        self.assertEqual(job.kind, 'handoff')
        self.run_job(job)
        self.assertIn(DEFAULT_HANDOFF_MESSAGE, self.bot_messages(session).body)

    def test_custom_handoff_message(self):
        self.livechat.write({'woow_ai_handoff_keywords': '專人', 'woow_ai_handoff_message': '好的，專人馬上來。'})
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '請幫我找專人')
        self.run_job(self.jobs(session))
        self.assertIn('好的，專人馬上來。', self.bot_messages(session).body)

    def test_handoff_without_backend_key(self):
        self.contact.action_clear_api_key()
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '找客服')
        job = self.run_job(self.jobs(session))
        self.assertEqual(job.state, 'done')
        self.assertTrue(self.bot_messages(session))

    def test_handoff_supersedes_pending_reply(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '請問營業時間')
        self.post(session, self.visitor, '算了，找真人')
        reply_job, handoff_job = self.jobs(session)
        self.assertEqual((reply_job.kind, handoff_job.kind), ('reply', 'handoff'))
        with patch(BACKEND_POST) as post:
            self.Job._run_chain_in_transaction(reply_job.id)
        post.assert_not_called()
        self.assertEqual((reply_job.state, handoff_job.state), ('skipped', 'done'))
        self.assertEqual(len(self.bot_messages(session)), 1)

    def test_handoff_not_repeated_by_recovery(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '真人客服')
        job = self.run_job(self.jobs(session))
        self.run_job(job)
        self.Job._cron_recover_jobs(commit=False)
        self.assertEqual(len(self.bot_messages(session)), 1)

    def test_handoff_skipped_when_ai_turned_off(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '真人客服')
        job = self.jobs(session)
        self.livechat.woow_ai_enabled = False
        self.run_job(job)
        self.assertEqual(job.state, 'skipped')
        self.assertFalse(self.bot_messages(session))


@tagged('woow_ai_contact_livechat', 'post_install', '-at_install')
class TestPausedNoticeAndResume(LivechatCase):

    def _operator_message_minutes_ago(self, session, minutes):
        message = self.post(session, self.operator, '我來處理')
        message.sudo().write({'date': fields.Datetime.now() - timedelta(minutes=minutes)})
        return message

    def test_paused_notice_once_when_nobody_answers(self):
        session = self.new_session(visitor=self.visitor)
        self._operator_message_minutes_ago(session, 10)  # the operator answered once, then left
        with patch.object(type(self.env['res.partner']), '_bus_send') as bus:
            self.post(session, self.visitor, '還有人在嗎？')
        self.assertFalse(self.jobs(session))
        notices = self.bot_messages(session)
        self.assertEqual(len(notices), 1)
        self.assertIn(DEFAULT_PAUSED_MESSAGE, notices.body)
        self.assertTrue(session.woow_ai_pause_notice_sent)
        self.assertEqual(len(self.notifications(bus)), 1)
        self.assertIn('/ai on', self.odoobot_messages(self.operator)[-1:].body)
        self.post(session, self.visitor, '哈囉？')
        self.assertEqual(len(self.bot_messages(session)), 1)

    def test_no_paused_notice_while_an_operator_chats(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.operator, '我來處理')
        self.post(session, self.visitor, '好的謝謝')
        self.assertFalse(self.bot_messages(session))
        self.assertFalse(session.woow_ai_pause_notice_sent)

    def test_empty_paused_notice_is_off(self):
        self.livechat.woow_ai_paused_message = False
        session = self.new_session(visitor=self.visitor)
        self._operator_message_minutes_ago(session, 10)
        self.post(session, self.visitor, '還有人在嗎？')
        self.assertFalse(self.bot_messages(session))

    def test_new_pause_allows_a_new_notice(self):
        session = self.new_session(visitor=self.visitor)
        session.write({'woow_ai_paused_until': fields.Datetime.now() - timedelta(minutes=1),
                       'woow_ai_pause_notice_sent': True})
        session._woow_ai_pause()
        self.assertFalse(session.woow_ai_pause_notice_sent)

    def test_resume_command_answers_the_waiting_visitor(self):
        session = self.new_session(visitor=self.visitor)
        self._operator_message_minutes_ago(session, 10)
        self.post(session, self.visitor, '還有人在嗎？')
        self.assertTrue(session._woow_ai_is_paused())
        with patch.object(type(self.env['res.users']), '_bus_send_transient_message') as transient:
            session.with_user(self.operator_user).execute_command_woow_ai(body='/ai on')
        self.assertFalse(session._woow_ai_is_paused())
        job = self.jobs(session)
        self.assertEqual((len(job), job.kind), (1, 'reply'))
        self.assertEqual(job.message_id.body, '<p>還有人在嗎？</p>')
        self.assertEqual(transient.call_count, 1)
        with patch.object(type(self.env['res.users']), '_bus_send_transient_message'):
            session.with_user(self.operator_user).execute_command_woow_ai(body='/ai off')
            self.assertTrue(session._woow_ai_is_paused())
            session.with_user(self.operator_user).execute_command_woow_ai(body='/ai')

    def test_resume_button(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.operator, '我來處理')
        session.with_user(self.operator_user).action_woow_ai_resume()
        self.assertFalse(session.woow_ai_paused_until)
        self.assertFalse(self.jobs(session))  # the last message is the operator's: nothing to answer

    def test_visitors_cannot_resume(self):
        session = self.new_session(visitor=self.portal)
        self.post(session, self.operator, '我來處理')
        with self.assertRaises(AccessError):
            session.with_user(self.portal_user).execute_command_woow_ai(body='/ai on')
        with self.assertRaises(AccessError):
            session.with_user(self.portal_user).action_woow_ai_resume()
        self.assertTrue(session._woow_ai_is_paused())


@tagged('woow_ai_contact_livechat', 'post_install', '-at_install')
class TestFailureAlerts(LivechatCase):

    def test_backend_error_tells_visitor_and_alerts_operators(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor)
        job = self.jobs(session)
        with patch(BACKEND_POST, return_value=http_response(500, {'error': 'boom'})), \
                patch.object(type(self.env['res.partner']), '_bus_send') as bus:
            self.run_job(job)
        self.assertEqual(job.state, 'failed')
        self.assertTrue(self.bot_messages(session))  # the error message
        alert = self.odoobot_messages(self.operator)
        self.assertEqual(len(alert), 1)
        self.assertIn('HTTP 500', alert.body)
        self.assertEqual([n['type'] for n in self.notifications(bus)], ['danger'])

    def test_job_too_old_alerts_operators(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor)
        job = self.jobs(session)
        self.env.flush_all()
        self.env.cr.execute("UPDATE woow_ai_job SET create_date = create_date - interval '2 hours' WHERE id = %s",
                            (job.id,))
        job.invalidate_recordset()
        with patch.object(type(self.env['res.partner']), '_bus_send'):
            self.run_job(job)
        self.assertEqual(job.state, 'failed')
        self.assertFalse(self.bot_messages(session))
        self.assertTrue(self.odoobot_messages(self.operator))

    def test_alert_recipients_fall_back_to_channel_operators_and_managers(self):
        chatbot = self.env['res.partner'].create({'name': 'Chatbot'})
        session = self.new_session(visitor=self.visitor, operator=chatbot)
        self.assertEqual(session._woow_ai_alert_recipients(), self.operator)
        self.livechat.user_ids = False
        recipients = session._woow_ai_alert_recipients()
        self.assertIn(self.manager_user.partner_id, recipients)
        self.assertNotIn(self.bot, recipients)
