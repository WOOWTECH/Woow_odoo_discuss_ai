# -*- coding: utf-8 -*-
"""The backend decides the handoff (owner decision 40; QA D11/D12: 「可以轉接專人嗎」
「我要跟人講話」 missed the keyword list and the AI then said 「我無法轉接」):
n8n answers ``"handoff": true``, openai backends (LiteLLM, Hermes) and n8n
``output`` end the reply with ``[[handoff]]``; keywords are an optional pre-filter."""

from unittest.mock import patch

from odoo import Command
from odoo.tests.common import tagged

from odoo.addons.woow_ai_contact_livechat.models.discuss_channel import HANDOFF_SYSTEM_LINE, strip_handoff_marker
from odoo.addons.woow_ai_contact_livechat.models.im_livechat_channel import DEFAULT_HANDOFF_MESSAGE

from .common import BACKEND_POST, TEST_KEY, LivechatCase, http_response, openai_ok


@tagged('woow_ai_contact_livechat', 'post_install', '-at_install')
class TestBackendHandoff(LivechatCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # decision 40 default: no keyword pre-filter, the backend decides
        cls.livechat.woow_ai_handoff_keywords = False
        cls.n8n = cls.env['woow.ai.contact'].create({
            'name': 'n8n 客服', 'backend_type': 'n8n', 'endpoint_url': 'https://n8n.example.com/webhook/lc',
            'api_key': TEST_KEY, 'allowed_group_ids': [Command.clear()],
        })

    def events(self, session):
        return self.env['woow.ai.livechat.event'].search([('channel_id', '=', session.id)], order='id')

    def ask(self, text, response, contact=None, session=None):
        """A visitor message answered by the mocked backend; returns (session, job, post mock, bus mock)."""
        if contact:
            self.livechat.woow_ai_contact_id = contact
        session = session or self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, text)
        job = self.jobs(session)[-1]
        self.assertEqual(job.kind, 'reply')  # no keyword: the backend is asked
        with patch(BACKEND_POST, return_value=response) as post, \
                patch.object(type(self.env['res.partner']), '_bus_send') as bus:
            self.run_job(job)
        return session, job, post, bus

    def assert_handed_over(self, session, job, bus, reason=None):
        self.assertEqual(job.state, 'done', job.error)
        self.assertTrue(session._woow_ai_is_paused())
        self.assertTrue(session.woow_ai_pause_notice_sent)
        event = self.events(session)
        self.assertEqual((event.kind, event.source, event.trigger_message_id, event.minutes, event.reason or None),
                         ('handoff', 'backend', job.message_id, 30, reason))
        self.assertEqual(len(self.notifications(bus)), 1)
        self.assertIn('/odoo/discuss?active_id=discuss.channel_%s' % session.id,
                      self.odoobot_messages(self.operator)[-1:].body)
        # paused afterwards: the next visitor message gets no job
        self.post(session, self.visitor, '有人在嗎？')
        self.assertEqual(len(self.jobs(session)), 1)
        return event

    # --- n8n "handoff" field ---

    def test_n8n_handoff_with_text(self):
        session, job, _post, bus = self.ask('可以轉接專人嗎', http_response(200, {
            'output': '好的，我幫您轉接專人，請稍候。', 'handoff': True, 'handoff_reason': '訪客要求專人'}),
            contact=self.n8n)
        replies = self.bot_messages(session, self.n8n.partner_id)
        self.assertEqual(replies.mapped('body'), ['<p>好的，我幫您轉接專人，請稍候。</p>'])  # no fixed notice on top
        self.assertEqual(job.reply_message_id, replies)
        event = self.assert_handed_over(session, job, bus, reason='訪客要求專人')
        self.assertFalse(event.message_id)  # the backend's own words stay in the history
        self.assertIn('訪客要求專人', self.odoobot_messages(self.operator)[-1].body)

    def test_n8n_handoff_without_text_posts_the_handoff_message(self):
        session, job, _post, bus = self.ask('我要跟人講話', http_response(200, {'handoff': True}), contact=self.n8n)
        notice = self.bot_messages(session, self.n8n.partner_id)
        self.assertEqual(notice.mapped('body'), ['<p>%s</p>' % DEFAULT_HANDOFF_MESSAGE])
        event = self.assert_handed_over(session, job, bus)
        self.assertEqual(event.message_id, notice)  # a notice: left out of the history like a keyword one

    def test_n8n_item_list_and_output_marker(self):
        session, job, _post, bus = self.ask('我要跟人講話', http_response(200, [{'handoff': True, 'replies': ['馬上轉接']}]),
                                            contact=self.n8n)
        self.assertEqual(self.bot_messages(session, self.n8n.partner_id).mapped('body'), ['<p>馬上轉接</p>'])
        self.assert_handed_over(session, job, bus)
        # a Chat Trigger workflow can only answer "output": the marker works there too
        session, job, _post, bus = self.ask('可以轉接專人嗎', http_response(200, [{'output': '好的，請稍候。\n[[handoff]]'}]))
        self.assertEqual(self.bot_messages(session, self.n8n.partner_id).mapped('body'), ['<p>好的，請稍候。</p>'])
        self.assert_handed_over(session, job, bus)

    def test_n8n_payload_says_handoff_available(self):
        _session, _job, post, _bus = self.ask('你好', http_response(200, {'output': '您好'}), contact=self.n8n)
        self.assertIs(self.request_body(post)['channel']['livechat']['handoff_available'], True)
        self.livechat.user_ids = [Command.clear()]
        _session, _job, post, _bus = self.ask('你好', http_response(200, {'output': '您好'}))
        self.assertIs(self.request_body(post)['channel']['livechat']['handoff_available'], False)

    # --- openai marker ---

    def test_openai_marker_stripped_and_handed_over(self):
        session, job, post, bus = self.ask('可以轉接專人嗎', openai_ok('好的，我幫您轉接真人客服，請稍候。\n\n[[HANDOFF]]'))
        reply = self.bot_messages(session)
        self.assertEqual(reply.mapped('body'), ['<p>好的，我幫您轉接真人客服，請稍候。</p>'])
        self.assertNotIn('handoff', reply.body.lower())
        event = self.assert_handed_over(session, job, bus)
        self.assertFalse(event.message_id)
        # the system line told the model how to hand over (operators exist)
        system = self.request_body(post)['messages'][0]['content']
        self.assertTrue(system.startswith('You are the shop assistant.'))
        self.assertTrue(system.endswith(HANDOFF_SYSTEM_LINE))
        self.assertEqual(system.count(HANDOFF_SYSTEM_LINE), 1)

    def test_openai_marker_only_posts_the_handoff_message(self):
        self.livechat.woow_ai_handoff_message = '好的，客服馬上來。'
        session, job, _post, bus = self.ask('我要跟人講話', openai_ok('[[handoff]]'))
        self.assertEqual(self.bot_messages(session).mapped('body'), ['<p>好的，客服馬上來。</p>'])
        self.assertEqual(self.assert_handed_over(session, job, bus).message_id, self.bot_messages(session))

    def test_marker_variants(self):
        for text, expected in (
            ('好的 [[handoff]]', '好的'),
            ('好的，請稍候。[[Handoff]]', '好的，請稍候。'),
            ('第一行\n[[handoff]]\n\n\n第二行', '第一行\n\n第二行'),
            ('[[ handoff ]]\n請稍候', '請稍候'),
            ('請稍候 `[[handoff]]`', '請稍候'),
            ('請稍候 \\[\\[handoff\\]\\]', '請稍候'),
        ):
            self.assertEqual(strip_handoff_marker(text), (expected, True), text)
        for text in ('人工智慧是什麼', '[handoff]', '[[hand off]]', ''):
            self.assertEqual(strip_handoff_marker(text), (text, False), text)

    def test_no_marker_no_handoff(self):
        session, job, post, bus = self.ask('人工智慧是什麼', openai_ok('人工智慧是讓電腦學習與推理的技術。'))
        self.assertEqual(job.state, 'done', job.error)
        self.assertFalse(session.woow_ai_paused_until)
        self.assertFalse(self.events(session))
        self.assertFalse(self.notifications(bus))

    def test_handoff_event_is_a_fact_for_the_model(self):
        session, job, _post, _bus = self.ask('可以轉接專人嗎', openai_ok('好的，幫您轉接。[[handoff]]'))
        session.with_user(self.operator_user).action_woow_ai_resume()
        self.post(session, self.visitor, '剛剛有轉接嗎？')
        with patch(BACKEND_POST, return_value=openai_ok('有的。')) as post:
            self.run_job(self.jobs(session)[-1])
        body = self.request_body(post)
        system = body['messages'][0]['content']
        self.assertIn('AI 判斷訪客要找真人客服（訪客說：「可以轉接專人嗎」），你（AI）在回覆中請求轉接', system)
        # the model's own reply (marker stripped) stays its assistant turn
        self.assertIn({'role': 'assistant', 'content': '好的，幫您轉接。'}, body['messages'])

    def test_no_system_line_without_operators(self):
        self.livechat.user_ids = [Command.clear()]
        _session, _job, post, _bus = self.ask('你好', openai_ok('您好'))
        self.assertNotIn('[[handoff]]', self.request_body(post)['messages'][0]['content'])

    def test_answer_dropped_when_an_operator_wrote_meanwhile(self):
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '可以轉接專人嗎')
        job = self.jobs(session)

        def operator_writes(*_args, **_kwargs):
            self.post(session, self.operator, '我來了')
            return openai_ok('好的 [[handoff]]')

        with patch(BACKEND_POST, side_effect=operator_writes), \
                patch.object(type(self.env['res.partner']), '_bus_send') as bus:
            self.run_job(job)
        self.assertEqual(job.state, 'skipped')
        self.assertFalse(self.bot_messages(session))
        self.assertFalse(self.events(session))
        self.assertFalse(self.notifications(bus))

    # --- keywords: optional pre-filter ---

    def test_default_keywords_empty(self):
        channel = self.env['im_livechat.channel'].create({'name': 'New site'})
        self.assertFalse(channel.woow_ai_handoff_keywords)
        self.assertFalse(channel._woow_ai_is_handoff_request('我要找真人客服'))

    def test_configured_keyword_still_hands_over_without_backend(self):
        self.livechat.woow_ai_handoff_keywords = '找真人'
        session = self.new_session(visitor=self.visitor)
        self.post(session, self.visitor, '我要找真人')
        job = self.jobs(session)
        self.assertEqual(job.kind, 'handoff')
        with patch(BACKEND_POST) as post, patch.object(type(self.env['res.partner']), '_bus_send'):
            self.run_job(job)
        post.assert_not_called()
        self.assertEqual(self.events(session).source, 'keyword')
        self.assertEqual(self.bot_messages(session).mapped('body'), ['<p>%s</p>' % DEFAULT_HANDOFF_MESSAGE])
