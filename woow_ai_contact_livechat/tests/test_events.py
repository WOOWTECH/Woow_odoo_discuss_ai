# -*- coding: utf-8 -*-
"""Conversation events: the handoff / paused notices are facts for the model,
not its own words (real-LLM acceptance 10-31: after "立即恢復 AI" the model
said "先前說『已通知客服』並不正確"); operator messages are labelled user
turns; pauses and resumes by operators are events too."""

from datetime import timedelta
from unittest.mock import patch

from psycopg2 import errors

from odoo import fields
from odoo.tests.common import tagged

from odoo.addons.woow_ai_contact_livechat.models.im_livechat_channel import (DEFAULT_HANDOFF_MESSAGE,
                                                                             DEFAULT_PAUSED_MESSAGE)

from .common import BACKEND_POST, TEST_KEY, LivechatCase, http_response, openai_ok



@tagged('woow_ai_contact_livechat', 'post_install', '-at_install')
class TestConversationEvents(LivechatCase):

    def events(self, session):
        return self.env['woow.ai.livechat.event'].search([('channel_id', '=', session.id)], order='id')

    def handoff_then_resume(self, session):
        """The acceptance scenario E: a question, a handoff, a visitor waiting, 立即恢復 AI."""
        self.post(session, self.visitor, '燈泡多少錢？')
        with patch(BACKEND_POST, return_value=openai_ok('A1 燈泡 NT$1,520。')):
            self.run_job(self.jobs(session))
        self.post(session, self.visitor, '我想找真人客服')
        handoff = self.jobs(session)[-1]
        self.assertEqual(handoff.kind, 'handoff')
        with patch.object(type(self.env['res.partner']), '_bus_send'):
            self.run_job(handoff)
        self.post(session, self.visitor, '有人在嗎？')  # paused: no job, no second notice
        self.assertEqual(len(self.jobs(session)), 2)
        session.with_user(self.operator_user).action_woow_ai_resume()
        reply = self.jobs(session)[-1]
        self.assertEqual((reply.kind, reply.message_id.body), ('reply', '<p>有人在嗎？</p>'))
        return handoff, reply

    def test_handoff_and_resume_are_facts_not_assistant_turns(self):
        session = self.new_session(visitor=self.visitor)
        handoff, reply = self.handoff_then_resume(session)
        events = self.events(session)
        self.assertEqual(events.mapped('kind'), ['handoff', 'resume'])
        notice = handoff.reply_message_id
        self.assertIn(DEFAULT_HANDOFF_MESSAGE, notice.body)
        self.assertEqual((events[0].message_id, events[0].trigger_message_id, events[0].minutes),
                         (notice, handoff.message_id, 30))
        self.assertEqual(events[1].user_id, self.operator_user)

        with patch(BACKEND_POST, return_value=openai_ok('在的，請問需要什麼協助？')) as post:
            self.run_job(reply)
        self.assertEqual(reply.state, 'done', reply.error)
        messages = self.request_body(post)['messages']
        turns = [(m['role'], m['content']) for m in messages[1:]]
        self.assertEqual(turns, [
            ('user', '燈泡多少錢？'),
            ('assistant', 'A1 燈泡 NT$1,520。'),
            ('user', '我想找真人客服'),
            ('user', '有人在嗎？'),
        ])  # the handoff notice is not an assistant turn any more
        system = messages[0]['content']
        self.assertIn('Conversation events recorded by Odoo', system)
        self.assertIn('not messages written by you', system)
        self.assertIn('訪客要求真人客服（訪客說：「我想找真人客服」）', system)
        self.assertIn('轉接通知「%s」' % DEFAULT_HANDOFF_MESSAGE, system)
        self.assertIn('已通知真人客服人員（客服已收到通知）', system)
        self.assertIn('AI 暫停回覆 30 分鐘', system)
        self.assertIn('客服人員「客服小明」恢復了 AI 回覆', system)
        self.assertLess(system.index('訪客要求真人客服'), system.index('恢復了 AI 回覆'))
        # the visitor's identity block comes before the events, the shop prompt first
        self.assertTrue(system.startswith('You are the shop assistant.'))

    def test_n8n_gets_events_and_no_notice_in_history(self):
        self.contact.write({'backend_type': 'n8n', 'endpoint_url': 'https://n8n.example.com/webhook/lc',
                            'api_key': TEST_KEY})
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '我想找真人客服')
        with patch.object(type(self.env['res.partner']), '_bus_send'):
            self.run_job(self.jobs(session))
        self.post(session, self.visitor, '有人在嗎？')
        session.with_user(self.operator_user).action_woow_ai_resume()
        with patch(BACKEND_POST, return_value=http_response(200, {'reply': '在的'})) as post:
            self.run_job(self.jobs(session)[-1])
        payload = self.request_body(post)
        self.assertEqual([e['kind'] for e in payload['events']], ['handoff', 'resume'])
        self.assertTrue(payload['events'][0]['time'].endswith('Z'))
        self.assertIn('已通知真人客服人員', payload['events'][0]['text'])
        texts = [e['text'] for e in payload['history']] + [payload['message']['text']]
        self.assertNotIn(DEFAULT_HANDOFF_MESSAGE, ' '.join(texts))
        self.assertEqual(payload['message']['text'], '有人在嗎？')

    def test_paused_notice_is_an_event(self):
        session = self.new_session(visitor=self.visitor)
        operator_message = self.post(session, self.operator, '我來處理')
        operator_message.sudo().write({'date': fields.Datetime.now() - timedelta(minutes=10)})
        with patch.object(type(self.env['res.partner']), '_bus_send'):
            self.post(session, self.visitor, '還有人在嗎？')
        notice = self.bot_messages(session)
        self.assertIn(DEFAULT_PAUSED_MESSAGE, notice.body)
        event = self.events(session)
        self.assertEqual((event.kind, event.message_id), ('paused_notice', notice))
        history = session._woow_ai_history(self.contact, session._woow_ai_context_messages(self.contact))
        self.assertEqual([(e['role'], e['text']) for e in history], [
            ('user', '[真人客服 客服小明] 我來處理'),
            ('user', '還有人在嗎？'),
        ])
        texts = [e['text'] for e in session._woow_ai_conversation_events(self.contact, history)]
        self.assertEqual(len(texts), 1)
        self.assertIn('等候提示「%s」' % DEFAULT_PAUSED_MESSAGE, texts[0])

    def test_operator_pause_and_resume_commands(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '你好')
        with patch.object(type(self.env['res.users']), '_bus_send_transient_message'):
            session.with_user(self.operator_user).execute_command_woow_ai(body='/ai off')
            session.with_user(self.operator_user).execute_command_woow_ai(body='/ai on')
            session.with_user(self.operator_user).execute_command_woow_ai(body='/ai on')  # not paused: no event
        events = self.events(session)
        self.assertEqual(events.mapped('kind'), ['pause', 'resume'])
        self.assertEqual(events.user_id, self.operator_user)
        self.assertEqual(events[0].minutes, 30)
        session.with_user(self.operator_user).action_woow_ai_pause()
        self.assertEqual(self.events(session)[-1].kind, 'pause')
        texts = [e['text'] for e in session._woow_ai_conversation_events(self.contact, [])]
        self.assertIn('客服人員「客服小明」暫停了 AI 回覆 30 分鐘。', texts)

    def test_event_concurrency_errors_reach_odoo_retry(self):
        """A visitor and an operator writing at the same time: a concurrent update
        raised while recording an event aborts the transaction; it must reach
        Odoo's request retry instead of being logged and swallowed."""
        session = self.new_session(visitor=self.visitor)
        Event = type(self.env['woow.ai.livechat.event'])
        for exc in (errors.SerializationFailure, errors.LockNotAvailable, errors.DeadlockDetected):
            with self.subTest(exc=exc.__name__), patch.object(Event, 'create', side_effect=exc('concurrent update')):
                with self.assertRaises(exc):
                    session._woow_ai_log_event('resume')
        with patch.object(Event, 'create', side_effect=ValueError('other')), \
                self.assertLogs('odoo.addons.woow_ai_contact_livechat.models.discuss_channel', level='ERROR'):
            self.assertFalse(session._woow_ai_log_event('resume'), 'other errors: logged, never fail the caller')

    def test_events_older_than_the_context_are_left_out(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '舊問題')
        old = session._woow_ai_log_event('resume')
        self.env.flush_all()
        self.env.cr.execute("UPDATE woow_ai_livechat_event SET create_date = create_date - interval '1 hour' "
                            "WHERE id = %s", (old.id,))
        self.env.cr.execute("UPDATE mail_message SET create_date = create_date - interval '2 hours' "
                            "WHERE model = 'discuss.channel' AND res_id = %s", (session.id,))
        self.env.invalidate_all()
        self.post(session, self.visitor, '新問題')
        self.contact.max_history = 1  # the context starts at '新問題'
        context = session._woow_ai_context_messages(self.contact)
        history = session._woow_ai_history(self.contact, context)
        self.assertEqual([e['text'] for e in history], ['新問題'])
        self.assertEqual(session._woow_ai_conversation_events(self.contact, history), [])
        self.contact.max_history = 50
        history = session._woow_ai_history(self.contact, session._woow_ai_context_messages(self.contact))
        self.assertEqual(len(session._woow_ai_conversation_events(self.contact, history)), 1)

    def test_deleted_notice_keeps_the_event(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '轉人工客服')
        with patch.object(type(self.env['res.partner']), '_bus_send'):
            self.run_job(self.jobs(session))
        event = self.events(session)
        event.message_id.unlink()
        self.env.invalidate_all()
        self.assertFalse(event.message_id)
        texts = [e['text'] for e in session._woow_ai_conversation_events(self.contact, [])]
        self.assertIn('轉接通知「已通知客服」', texts[0])
