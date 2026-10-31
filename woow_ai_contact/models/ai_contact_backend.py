# -*- coding: utf-8 -*-
"""Backend protocols of an AI contact: OpenAI compatible and n8n webhook.

See docs/PROTOCOL.md for the wire format. No transaction is open during the
HTTP call: the caller passes ``before_call`` (the job commits there).
"""

import base64
import copy
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime, timezone

import requests

from odoo import api, models

from ..tools import media

_logger = logging.getLogger(__name__)

INLINE_IMAGE_MAX_BYTES = 5 * 1024 * 1024
INLINE_IMAGE_MESSAGES = 3  # the most recent user messages whose images are inlined
CONNECT_TIMEOUT = 10
MAX_CONNECT_RETRIES = 1
CONNECT_RETRY_WAIT = 3
MAX_LOG_PAYLOAD = 200000  # characters
MAX_ERROR_LENGTH = 300
MAX_REPLIES = 10

_THINK_RE = re.compile(r'<think>.*?</think>', re.DOTALL)
_THINK_OPEN_RE = re.compile(r'<think>.*$', re.DOTALL)


class BackendError(Exception):
    """A backend call failed; ``status`` is a woow.ai.call.log status."""

    def __init__(self, status, message, http_status=None):
        super().__init__(message)
        self.status = status
        self.http_status = http_status


def strip_think(text):
    if not text:
        return ''
    text = _THINK_RE.sub('', text)
    return _THINK_OPEN_RE.sub('', text).strip()


def _json_dumps(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'), default=str)


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace('+00:00', 'Z')


class WoowAiContactBackend(models.Model):
    _inherit = 'woow.ai.contact'

    # --- Context ---

    @api.model
    def _session_id(self, channel):
        uuid = self.env['ir.config_parameter'].sudo().get_param('database.uuid') or ''
        return 'woow-ai:%s:%s' % (uuid.replace('-', '')[:8], channel.id)

    @api.model
    def _identity(self, partner):
        """Basic identity of the person the AI talks to (no login is delegated)."""
        partner = partner.sudo()
        user = partner.with_context(active_test=False).user_ids[:1] if partner else self.env['res.users']
        return {
            'user_id': user.id or None,
            'partner_id': partner.id or None,
            'name': partner.name or '',
            'email': partner.email or '',
            'lang': (user.lang if user else partner.lang) or '',
            'tz': (user.tz if user else partner.tz) or '',
            'is_portal': bool(user and user._is_portal()),
            'company_id': user.company_id.id if user else None,
        }

    @api.model
    def _signing_secret(self):
        return self.env['ir.config_parameter'].sudo().get_param('database.secret') or ''

    @api.model
    def _attachment_info(self, attachment, base_url, now=None):
        """ATT object (docs/PROTOCOL.md) with a signed download URL valid 1 hour."""
        attachment = attachment.sudo()
        token, expires = media.make_token(self._signing_secret(), attachment.id, attachment.checksum, now=now)
        return {
            'id': attachment.id,
            'name': attachment.name or '',
            'mimetype': attachment.mimetype or 'application/octet-stream',
            'size': attachment.file_size or 0,
            'url': '%s/woow_ai/attachment/%s/%s' % (base_url.rstrip('/'), attachment.id, token),
            'expires_at': _iso(expires),
        }

    @staticmethod
    def _attachment_line(info):
        return '[附件] %s（%s，%s）：%s' % (
            info['name'], info['mimetype'], media.human_size(info['size']), info['url'])

    # --- Request building ---

    def _openai_messages(self, history, base_url):
        self.ensure_one()
        contact = self.sudo()
        messages = []
        if (contact.system_prompt or '').strip():
            messages.append({'role': 'system', 'content': contact.system_prompt.strip()})
        user_positions = [i for i, entry in enumerate(history) if entry['role'] == 'user']
        inline_positions = set(user_positions[-INLINE_IMAGE_MESSAGES:])
        for position, entry in enumerate(history):
            lines = [entry['text']] if entry['text'] else []
            images = []
            for attachment in entry['attachments'].sudo():
                if (position in inline_positions
                        and attachment.mimetype in media.INLINE_IMAGE_TYPES
                        and (attachment.file_size or 0) <= INLINE_IMAGE_MAX_BYTES):
                    raw = attachment.raw or b''
                    if raw and len(raw) <= INLINE_IMAGE_MAX_BYTES:
                        images.append('data:%s;base64,%s' % (
                            attachment.mimetype, base64.b64encode(raw).decode()))
                        continue
                lines.append(self._attachment_line(self._attachment_info(attachment, base_url)))
            text = '\n'.join(lines)
            if images:
                content = [{'type': 'text', 'text': text}] if text else []
                content += [{'type': 'image_url', 'image_url': {'url': uri}} for uri in images]
            else:
                content = text
            messages.append({'role': entry['role'], 'content': content})
        return messages

    def _build_request(self, channel, history, requester, trigger_message=None):
        """Returns (url, headers, body bytes, payload for the call log)."""
        self.ensure_one()
        contact = self.sudo()
        api_key = contact._get_api_key()
        endpoint = (contact.endpoint_url or '').strip()
        if not endpoint or not api_key:
            raise BackendError('error', 'backend not configured (endpoint URL or API key missing)')
        base_url = channel.get_base_url()
        session_id = self._session_id(channel)
        identity = self._identity(requester)
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer %s' % api_key}
        if contact.backend_type == 'openai':
            url = endpoint.rstrip('/') + '/chat/completions'
            payload = {
                'messages': contact._openai_messages(history, base_url),
                'user': session_id,
                'metadata': {'session_id': session_id, 'odoo_user': identity},
            }
            if (contact.model or '').strip():
                payload = {'model': contact.model.strip(), **payload}
            body = _json_dumps(payload).encode()
        else:
            url = endpoint
            payload = contact._n8n_payload(channel, history, identity, session_id, base_url, trigger_message)
            body = _json_dumps(payload).encode()
            timestamp = str(int(time.time()))
            signature = hmac.new(api_key.encode(), timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
            headers['X-Woow-Timestamp'] = timestamp
            headers['X-Woow-Signature'] = 'sha256=%s' % signature
        return url, headers, body, payload

    def _n8n_payload(self, channel, history, identity, session_id, base_url, trigger_message=None):
        self.ensure_one()
        contact = self.sudo()

        def entry_json(entry):
            return {
                'role': entry['role'],
                'text': entry['text'],
                'attachments': [self._attachment_info(a, base_url) for a in entry['attachments'].sudo()],
            }

        current = None
        if trigger_message:
            current = next((e for e in history if e.get('message_id') == trigger_message.id), None)
        if current is None:
            current = next((e for e in reversed(history) if e['role'] == 'user'), None)
        past = [e for e in history if e is not current]
        message = {'id': None, 'text': '', 'attachments': []}
        if current:
            message = {'id': current.get('message_id'), 'text': current['text'],
                       'attachments': entry_json(current)['attachments']}
        return {
            'event': 'message',
            'session_id': session_id,
            'contact': {'id': contact.id, 'name': contact.name},
            'user': identity,
            'channel': channel._woow_ai_channel_info(contact),
            'system_prompt': (contact.system_prompt or '').strip(),
            'message': message,
            'history': [entry_json(e) for e in past],
        }

    # --- Response parsing ---

    @staticmethod
    def _openai_text(data):
        if not isinstance(data, dict) or not isinstance(data.get('choices'), list) or not data['choices']:
            raise BackendError('invalid_response', "invalid response: no 'choices'")
        message = (data['choices'][0] or {}).get('message') or {}
        content = message.get('content')
        if isinstance(content, list):
            content = '\n'.join(
                part.get('text') or '' for part in content
                if isinstance(part, dict) and part.get('type') in ('text', 'output_text'))
        if content is not None and not isinstance(content, str):
            raise BackendError('invalid_response', 'invalid response: content is not text')
        return strip_think(content or '')

    @staticmethod
    def _n8n_replies(data):
        """n8n JSON -> (replies, skip)."""
        if isinstance(data, list) and data and isinstance(data[0], dict):
            data = data[0]  # "Respond to Webhook" with all incoming items
        if not isinstance(data, dict):
            raise BackendError('invalid_response', 'invalid response: not a JSON object')
        if data.get('skip') is True:
            return [], True
        if 'replies' in data:
            raw = data['replies']
            if not isinstance(raw, list):
                raise BackendError('invalid_response', "invalid response: 'replies' is not a list")
        elif 'reply' in data:
            raw = [data['reply']]
        else:
            raise BackendError('invalid_response', "invalid response: no 'reply', 'replies' or 'skip'")
        replies = []
        for item in raw[:MAX_REPLIES]:
            if isinstance(item, str):
                item = {'text': item}
            if not isinstance(item, dict):
                raise BackendError('invalid_response', 'invalid response: reply is not an object')
            text = item.get('text') or ''
            attachments = item.get('attachments') or []
            if not isinstance(text, str) or not isinstance(attachments, list):
                raise BackendError('invalid_response', 'invalid response: bad reply fields')
            replies.append({'text': strip_think(text), 'attachments': attachments})
        return replies, False

    # --- Call ---

    def _http_post(self, url, headers, body, read_timeout):
        """POST with explicit timeouts; one retry when the connection failed.
        Reads nothing from the database (no transaction is open here)."""
        timeout = (CONNECT_TIMEOUT, read_timeout)
        for attempt in range(MAX_CONNECT_RETRIES + 1):
            try:
                return requests.post(url, data=body, headers=headers, timeout=timeout)
            except requests.ConnectionError as e:
                # ConnectTimeout is both; a read timeout is never retried
                if isinstance(e, requests.ReadTimeout) or attempt >= MAX_CONNECT_RETRIES:
                    raise
                _logger.info("AI contact %s: connection failed (%s), retrying once", self._ids, type(e).__name__)
                time.sleep(CONNECT_RETRY_WAIT)

    def _call_backend(self, channel, history, requester, job=None, before_call=None):
        """Call the backend once (connection retry aside) and log the call.

        Returns:
            dict: replies (list of {'text', 'attachments'}), skip (bool),
                error (short text or None)
        """
        self.ensure_one()
        contact = self.sudo()
        trigger = job.message_id if job else None
        started = time.time()
        log = {'status': 'error', 'http_status': None, 'error': None, 'request': None, 'response': None}
        result = {'replies': [], 'skip': False, 'error': None}
        try:
            url, headers, body, payload = contact._build_request(channel, history, requester, trigger)
            log['request'] = payload
            read_timeout = contact.timeout_seconds or 300
            backend_type = contact.backend_type
            contact_id = contact.id
            if before_call:
                before_call()  # commits: no transaction stays open during the call
            started = time.time()
            try:
                response = contact._http_post(url, headers, body, read_timeout)
            except requests.Timeout:
                raise BackendError('timeout', 'timeout after %s s' % read_timeout)
            except requests.RequestException as e:
                raise BackendError('error', 'connection failed (%s)' % type(e).__name__)
            log['http_status'] = response.status_code
            if response.status_code != 200:
                _logger.warning("AI contact %s: backend HTTP %s: %s", contact_id, response.status_code,
                                (response.text or '')[:500])
                raise BackendError('http_error', 'HTTP %s' % response.status_code, response.status_code)
            try:
                data = response.json()
            except ValueError:
                raise BackendError('invalid_response', 'invalid response: not JSON')
            log['response'] = data
            if backend_type == 'openai':
                text = contact._openai_text(data)
                if not text:
                    raise BackendError('invalid_response', 'empty reply')
                result['replies'] = [{'text': text, 'attachments': []}]
            else:
                result['replies'], result['skip'] = contact._n8n_replies(data)
                if not result['skip'] and not any(r['text'] or r['attachments'] for r in result['replies']):
                    raise BackendError('invalid_response', 'empty reply')
            log['status'] = 'ok'
        except BackendError as e:
            log['status'], log['error'] = e.status, str(e)
            if e.http_status:
                log['http_status'] = e.http_status
            result['error'] = str(e)
        except Exception as e:  # noqa: BLE001 - every failure becomes an error reply
            _logger.exception("AI contact %s: backend call failed", contact.id)
            log['status'], log['error'] = 'error', '%s: %s' % (type(e).__name__, e)
            result['error'] = log['error']
        duration_ms = int((time.time() - started) * 1000)
        contact._create_call_log(channel, job, log, duration_ms)
        return result

    # --- Call log ---

    @staticmethod
    def _redact_payload(payload):
        """Copy of a request payload without inline file data."""
        payload = copy.deepcopy(payload)
        for message in payload.get('messages', []) if isinstance(payload, dict) else []:
            content = message.get('content')
            if isinstance(content, list):
                for part in content:
                    url = (part.get('image_url') or {}).get('url') if isinstance(part, dict) else None
                    if isinstance(url, str) and url.startswith('data:'):
                        head = url.split(',', 1)[0]
                        part['image_url']['url'] = '%s,<%d base64 characters omitted>' % (
                            head, len(url) - len(head) - 1)
        return payload

    @staticmethod
    def _redact_response(data):
        data = copy.deepcopy(data)
        replies = []
        if isinstance(data, dict):
            replies = data.get('replies') if isinstance(data.get('replies'), list) else [data.get('reply')]
        for reply in replies:
            if isinstance(reply, dict):
                for att in reply.get('attachments') or []:
                    if isinstance(att, dict) and isinstance(att.get('data_base64'), str):
                        att['data_base64'] = '<%d base64 characters omitted>' % len(att['data_base64'])
        return data

    def _create_call_log(self, channel, job, log, duration_ms, backend_type=None):
        self.ensure_one()
        contact = self.sudo()
        vals = {
            'contact_id': contact.id,
            'channel_id': channel.id if channel else False,
            'job_id': job.id if job else False,
            'backend_type': backend_type or contact.backend_type,
            'status': log['status'],
            'http_status': log['http_status'] or 0,
            'duration_ms': duration_ms,
            'error': (log['error'] or '')[:MAX_ERROR_LENGTH] or False,
        }
        if contact.log_payload:
            if log['request'] is not None:
                vals['request_payload'] = _json_dumps(self._redact_payload(log['request']))[:MAX_LOG_PAYLOAD]
            if log['response'] is not None:
                vals['response_payload'] = _json_dumps(self._redact_response(log['response']))[:MAX_LOG_PAYLOAD]
        try:
            with self.env.cr.savepoint():
                return self.env['woow.ai.call.log'].sudo().create(vals)
        except Exception:  # noqa: BLE001 - a log must never break the reply
            _logger.exception("AI contact %s: could not create the call log", contact.id)
            return self.env['woow.ai.call.log']
