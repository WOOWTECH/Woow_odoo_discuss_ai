# -*- coding: utf-8 -*-
"""Shared fixtures (no real HTTP, no real key: every request is mocked)."""

import base64
import json
import socket
import threading
import time
from unittest.mock import MagicMock

import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError

from odoo import Command
from odoo.tests.common import TransactionCase, new_test_user

BACKEND_POST = 'odoo.addons.woow_ai_contact.models.ai_contact_backend.requests.post'
BACKEND_GET = 'odoo.addons.woow_ai_contact.models.ai_contact_backend.requests.get'
MEDIA_GET = 'odoo.addons.woow_ai_contact.tools.media.requests.get'
BACKEND_SLEEP = 'odoo.addons.woow_ai_contact.models.ai_contact_backend.time.sleep'

TEST_KEY = 'test-backend-key'  # placeholder, not a real key

# 1x1 transparent PNG
TINY_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')
TINY_PDF = b'%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n'


def http_response(status=200, json_data=None, text=None, headers=None, chunks=None):
    response = MagicMock()
    response.status_code = status
    response.headers = headers or {}
    if json_data is not None:
        response.json.return_value = json_data
        response.text = json.dumps(json_data)
    else:
        response.json.side_effect = ValueError('not json')
        response.text = text or ''
    response.iter_content.return_value = chunks or []
    return response


def openai_ok(content='您好，我是 AI。', usage=None):
    data = {'choices': [{'message': {'role': 'assistant', 'content': content}}]}
    if usage:
        data['usage'] = usage
    return http_response(200, data)


def sse_response(chunks, status=200, error=None):
    """Streamed (SSE) answer; `chunks` are str/bytes pieces of the raw body.
    `error` is raised by iter_content after the chunks (e.g. an idle timeout)."""
    response = MagicMock()
    response.status_code = status
    response.headers = {'Content-Type': 'text/event-stream; charset=utf-8'}
    raw = [c.encode() if isinstance(c, str) else c for c in chunks]

    def iter_content(chunk_size=None):
        yield from raw
        if error:
            raise error

    response.iter_content.side_effect = iter_content
    response.json.side_effect = ValueError('not json')
    return response


def sse_event(data, event=None):
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return ('event: %s\n' % event if event else '') + 'data: %s\n\n' % payload


def delta(content, finish_reason=None, **extra):
    choice = {'index': 0, 'delta': {'content': content} if content is not None else {}}
    if finish_reason:
        choice['finish_reason'] = finish_reason
    return dict({'choices': [choice]}, **extra)


def refused():
    """requests' error for a connection that could not be opened (nothing sent)."""
    return requests.ConnectionError(MaxRetryError(None, '/v1/chat/completions',
                                                  NewConnectionError(None, 'Connection refused')))


def reset_after_send():
    """requests' error when the backend got the request and then reset the connection."""
    return requests.ConnectionError(ProtocolError('Connection aborted.', ConnectionResetError(104, 'reset')))


class LocalServer:
    """Raw TCP server on 127.0.0.1 (the only host tests may reach) for wire
    level cases a mock cannot show. ``handler(conn, request_bytes)`` answers
    one connection; ``requests`` counts the requests received."""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.url = 'http://127.0.0.1:%s' % self.port
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(5)
        self.sock.close()
        return False

    @staticmethod
    def read_request(conn):
        conn.settimeout(5)
        data = b''
        while b'\r\n\r\n' not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return data
            data += chunk
        head, _sep, body = data.partition(b'\r\n\r\n')
        length = 0
        for line in head.split(b'\r\n')[1:]:
            name, _sep, value = line.partition(b':')
            if name.strip().lower() == b'content-length':
                length = int(value.strip())
        while len(body) < length:
            chunk = conn.recv(65536)
            if not chunk:
                break
            body += chunk
        return head + b'\r\n\r\n' + body

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            request = self.read_request(conn)
            self.requests.append(request)
            self.handler(conn, request)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def free_port():
    """A local port nobody listens on (connection refused)."""
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def slow_sse_handler(seconds, interval=0.2):
    """Non-chunked SSE answer (HTTP/1.0 style: no Content-Length, closed at the
    end) that only sends small keepalives for `seconds`."""
    def handler(conn, _request):
        conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n')
        end = time.time() + seconds
        while time.time() < end:
            conn.sendall(b': keepalive\n\n')
            time.sleep(interval)
    return handler


class AiContactCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.internal = new_test_user(cls.env, 'ai_internal', groups='base.group_user', name='Internal Iris',
                                     email='iris@example.com')
        cls.portal = new_test_user(cls.env, 'ai_portal', groups='base.group_portal', name='Portal Pat',
                                   email='pat@example.com')
        cls.group_testers = cls.env['res.groups'].create({'name': 'AI testers'})
        cls.contact = cls.env['woow.ai.contact'].create({
            'name': 'Product Advisor',
            'backend_type': 'openai',
            'endpoint_url': 'https://llm.example.com/v1',
            'api_key': TEST_KEY,
            'model': 'test-model',
            'system_prompt': 'You are helpful.',
            'allowed_group_ids': [Command.set([cls.env.ref('base.group_user').id, cls.env.ref('base.group_portal').id])],
            'download_domains': 'files.example.com\n*.cdn.example.com',
        })
        cls.n8n = cls.env['woow.ai.contact'].create({
            'name': 'Workflow Bot',
            'backend_type': 'n8n',
            'allowed_group_ids': [Command.set([cls.env.ref('base.group_user').id, cls.env.ref('base.group_portal').id])],
            'endpoint_url': 'https://n8n.example.com/webhook/abc',
            'api_key': TEST_KEY,
            'download_domains': 'files.example.com',
        })
        cls.Job = cls.env['woow.ai.job']

    def chat(self, user, contact=None):
        chat, _created = (contact or self.contact)._get_or_create_chat(user)
        return chat

    def post(self, chat, user, body='hello', attachments=None, message_type='comment'):
        return chat.with_user(user).message_post(
            body=body, message_type=message_type, subtype_xmlid='mail.mt_comment',
            attachment_ids=(attachments or self.env['ir.attachment']).ids,
        )

    def attachment(self, chat, user, name='a.png', raw=TINY_PNG, mimetype=None):
        # pending upload (as the composer does): message_post links it to the channel
        vals = {'name': name, 'raw': raw, 'res_model': 'mail.compose.message', 'res_id': 0}
        if mimetype:
            vals['mimetype'] = mimetype
        return self.env['ir.attachment'].with_user(user).sudo().create(vals)

    def jobs(self, chat):
        return self.Job.search([('channel_id', '=', chat.id)], order='id')

    def bot_messages(self, chat, contact=None):
        return self.env['mail.message'].search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', chat.id),
            ('author_id', '=', (contact or self.contact).partner_id.id),
        ], order='id')
