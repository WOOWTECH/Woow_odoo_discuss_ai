# -*- coding: utf-8 -*-
"""Backend protocol details: streaming, retries, message normalisation, inline
image budget, identity hooks, reply data URIs / MEDIA, call log usage."""
import base64
import json
import time
from datetime import datetime
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import ReadTimeoutError

from odoo.tests.common import _super_send, tagged
from odoo.tools import mute_logger

from odoo.addons.woow_ai_contact.models import ai_contact_backend
from odoo.addons.woow_ai_contact.models.ai_contact_backend import (BackendError, normalize_turns, redact,
                                                                   strip_think)

from .common import (BACKEND_POST, BACKEND_SLEEP, TINY_PNG, AiContactCase, LocalServer, delta, free_port,
                     http_response, openai_ok, refused, reset_after_send, slow_sse_handler, sse_event, sse_response)

BACKEND = 'odoo.addons.woow_ai_contact.models.ai_contact_backend'
PNG_B64 = base64.b64encode(TINY_PNG).decode()


def run(job):
    job._run_job(commit=False)
    return job


def call_log(env, job):
    return env['woow.ai.call.log'].search([('job_id', '=', job.id)])


@tagged('woow_ai_contact')
class TestStreaming(AiContactCase):

    def ask(self, response, text='hi', contact=None):
        chat = self.chat(self.internal, contact)
        self.post(chat, self.internal, text)
        job = self.jobs(chat)[-1]
        with patch(BACKEND_POST, return_value=response) as post:
            run(job)
        return chat, job, post

    def test_sse_reply_assembled(self):
        usage = {'prompt_tokens': 12, 'completion_tokens': 5, 'total_tokens': 17}
        body = (': keepalive\n\n'
                + sse_event({'tool': 'odoo.search', 'status': 'running'}, event='hermes.tool.progress')
                + sse_event({'choices': [{'index': 0, 'delta': {'reasoning_content': 'let me think'}}]})
                + sse_event(delta('<think>hidden</think>Hel'))
                + sse_event(delta('lo 你好'))
                + sse_event(delta(None, 'stop'))
                + sse_event({'choices': [], 'usage': usage})
                + 'data: [DONE]\n\n').encode()
        # chunks cut in the middle of lines and of a multi-byte character
        cut = body.index('你'.encode()) + 1
        chunks = [body[:7], body[7:60], body[60:cut], body[cut:]]
        chat, job, post = self.ask(sse_response(chunks))
        self.assertEqual(job.state, 'done', job.error)
        self.assertIn('Hello 你好', job.reply_message_id.body)
        self.assertNotIn('hidden', job.reply_message_id.body)
        self.assertNotIn('let me think', job.reply_message_id.body)
        self.assertNotIn('odoo.search', job.reply_message_id.body)
        self.assertEqual(post.call_args.kwargs['timeout'], (10, ai_contact_backend.STREAM_IDLE_TIMEOUT))
        log = call_log(self.env, job)
        self.assertEqual((log.status, log.prompt_tokens, log.completion_tokens, log.total_tokens),
                         ('ok', 12, 5, 17))

    def test_stream_asks_for_usage(self):
        # LiteLLM / OpenAI send the usage of a stream only when asked (acceptance 10-31: all 0)
        chat, job, post = self.ask(sse_response([sse_event(delta('ok', 'stop')), 'data: [DONE]\n\n']))
        body = json.loads(post.call_args.kwargs['data'])
        self.assertIs(body['stream'], True)
        self.assertEqual(body['stream_options'], {'include_usage': True})

    def test_no_stream_options_without_stream(self):
        self.contact.openai_stream = False
        chat, job, post = self.ask(openai_ok('not streamed'))
        self.assertNotIn('stream_options', json.loads(post.call_args.kwargs['data']))

    def test_sse_usage_only_chunk_as_litellm_sends_it(self):
        # LiteLLM 10-31: the finish chunk, then {"choices": [], "usage": {...}} (no "index"), then [DONE];
        # content chunks carry "usage": null or zero counts
        usage = {'completion_tokens': 9, 'prompt_tokens': 1698, 'total_tokens': 1707,
                 'completion_tokens_details': {'reasoning_tokens': 0},
                 'prompt_tokens_details': {'cached_tokens': 1408}}
        zero = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
        body = (sse_event(delta('PINEAPPLE', usage=None))
                + sse_event(delta('-77', usage=zero))
                + sse_event(delta(None, 'stop'))
                + sse_event({'id': 'x', 'object': 'chat.completion.chunk', 'choices': [], 'usage': usage})
                + sse_event({'choices': [], 'usage': zero})  # a trailing empty usage does not erase it
                + 'data: [DONE]\n\n')
        chat, job, _post = self.ask(sse_response([body]))
        self.assertEqual(job.state, 'done', job.error)
        self.assertIn('PINEAPPLE-77', job.reply_message_id.body)
        log = call_log(self.env, job)
        self.assertEqual((log.prompt_tokens, log.completion_tokens, log.total_tokens), (1698, 9, 1707))

    def test_usage_tokens_variants(self):
        tokens = ai_contact_backend.usage_tokens
        self.assertEqual(tokens({'input_tokens': 4, 'output_tokens': 6}), (4, 6, 10))
        self.assertEqual(tokens({'prompt_tokens': 4, 'completion_tokens': 6}), (4, 6, 10))
        self.assertEqual(tokens({'prompt_tokens': -1, 'completion_tokens': True, 'total_tokens': '9'}), (0, 0, 0))
        self.assertEqual(tokens(None), (0, 0, 0))

    def test_sse_idle_timeout_not_retried(self):
        idle = requests.ConnectionError(ReadTimeoutError(None, None, 'Read timed out.'))
        chat, job, post = self.ask(sse_response([': keepalive\n\n', sse_event(delta('par'))], error=idle))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(job.state, 'failed')
        self.assertEqual(call_log(self.env, job).status, 'timeout')
        self.assertIn('Sorry', job.reply_message_id.body)

    def test_sse_total_deadline(self):
        response = sse_response([sse_event(delta('a')), sse_event(delta('b')), 'data: [DONE]\n\n'])
        with self.assertRaises(BackendError) as caught:
            self.env['woow.ai.contact']._read_sse(response, time.time() - 1)
        self.assertEqual(caught.exception.status, 'timeout')

    def test_sse_cut_before_done(self):
        chat, job, _post = self.ask(sse_response([sse_event(delta('half an ans'))]))
        self.assertEqual(job.state, 'failed')
        self.assertEqual(call_log(self.env, job).status, 'invalid_response')

    def test_sse_error_chunk(self):
        chat, job, _post = self.ask(sse_response([sse_event({'error': {'message': 'upstream'}})]))
        self.assertEqual(job.state, 'failed')
        self.assertEqual(call_log(self.env, job).status, 'invalid_response')

    def test_stream_requested_but_json_answer(self):
        chat, job, post = self.ask(openai_ok('plain json', usage={'prompt_tokens': 3, 'completion_tokens': 2,
                                                                  'total_tokens': 5}))
        self.assertEqual(job.state, 'done', job.error)
        self.assertIn('plain json', job.reply_message_id.body)
        self.assertEqual(call_log(self.env, job).total_tokens, 5)

    def test_redirect_never_followed(self):
        # an https endpoint answering 307 to plain http: the conversation is not re-sent
        redirect = http_response(307, headers={'Location': 'http://other.example.com/steal'})
        chat, job, post = self.ask(redirect)
        self.assertEqual(post.call_count, 1)
        self.assertIs(post.call_args.kwargs['allow_redirects'], False)
        self.assertEqual(job.state, 'failed')
        log = call_log(self.env, job)
        self.assertEqual((log.status, log.http_status), ('http_error', 307))

    def test_stream_off(self):
        self.contact.openai_stream = False
        chat, job, post = self.ask(openai_ok('not streamed'))
        self.assertEqual(job.state, 'done')
        body = json.loads(post.call_args.kwargs['data'])
        self.assertNotIn('stream', body)
        self.assertFalse(post.call_args.kwargs['stream'])
        self.assertEqual(post.call_args.kwargs['timeout'], (10, 300))

    def test_stream_log_payload(self):
        self.contact.log_payload = True
        chat, job, _post = self.ask(sse_response([sse_event(delta('streamed text', 'stop')), 'data: [DONE]\n\n']))
        log = call_log(self.env, job)
        self.assertIn('streamed text', log.response_payload)
        self.assertIn('"stream":true', log.response_payload)


@tagged('woow_ai_contact')
class TestRetries(AiContactCase):

    def ask(self, side_effect, **contact_vals):
        if contact_vals:
            self.contact.write(contact_vals)
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'x')
        job = self.jobs(chat)[-1]
        with patch(BACKEND_POST, side_effect=side_effect) as post, patch(BACKEND_SLEEP) as sleep:
            run(job)
        return job, post, sleep

    def test_default_one_retry_on_429_and_503(self):
        self.assertEqual(self.contact.max_retries, 1)
        job, post, sleep = self.ask([http_response(429, text='slow down'), openai_ok('ok')])
        self.assertEqual(post.call_count, 2)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [3])
        self.assertEqual(job.state, 'done')
        job, post, sleep = self.ask([http_response(503), http_response(503)])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(job.state, 'failed')
        self.assertEqual(call_log(self.env, job).http_status, 503)

    def test_two_retries_backoff(self):
        job, post, sleep = self.ask([refused(), http_response(503), openai_ok('third')],
                                    max_retries=2)
        self.assertEqual(post.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [3, 10])
        self.assertEqual(job.state, 'done')

    def test_never_retried(self):
        for status in (500, 502, 504, 524):
            job, post, sleep = self.ask([http_response(status), openai_ok()], max_retries=2)
            self.assertEqual(post.call_count, 1, status)
            sleep.assert_not_called()
            self.assertEqual(job.state, 'failed')
        job, post, sleep = self.ask([requests.ReadTimeout('slow'), openai_ok()], max_retries=2)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(call_log(self.env, job).status, 'timeout')

    def test_no_retry(self):
        job, post, sleep = self.ask([refused(), openai_ok()], max_retries=0)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(job.state, 'failed')
        self.assertIn('connection failed', job.error)

    def test_connection_lost_after_send_not_retried(self):
        # the backend received the request, then reset / closed the connection:
        # it may be running tools, so the request is never sent again (decision 26)
        for error in (reset_after_send(), requests.ConnectionError('Connection aborted.')):
            job, post, sleep = self.ask([error, openai_ok('again')], max_retries=2)
            self.assertEqual(post.call_count, 1)
            sleep.assert_not_called()
            self.assertEqual(job.state, 'failed')
        # a connect timeout and an unknown host were never sent: retried
        job, post, sleep = self.ask([requests.ConnectTimeout('slow connect'), openai_ok('ok')])
        self.assertEqual((post.call_count, job.state), (2, 'done'))

    def test_retries_stay_within_timeout(self):
        # 5 s total: the 3 s wait fits, the 10 s one does not
        job, post, sleep = self.ask([http_response(429)] * 3, max_retries=2, timeout_seconds=5)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(job.state, 'failed')


@tagged('woow_ai_contact')
class TestOpenAiMessages(AiContactCase):

    def body_for(self, chat):
        job = self.jobs(chat)[-1]
        with patch(BACKEND_POST, return_value=openai_ok()) as post:
            run(job)
        return json.loads(post.call_args.kwargs['data'])

    def test_normalize_turns(self):
        u = lambda t: {'role': 'user', 'content': t}  # noqa: E731
        a = lambda t: {'role': 'assistant', 'content': t}  # noqa: E731
        self.assertEqual(normalize_turns([a('welcome'), u('q1'), a('a1'), u('q2')]),
                         [u('q1'), a('a1'), u('q2')])
        self.assertEqual(normalize_turns([u('q1'), a('a1'), u('q2'), a('push')]),
                         [u('q1'), a('a1'), a('push'), u('q2')])
        moved = normalize_turns([a('welcome'), u('q'), a('push 1'), a('push 2')])
        self.assertEqual(moved[-1], u('q'))
        self.assertEqual(moved[0]['role'], 'user')
        self.assertIn('push 1\n\npush 2', moved[0]['content'])
        self.assertEqual(normalize_turns([a('only bot')]), [])

    def test_last_message_is_always_user(self):
        self.contact.welcome_message = 'Welcome!'
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'q1')
        with patch(BACKEND_POST, return_value=openai_ok('a1')):
            run(self.jobs(chat))
        self.post(chat, self.internal, 'q2')
        # a push arrives before the job runs
        self.contact._post_text(chat, 'your order shipped')
        messages = self.body_for(chat)['messages']
        self.assertEqual([(m['role'], m['content']) for m in messages[1:]], [
            ('user', 'q1'), ('assistant', 'a1'), ('assistant', 'your order shipped'), ('user', 'q2')])

    def test_inline_image_count_and_budget(self):
        chat = self.chat(self.internal)
        atts = self.env['ir.attachment']
        for i in range(6):
            atts |= self.attachment(chat, self.internal, name='p%s.png' % i)
        self.post(chat, self.internal, 'six pictures', attachments=atts)
        content = self.body_for(chat)['messages'][-1]['content']
        images = [p for p in content if p['type'] == 'image_url']
        self.assertEqual(len(images), ai_contact_backend.INLINE_IMAGE_MAX_COUNT)
        self.assertEqual(content[0]['text'].count('[附件] '), 2)
        # total budget: only what fits is inlined, newest message first
        old = self.attachment(chat, self.internal, name='old.png')
        self.post(chat, self.internal, 'old', attachments=old)
        new = self.attachment(chat, self.internal, name='new.png')
        self.post(chat, self.internal, 'new', attachments=new)
        uri_len = len('data:image/png;base64,') + len(PNG_B64)
        with patch(BACKEND + '.INLINE_IMAGE_BUDGET', uri_len + 10):
            messages = self.body_for(chat)['messages']
        self.assertIsInstance(messages[-1]['content'], list)  # 'new' inlined
        self.assertIsInstance(messages[-2]['content'], str)  # 'old' over budget: a link line
        self.assertIn('[附件] old.png', messages[-2]['content'])

    def test_inline_image_type_sniffed(self):
        chat = self.chat(self.internal)
        fake = self.attachment(chat, self.internal, name='fake.png', raw=b'<html><script>x()</script></html>',
                               mimetype='image/png')
        self.assertEqual(fake.mimetype, 'image/png')
        self.post(chat, self.internal, 'look', attachments=fake)
        content = self.body_for(chat)['messages'][-1]['content']
        self.assertIsInstance(content, str)
        self.assertIn('[附件] fake.png', content)

    def test_long_message_capped(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'x' * 20000)
        content = self.body_for(chat)['messages'][-1]['content']
        self.assertLess(len(content), 16100)
        self.assertTrue(content.endswith('[truncated]'))

    def test_identity_and_prompt_hooks(self):
        Channel = type(self.env['discuss.channel'])
        guest = {'user_id': None, 'partner_id': None, 'name': 'Visitor 42', 'email': '', 'lang': 'en_US',
                 'tz': '', 'is_portal': False, 'company_id': None, 'is_guest': True, 'guest_id': 9}
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'hi')
        with patch.object(Channel, '_woow_ai_identity', autospec=True, return_value=guest), \
                patch.object(Channel, '_woow_ai_system_prompt', autospec=True, return_value='Bridge prompt'):
            body = self.body_for(chat)
        system = body['messages'][0]['content']
        self.assertTrue(system.startswith('Bridge prompt\n\n'))
        self.assertIn('- name: Visitor 42', system)
        self.assertIn('- account: website visitor (guest)', system)
        self.assertEqual(body['metadata']['odoo_is_guest'], 'true')
        self.assertEqual(body['metadata']['odoo_guest_id'], '9')
        self.assertNotIn('odoo_user_id', body['metadata'])
        # n8n uses the same hooks
        chat2 = self.chat(self.portal, self.n8n)
        self.post(chat2, self.portal, 'hi')
        with patch.object(Channel, '_woow_ai_identity', autospec=True, return_value=guest), \
                patch.object(Channel, '_woow_ai_system_prompt', autospec=True, return_value='Bridge prompt'), \
                patch(BACKEND_POST, return_value=http_response(200, {'reply': 'ok'})) as post:
            run(self.jobs(chat2))
        payload = json.loads(post.call_args.kwargs['data'])
        self.assertEqual(payload['system_prompt'], 'Bridge prompt')
        self.assertEqual(payload['user'], guest)

    def test_conversation_events_hook(self):
        # core: no events, no block, an empty list for n8n
        Channel = type(self.env['discuss.channel'])
        self.internal.tz = 'Asia/Taipei'
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'hi')
        self.assertNotIn('Conversation events', self.body_for(chat)['messages'][0]['content'])
        events = [{'date': datetime(2026, 10, 31, 15, 55), 'kind': 'handoff', 'text': '已轉接真人客服，客服已收到通知。'},
                  {'date': datetime(2026, 10, 31, 15, 58), 'kind': 'resume', 'text': 'AI 已恢復。'}]
        self.post(chat, self.internal, 'again')
        with patch.object(Channel, '_woow_ai_conversation_events', autospec=True, return_value=events):
            system = self.body_for(chat)['messages'][0]['content']
        block = system.split('\n\n')[-1].split('\n')
        self.assertTrue(block[0].startswith('Conversation events recorded by Odoo, oldest first (facts, not '
                                            'messages written by you; times in Asia/Taipei, now '))
        self.assertEqual(block[1:], ['- 2026-10-31 23:55 已轉接真人客服，客服已收到通知。', '- 2026-10-31 23:58 AI 已恢復。'])
        chat2 = self.chat(self.portal, self.n8n)
        self.post(chat2, self.portal, 'hi')
        with patch(BACKEND_POST, return_value=http_response(200, {'reply': 'ok'})) as post:
            run(self.jobs(chat2))
        self.assertEqual(json.loads(post.call_args.kwargs['data'])['events'], [])
        self.post(chat2, self.portal, 'again')
        with patch.object(Channel, '_woow_ai_conversation_events', autospec=True, return_value=events), \
                patch(BACKEND_POST, return_value=http_response(200, {'reply': 'ok'})) as post:
            run(self.jobs(chat2)[-1])
        self.assertEqual(json.loads(post.call_args.kwargs['data'])['events'][0],
                         {'time': '2026-10-31T15:55:00Z', 'kind': 'handoff', 'text': '已轉接真人客服，客服已收到通知。'})

    def test_n8n_user_keeps_email(self):
        chat = self.chat(self.portal, self.n8n)
        self.post(chat, self.portal, 'hi')
        with patch(BACKEND_POST, return_value=http_response(200, {'reply': 'ok'})) as post:
            run(self.jobs(chat))
        self.assertEqual(json.loads(post.call_args.kwargs['data'])['user']['email'], 'pat@example.com')


@tagged('woow_ai_contact')
class TestReplyContent(AiContactCase):

    def reply(self, content, contact_vals=None):
        if contact_vals:
            self.contact.write(contact_vals)
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'q')
        job = self.jobs(chat)[-1]
        with patch(BACKEND_POST, return_value=openai_ok(content)):
            run(job)
        return job

    def test_data_uri_images_attached_without_allowlist(self):
        job = self.reply('圖表 ![chart](data:image/png;base64,%s) 還有 data:image/png;base64,%s 完'
                         % (PNG_B64, PNG_B64), {'download_domains': False, 'log_payload': True})
        self.assertEqual(job.state, 'done', job.error)
        reply = job.reply_message_id
        self.assertEqual(sorted(reply.attachment_ids.mapped('name')), ['chart.png', 'image-2.png'])
        self.assertEqual(set(reply.attachment_ids.mapped('mimetype')), {'image/png'})
        self.assertNotIn('base64', reply.body)
        self.assertNotIn(PNG_B64[:20], reply.body)
        log = call_log(self.env, job)
        self.assertNotIn(PNG_B64, log.response_payload)
        self.assertIn('base64 characters omitted', log.response_payload)
        self.assertFalse(log.error)

    def test_data_uri_wrong_type_or_too_large(self):
        html = base64.b64encode(b'<html><script>alert(1)</script></html>').decode()
        big = base64.b64encode(TINY_PNG + b'0' * (1024 * 1024 + 10)).decode()
        job = self.reply('a ![x](data:image/png;base64,%s) b data:image/png;base64,%s c data:text/html;base64,%s'
                         % (html, big, html), {'max_download_mb': 1})
        self.assertEqual(job.state, 'done', job.error)
        reply = job.reply_message_id
        self.assertFalse(reply.attachment_ids)
        self.assertEqual(reply.body.count('An image in the reply could not be attached'), 3)
        self.assertNotIn(html, reply.body)
        self.assertIn('not attached', call_log(self.env, job).error)

    def test_media_path_replaced_and_logged(self):
        job = self.reply('報表做好了：MEDIA:/tmp/hermes/report.xlsx')
        body = job.reply_message_id.body
        self.assertNotIn('/tmp/hermes', body)
        self.assertNotIn('MEDIA:', body)
        self.assertIn('could not be delivered', body)
        self.assertIn('MEDIA', call_log(self.env, job).error)
        self.assertEqual(call_log(self.env, job).status, 'ok')

    def test_notes_in_user_language(self):
        self.env['res.lang']._activate_lang('zh_TW')
        self.env['ir.module.module']._load_module_terms(['woow_ai_contact'], ['zh_TW'], overwrite=True)
        self.internal.lang = 'zh_TW'
        job = self.reply('檔案：MEDIA:/tmp/a.xlsx')
        self.assertIn('AI 產生了檔案，但無法傳送', job.reply_message_id.body)

    def test_think_and_special_tokens(self):
        job = self.reply('<thinking>plan</thinking>答案<|eot_id|>')
        self.assertIn('答案', job.reply_message_id.body)
        self.assertNotIn('plan', job.reply_message_id.body)
        self.assertNotIn('eot_id', job.reply_message_id.body)

    def test_link_hosts_hook(self):
        Channel = type(self.env['discuss.channel'])
        with patch.object(Channel, '_woow_ai_link_hosts', autospec=True, return_value=['shop.example.com']):
            job = self.reply('[買](https://shop.example.com/p/1) [釣魚](https://evil.example.net/login)')
        body = job.reply_message_id.body
        self.assertIn('href="https://shop.example.com/p/1"', body)
        self.assertNotIn('evil.example.net', body)
        self.assertIn('釣魚', body)

    def test_redact(self):
        data = {'choices': [{'message': {'content': 'x data:image/png;base64,%s y' % PNG_B64}}],
                'reply': {'attachments': [{'data_base64': 'QUJD'}]}}
        redacted = redact(data)
        self.assertNotIn(PNG_B64, json.dumps(redacted))
        self.assertIn('data:image/png;base64,<%d base64 characters omitted>' % len(PNG_B64),
                      redacted['choices'][0]['message']['content'])
        self.assertEqual(redacted['reply']['attachments'][0]['data_base64'], '<4 base64 characters omitted>')
        self.assertIn(PNG_B64, json.dumps(data), 'the original is not modified')

    def test_strip_think(self):
        self.assertEqual(strip_think('<think>a</think>b'), 'b')
        self.assertEqual(strip_think('<THINKING>a</THINKING> b'), 'b')
        self.assertEqual(strip_think('b<thinking>never closed'), 'b')
        self.assertEqual(strip_think('reasoning without opening tag</think>answer'), 'answer')
        self.assertEqual(strip_think('x<|im_end|>'), 'x')
        self.assertEqual(strip_think('a <|not a token|> b'), 'a <|not a token|> b')


@tagged('woow_ai_contact')
class TestJobHooks(AiContactCase):

    def test_skip_when_contact_changed(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'q')
        job = self.jobs(chat)
        Channel = type(self.env['discuss.channel'])
        with patch.object(Channel, '_woow_ai_channel_contact', autospec=True, return_value=self.n8n), \
                patch(BACKEND_POST) as post:
            run(job)
        post.assert_not_called()
        self.assertEqual(job.state, 'skipped')
        self.assertIn('contact changed', job.error)
        self.assertFalse(self.bot_messages(chat))

    def test_contact_changed_while_answering(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'q')
        job = self.jobs(chat)
        Channel = type(self.env['discuss.channel'])
        original = Channel._woow_ai_channel_contact
        state = {'calls': 0}

        def turned_off(channel):
            state['calls'] += 1
            return original(channel) if state['calls'] == 1 else channel.env['woow.ai.contact']

        with patch.object(Channel, '_woow_ai_channel_contact', autospec=True, side_effect=turned_off), \
                patch(BACKEND_POST, return_value=openai_ok('late')):
            run(job)
        self.assertEqual(job.state, 'skipped')
        self.assertFalse(self.bot_messages(chat))

    @mute_logger('odoo.addons.woow_ai_contact.models.ai_job')
    def test_notify_failure_visitor_told(self):
        Job = type(self.env['woow.ai.job'])
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'q')
        job = self.jobs(chat)
        with patch.object(Job, '_notify_failure', autospec=True) as notify, \
                patch(BACKEND_POST, return_value=http_response(500)):
            run(job)
        self.assertEqual(notify.call_args.kwargs.get('visitor_told'), True)
        self.post(chat, self.internal, 'old')
        old = self.jobs(chat)[-1]
        self.env.cr.execute("UPDATE woow_ai_job SET create_date = create_date - interval '2 hours' WHERE id = %s",
                            (old.id,))
        old.invalidate_recordset()
        with patch.object(Job, '_notify_failure', autospec=True) as notify, patch(BACKEND_POST) as post:
            run(old)
        post.assert_not_called()
        self.assertEqual(notify.call_args.kwargs.get('visitor_told'), False)
        self.assertFalse(old.reply_message_id)


@tagged('woow_ai_contact')
class TestWire(AiContactCase):
    """Real sockets on 127.0.0.1: what requests does on the wire."""

    @classmethod
    def _request_handler(cls, s, r, **kw):
        # Odoo's test guard compares the timeout with an int; ours is a (connect, read) tuple
        if urlsplit(r.url).hostname == '127.0.0.1':
            return _super_send(s, r, **kw)
        return super()._request_handler(s, r, **kw)

    def test_wire_reset_after_full_request_not_retried(self):
        def close_after_reading(conn, _request):
            pass  # the whole request was read: close without an answer

        with LocalServer(close_after_reading) as server, patch(BACKEND_SLEEP) as sleep:
            with self.assertRaises(requests.ConnectionError):
                self.contact._http_post(server.url + '/v1/chat/completions', {'Content-Type': 'application/json'},
                                        b'{"messages": []}', 5, max_retries=2)
        self.assertEqual(len(server.requests), 1)
        self.assertTrue(server.requests[0].endswith(b'{"messages": []}'))
        sleep.assert_not_called()

    def test_wire_refused_connection_retried(self):
        url = 'http://127.0.0.1:%s/v1/chat/completions' % free_port()
        with patch(BACKEND_SLEEP) as sleep:
            with self.assertRaises(requests.ConnectionError):
                self.contact._http_post(url, {}, b'{}', 5, max_retries=1)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [3])

    def test_sse_total_deadline_without_chunked_encoding(self):
        # no chunked encoding: a 1024-byte read waits for 1024 bytes or EOF while
        # small keepalives keep resetting the socket timeout; the deadline still cuts it
        with LocalServer(slow_sse_handler(6)) as server:
            response = self.contact._http_post(server.url + '/v1/chat/completions', {}, b'{}', 30, stream=True,
                                               max_retries=0)
            started = time.time()
            with self.assertRaises(BackendError) as caught:
                self.env['woow.ai.contact']._read_sse(response, started + 1)
            elapsed = time.time() - started
            response.close()
        self.assertEqual(caught.exception.status, 'timeout')
        self.assertLess(elapsed, 4, 'the deadline must not wait for the stream to end')

    def test_wire_redirect_not_followed(self):
        with LocalServer(lambda conn, _r: conn.sendall(b'stolen')) as target:
            location = ('HTTP/1.1 307 Temporary Redirect\r\nLocation: %s/steal\r\nContent-Length: 0\r\n'
                        'Connection: close\r\n\r\n' % target.url).encode()
            with LocalServer(lambda conn, _r: conn.sendall(location)) as server:
                response = self.contact._http_post(server.url + '/v1/chat/completions', {}, b'{"secret": 1}', 5,
                                                   max_retries=0)
                response.close()
            time.sleep(0.3)
            self.assertEqual(response.status_code, 307)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(target.requests, [], 'the redirect target never gets the conversation')
