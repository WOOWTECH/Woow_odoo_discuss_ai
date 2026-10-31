# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch

import requests

from odoo import fields
from odoo.tests.common import tagged

from odoo.addons.woow_ai_contact.models.ai_job import JOB_MAX_AGE_MINUTES, MAX_JOB_RUNS
from odoo.addons.woow_ai_contact.tools import media

from .test_media import ooxml
from .common import (BACKEND_POST, BACKEND_SLEEP, MEDIA_GET, TEST_KEY, TINY_PDF, TINY_PNG, AiContactCase,
                     http_response, openai_ok)


def run(job):
    job._run_job(commit=False)
    return job


@tagged('woow_ai_contact')
class TestOpenAiJob(AiContactCase):

    def test_reply_posted_with_markdown_table(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, '報價？')
        job = self.jobs(chat)
        with patch(BACKEND_POST, return_value=openai_ok('| 品名 | 價格 |\n|---|---:|\n| A | 100 |')) as post:
            run(job)
        self.assertEqual(job.state, 'done', job.error)
        self.assertEqual(job.run_count, 1)
        reply = job.reply_message_id
        self.assertEqual(reply.author_id, self.contact.partner_id)
        self.assertEqual(reply.message_type, 'comment')
        self.assertIn('<table class="table table-sm table-bordered">', reply.body)
        # request
        self.assertEqual(post.call_count, 1)
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://llm.example.com/v1/chat/completions')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer %s' % TEST_KEY)
        # streamed by default: the read timeout is the idle timeout
        self.assertTrue(kwargs['stream'])
        self.assertEqual(kwargs['timeout'], (10, 30))
        body = json.loads(kwargs['data'])
        self.assertEqual(body['model'], 'test-model')
        self.assertIs(body['stream'], True)
        self.assertNotIn('tools', body)
        session_id = self.env['woow.ai.contact']._session_id(chat)
        self.assertEqual(body['user'], session_id)
        uuid = self.env['ir.config_parameter'].sudo().get_param('database.uuid').replace('-', '')[:8]
        self.assertEqual(session_id, 'woow-ai-%s-%s' % (uuid, chat.id))
        self.assertNotIn(':', session_id)
        # metadata: flat, string values only (OpenAI API rule); email kept there
        metadata = body['metadata']
        self.assertTrue(all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()))
        self.assertEqual(metadata['session_id'], session_id)
        self.assertEqual(metadata['odoo_user_id'], str(self.internal.id))
        self.assertEqual(metadata['odoo_email'], 'iris@example.com')
        self.assertEqual(metadata['odoo_is_portal'], 'false')
        # system: the prompt, then the identity block the model can see (no email)
        system = body['messages'][0]
        self.assertEqual(system['role'], 'system')
        self.assertTrue(system['content'].startswith('You are helpful.\n\n'))
        self.assertIn('- name: Internal Iris', system['content'])
        self.assertIn('- account: internal user', system['content'])
        self.assertIn('- user_id: %s' % self.internal.id, system['content'])
        self.assertNotIn('iris@example.com', system['content'])
        self.assertEqual(body['messages'][-1], {'role': 'user', 'content': '報價？'})
        # call log: status only
        log = self.env['woow.ai.call.log'].search([('job_id', '=', job.id)])
        self.assertEqual(log.status, 'ok')
        self.assertEqual(log.http_status, 200)
        self.assertFalse(log.request_payload)
        self.assertFalse(log.response_payload)

    def test_optional_model_and_prompt(self):
        self.contact.write({'model': False, 'system_prompt': False})
        chat = self.chat(self.portal)
        self.post(chat, self.portal, 'hi')
        with patch(BACKEND_POST, return_value=openai_ok()) as post:
            run(self.jobs(chat))
        body = json.loads(post.call_args.kwargs['data'])
        self.assertNotIn('model', body)
        # no prompt: the system message only carries the identity block
        self.assertEqual([m['role'] for m in body['messages']], ['system', 'user'])
        self.assertTrue(body['messages'][0]['content'].startswith('You are chatting in Odoo'))
        self.assertIn('- account: portal user', body['messages'][0]['content'])
        self.assertNotIn('pat@example.com', body['messages'][0]['content'])
        self.assertEqual(body['metadata']['odoo_is_portal'], 'true')

    def test_history_roles_and_think_tags(self):
        self.contact.welcome_message = 'Welcome!'
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'one')
        with patch(BACKEND_POST, return_value=openai_ok('<think>secret reasoning</think>answer one')):
            run(self.jobs(chat))
        self.assertNotIn('secret', self.jobs(chat).reply_message_id.body)
        self.post(chat, self.internal, 'two')
        with patch(BACKEND_POST, return_value=openai_ok('answer two')) as post:
            run(self.jobs(chat)[-1])
        messages = json.loads(post.call_args.kwargs['data'])['messages']
        # the leading welcome message is dropped: the turns start with the user
        self.assertEqual([(m['role'], m['content']) for m in messages[1:]], [
            ('user', 'one'), ('assistant', 'answer one'), ('user', 'two')])

    def test_max_history(self):
        self.contact.max_history = 2
        chat = self.chat(self.internal)
        for text in ('a', 'b', 'c'):
            self.post(chat, self.internal, text)
        with patch(BACKEND_POST, return_value=openai_ok()) as post:
            run(self.jobs(chat)[-1])
        messages = json.loads(post.call_args.kwargs['data'])['messages']
        self.assertEqual([m['content'] for m in messages[1:]], ['b', 'c'])

    def test_images_inlined_for_last_three_user_messages(self):
        chat = self.chat(self.internal)
        for i in range(4):
            att = self.attachment(chat, self.internal, name='p%s.png' % i)
            self.post(chat, self.internal, 'pic %s' % i, attachments=att)
        pdf = self.attachment(chat, self.internal, name='報價.pdf', raw=TINY_PDF)
        self.post(chat, self.internal, 'doc', attachments=pdf)
        with patch(BACKEND_POST, return_value=openai_ok()) as post:
            run(self.jobs(chat)[-1])
        messages = json.loads(post.call_args.kwargs['data'])['messages'][1:]
        # oldest image message: text line with a signed URL
        self.assertIsInstance(messages[0]['content'], str)
        self.assertIn('[附件] p0.png（image/png', messages[0]['content'])
        self.assertIn('/woow_ai/attachment/', messages[0]['content'])
        self.assertIsInstance(messages[1]['content'], str)  # 4 user messages after it: not in the last 3
        # last 3 user messages: pic 2, pic 3, doc
        self.assertIsInstance(messages[2]['content'], list)
        self.assertEqual(messages[2]['content'][0], {'type': 'text', 'text': 'pic 2'})
        self.assertTrue(messages[2]['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))
        self.assertIsInstance(messages[4]['content'], str)  # the PDF is never inlined
        self.assertIn('[附件] 報價.pdf（application/pdf', messages[4]['content'])
        token = messages[4]['content'].rsplit('/', 1)[1]
        self.assertTrue(media.check_token(self.env['woow.ai.contact']._signing_secret(),
                                          pdf.id, pdf.checksum, token))

    def test_rapid_messages_one_reply(self):
        chat = self.chat(self.internal)
        for text in ('1', '2', '3'):
            self.post(chat, self.internal, text)
        first, second, third = self.jobs(chat)
        with patch(BACKEND_POST, return_value=openai_ok('one answer')) as post:
            run(first)
            self.assertEqual(first.state, 'skipped')
            post.assert_not_called()
            run(third)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(third.state, 'done')
        self.assertEqual(second.state, 'skipped')
        messages = json.loads(post.call_args.kwargs['data'])['messages']
        self.assertEqual([m['content'] for m in messages[1:]], ['1', '2', '3'])
        self.assertEqual(len(self.bot_messages(chat)), 1)

    def test_newer_message_during_call_drops_reply(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'first')
        job = self.jobs(chat)

        def answer_while_user_writes(*args, **kwargs):
            self.post(chat, self.internal, 'second')
            return openai_ok('late answer')

        with patch(BACKEND_POST, side_effect=answer_while_user_writes):
            run(job)
        self.assertEqual(job.state, 'skipped')
        self.assertFalse(self.bot_messages(chat))
        self.assertEqual(self.jobs(chat)[-1].state, 'pending')

    def test_typing_indicator(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'hi')
        Member = type(self.env['discuss.channel.member'])
        with patch(BACKEND_POST, return_value=openai_ok()), \
                patch.object(Member, '_notify_typing', autospec=True) as typing:
            run(self.jobs(chat))
        self.assertEqual([c.args[1] for c in typing.call_args_list], [True, False])
        self.assertEqual(typing.call_args_list[0].args[0].partner_id, self.contact.partner_id)

    def test_timeout_posts_error_message(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'slow?')
        job = self.jobs(chat)
        with patch(BACKEND_POST, side_effect=requests.ReadTimeout('slow')) as post:
            run(job)
        self.assertEqual(post.call_count, 1, 'a read timeout is never retried')
        self.assertEqual(job.state, 'failed')
        self.assertIn('timeout', job.error)
        self.assertIn('Sorry', job.reply_message_id.body)
        log = self.env['woow.ai.call.log'].search([('job_id', '=', job.id)])
        self.assertEqual(log.status, 'timeout')

    def test_custom_error_message_and_http_error(self):
        self.contact.error_message = 'AI 暫時無法回覆'
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'x')
        job = self.jobs(chat)
        with patch(BACKEND_POST, return_value=http_response(500, text='upstream secret detail')):
            run(job)
        self.assertEqual(job.state, 'failed')
        self.assertIn('AI 暫時無法回覆', job.reply_message_id.body)
        log = self.env['woow.ai.call.log'].search([('job_id', '=', job.id)])
        self.assertEqual((log.status, log.http_status), ('http_error', 500))
        self.assertNotIn('upstream secret detail', log.error or '')

    def test_invalid_and_empty_responses(self):
        chat = self.chat(self.internal)
        for response in (http_response(200, text='<html>'), http_response(200, {'foo': 1}), openai_ok('')):
            self.post(chat, self.internal, 'x')
            job = self.jobs(chat)[-1]
            with patch(BACKEND_POST, return_value=response):
                run(job)
            self.assertEqual(job.state, 'failed')
            log = self.env['woow.ai.call.log'].search([('job_id', '=', job.id)])
            self.assertEqual(log.status, 'invalid_response')

    def test_connection_error_retried_once(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'x')
        job = self.jobs(chat)
        with patch(BACKEND_POST, side_effect=[requests.ConnectionError('down'), openai_ok('ok now')]) as post, \
                patch(BACKEND_SLEEP) as sleep:
            run(job)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once()
        self.assertEqual(job.state, 'done')

    def test_not_configured(self):
        self.contact.action_clear_api_key()
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'x')
        job = self.jobs(chat)
        with patch(BACKEND_POST) as post:
            run(job)
        post.assert_not_called()
        self.assertEqual(job.state, 'failed')
        self.assertTrue(job.reply_message_id)

    def test_recovery_cron(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'lost after restart')
        job = self.jobs(chat)
        job.write({'run_after': fields.Datetime.now() - timedelta(minutes=5)})
        with patch(BACKEND_POST, return_value=openai_ok('recovered')):
            count = self.Job._cron_recover_jobs(commit=False)
        self.assertEqual(count, 1)
        self.assertEqual(job.state, 'done')
        # a job that already ran MAX_JOB_RUNS times without finishing gets the error message
        self.post(chat, self.internal, 'crashing')
        job2 = self.jobs(chat)[-1]
        job2.write({'state': 'running', 'run_count': MAX_JOB_RUNS,
                    'started_at': fields.Datetime.now() - timedelta(minutes=5)})
        with patch(BACKEND_POST) as post:
            self.Job._cron_recover_jobs(commit=False)
        post.assert_not_called()
        self.assertEqual(job2.state, 'failed')
        self.assertTrue(job2.reply_message_id)

    def test_recent_jobs_not_recovered_and_old_jobs_given_up(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'fresh')
        self.assertEqual(self.Job._cron_recover_jobs(commit=False), 0)
        job = self.jobs(chat)
        self.env.cr.execute("UPDATE woow_ai_job SET create_date = %s WHERE id = %s",
                            (fields.Datetime.now() - timedelta(minutes=JOB_MAX_AGE_MINUTES + 1), job.id))
        job.invalidate_recordset()
        with patch(BACKEND_POST) as post:
            run(job)
        post.assert_not_called()
        self.assertEqual(job.state, 'failed')

    def test_already_answered_not_answered_twice(self):
        chat = self.chat(self.internal)
        message = self.post(chat, self.internal, 'q')
        job = self.jobs(chat)
        with patch(BACKEND_POST, return_value=openai_ok('a')):
            run(job)
        duplicate = self.Job._create_for_message(chat, self.contact, message)
        with patch(BACKEND_POST) as post:
            run(duplicate)
        post.assert_not_called()
        self.assertEqual(duplicate.state, 'done')
        self.assertEqual(len(self.bot_messages(chat)), 1)

    def test_log_payload_redacted_and_erased(self):
        self.contact.log_payload = True
        chat = self.chat(self.internal)
        att = self.attachment(chat, self.internal)
        self.post(chat, self.internal, 'look', attachments=att)
        job = self.jobs(chat)
        with patch(BACKEND_POST, return_value=openai_ok('nice picture')):
            run(job)
        log = self.env['woow.ai.call.log'].search([('job_id', '=', job.id)])
        self.assertIn('base64 characters omitted', log.request_payload)
        self.assertNotIn(base64.b64encode(TINY_PNG).decode(), log.request_payload)
        self.assertNotIn(TEST_KEY, log.request_payload)
        self.assertIn('nice picture', log.response_payload)
        self.contact.log_payload = False
        self.assertFalse(log.request_payload)
        self.assertFalse(log.response_payload)

    def test_cleanup_old_logs_and_jobs(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'x')
        job = self.jobs(chat)
        with patch(BACKEND_POST, return_value=openai_ok()):
            run(job)
        log = self.env['woow.ai.call.log'].search([('job_id', '=', job.id)])
        old = fields.Datetime.now() - timedelta(days=31)
        self.env.cr.execute("UPDATE woow_ai_call_log SET create_date = %s WHERE id = %s", (old, log.id))
        self.env.cr.execute("UPDATE woow_ai_job SET create_date = %s WHERE id = %s", (old, job.id))
        self.env.invalidate_all()
        self.env['woow.ai.call.log']._cleanup_old_logs()
        self.assertFalse(log.exists())
        self.assertFalse(job.exists())


@tagged('woow_ai_contact')
class TestN8nJob(AiContactCase):

    def run_n8n(self, response_json, text='hi', attachments=None, user=None):
        user = user or self.portal
        chat = self.chat(user, self.n8n)
        self.post(chat, user, text, attachments=attachments(chat) if attachments else None)
        job = self.jobs(chat)[-1]
        with patch(BACKEND_POST, return_value=http_response(200, response_json)) as post:
            run(job)
        return chat, job, post

    def test_request_signature_and_body(self):
        chat, job, post = self.run_n8n({'reply': {'text': 'ok'}}, text='報價單',
                                       attachments=lambda chat: self.attachment(chat, self.portal, 'q.pdf', TINY_PDF))
        self.assertEqual(job.state, 'done', job.error)
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://n8n.example.com/webhook/abc')
        headers = kwargs['headers']
        # the API key is only the signing secret: never sent (n8n stores headers)
        self.assertNotIn('Authorization', headers)
        self.assertNotIn(TEST_KEY, json.dumps(headers))
        self.assertNotIn(TEST_KEY.encode(), kwargs['data'])
        self.assertFalse(kwargs['stream'])
        self.assertEqual(kwargs['timeout'], (10, 300))
        expected = hmac.new(TEST_KEY.encode(), headers['X-Woow-Timestamp'].encode() + b'.' + kwargs['data'],
                            hashlib.sha256).hexdigest()
        self.assertEqual(headers['X-Woow-Signature'], 'sha256=%s' % expected)
        body = json.loads(kwargs['data'])
        self.assertEqual(body['event'], 'message')
        self.assertEqual(body['session_id'], self.env['woow.ai.contact']._session_id(chat))
        self.assertEqual(body['contact'], {'id': self.n8n.id, 'name': 'Workflow Bot'})
        self.assertEqual(body['channel'], {'id': chat.id, 'type': 'chat'})
        self.assertEqual(body['user']['partner_id'], self.portal.partner_id.id)
        self.assertTrue(body['user']['is_portal'])
        self.assertEqual(body['message']['id'], job.message_id.id)
        self.assertEqual(body['message']['text'], '報價單')
        att = body['message']['attachments'][0]
        self.assertEqual((att['name'], att['mimetype']), ('q.pdf', 'application/pdf'))
        self.assertIn('/woow_ai/attachment/%s/' % att['id'], att['url'])
        self.assertTrue(att['expires_at'].endswith('Z'))
        self.assertEqual(body['history'], [])
        self.assertEqual(body['system_prompt'], '')

    def test_reply_attachments(self):
        payload = {'reply': {
            'text': '圖表如下 ![chart](https://files.example.com/c/chart.png)\n'
                    '外部 [report](https://evil.example.net/r.pdf)',
            'attachments': [
                {'name': '報價.pdf', 'mimetype': 'application/pdf', 'data_base64': base64.b64encode(TINY_PDF).decode()},
                {'name': 'fake.png', 'mimetype': 'image/png',
                 'data_base64': base64.b64encode(b'<html><script>alert(1)</script></html>').decode()},
                {'name': 'outside.xlsx', 'url': 'https://evil.example.net/o.xlsx'},
            ],
        }}
        with patch(MEDIA_GET, return_value=http_response(200, chunks=[TINY_PNG])) as get:
            chat, job, _post = self.run_n8n(payload)
        self.assertEqual(job.state, 'done', job.error)
        get.assert_called_once()  # only the allowlisted chart was downloaded
        reply = job.reply_message_id
        by_name = {a.name: a for a in reply.attachment_ids}
        self.assertEqual(set(by_name), {'chart.png', '報價.pdf', 'fake.png'})
        self.assertEqual(by_name['chart.png'].mimetype, 'image/png')
        self.assertEqual(by_name['報價.pdf'].mimetype, 'application/pdf')
        self.assertEqual(by_name['fake.png'].mimetype, 'application/octet-stream')
        self.assertIn('chart.png', reply.body)
        self.assertNotIn('files.example.com', reply.body)
        self.assertIn('href="https://evil.example.net/r.pdf"', reply.body)
        self.assertIn('href="https://evil.example.net/o.xlsx"', reply.body)

    def test_office_files_keep_their_type(self):
        payload = {'reply': {'text': '報表', 'attachments': [
            {'name': '報表.xlsx', 'data_base64': base64.b64encode(ooxml('xl/workbook.xml')).decode()},
            {'name': '說明.docx', 'data_base64': base64.b64encode(ooxml('word/document.xml')).decode()},
        ]}}
        chat, job, _post = self.run_n8n(payload)
        by_name = {a.name: a.mimetype for a in job.reply_message_id.attachment_ids}
        self.assertEqual(by_name, {
            '報表.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            '說明.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        })

    def test_oversized_download_stays_link(self):
        self.n8n.max_download_mb = 1
        big = http_response(200, chunks=[b'x' * (600 * 1024), b'x' * (600 * 1024)])
        payload = {'reply': {'text': '![big](https://files.example.com/big.png)'}}
        with patch(MEDIA_GET, return_value=big):
            chat, job, _post = self.run_n8n(payload)
        self.assertFalse(job.reply_message_id.attachment_ids)
        self.assertIn('href="https://files.example.com/big.png"', job.reply_message_id.body)

    def test_multiple_replies_and_skip(self):
        chat, job, _post = self.run_n8n({'replies': [{'text': 'first'}, {'text': 'second'}]})
        self.assertEqual(job.state, 'done')
        self.assertEqual(len(self.bot_messages(chat, self.n8n)), 2)
        chat2, job2, _post = self.run_n8n({'skip': True}, user=self.internal)
        self.assertEqual(job2.state, 'done')
        self.assertFalse(job2.reply_message_id)
        self.assertFalse(self.bot_messages(chat2, self.n8n))

    def test_invalid_n8n_response(self):
        chat, job, _post = self.run_n8n({'unexpected': 1})
        self.assertEqual(job.state, 'failed')
        self.assertEqual(self.env['woow.ai.call.log'].search([('job_id', '=', job.id)]).status, 'invalid_response')

    def test_history_sent_with_roles(self):
        chat = self.chat(self.portal, self.n8n)
        self.post(chat, self.portal, 'q1')
        with patch(BACKEND_POST, return_value=http_response(200, {'reply': {'text': 'a1'}})):
            run(self.jobs(chat))
        self.post(chat, self.portal, 'q2')
        with patch(BACKEND_POST, return_value=http_response(200, {'reply': {'text': 'a2'}})) as post:
            run(self.jobs(chat)[-1])
        body = json.loads(post.call_args.kwargs['data'])
        self.assertEqual(body['message']['text'], 'q2')
        self.assertEqual([(h['role'], h['text']) for h in body['history']], [('user', 'q1'), ('assistant', 'a1')])
