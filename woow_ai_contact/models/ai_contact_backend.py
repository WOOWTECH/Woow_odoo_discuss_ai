# -*- coding: utf-8 -*-
"""Backend protocols of an AI contact: OpenAI compatible and n8n webhook.

See docs/PROTOCOL.md for the wire format. No transaction is open during the
HTTP call: the caller passes ``before_call`` (the job commits there).
"""

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime, timezone

import requests
from urllib3.exceptions import ReadTimeoutError

from odoo import api, models

from ..tools import media

_logger = logging.getLogger(__name__)

INLINE_IMAGE_MAX_BYTES = 5 * 1024 * 1024
INLINE_IMAGE_MESSAGES = 3  # the most recent user messages whose images may be inlined
INLINE_IMAGE_MAX_COUNT = 4  # images inlined per request
INLINE_IMAGE_BUDGET = 7 * 1024 * 1024  # data URI characters per request (Hermes caps requests at 10 MB)
CONNECT_TIMEOUT = 10
STREAM_IDLE_TIMEOUT = 30  # seconds without any byte from a streamed answer
RETRY_WAITS = (3, 10)  # seconds before the 1st / 2nd retry
RETRY_HTTP_STATUSES = (429, 503)
TEST_TIMEOUT = 15
MAX_LOG_PAYLOAD = 200000  # characters
MAX_ERROR_LENGTH = 300
MAX_REPLIES = 10
METADATA_MAX_KEYS = 16
METADATA_MAX_VALUE = 512
SSE_CHUNK_SIZE = 1024
EARLIER_ASSISTANT_PREFIX = '[Messages you (the assistant) sent before this user message]\n'

_THINK_RE = re.compile(r'<(think|thinking)>.*?</\1\s*>', re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r'<(?:think|thinking)>.*$', re.DOTALL | re.IGNORECASE)
# reasoning without its opening tag: "...</think>answer"
_THINK_CLOSE_RE = re.compile(r'^.*?</(?:think|thinking)\s*>', re.DOTALL | re.IGNORECASE)
_SPECIAL_TOKEN_RE = re.compile(r'<\|[a-z0-9_]{1,40}\|>', re.IGNORECASE)
_DATA_URI_RE = re.compile(r'(data:[a-z0-9.+-]+/[a-z0-9.+-]+(?:;[a-z0-9.+=-]+)*;base64),([A-Za-z0-9+/]+=*)',
                          re.IGNORECASE)


class BackendError(Exception):
    """A backend call failed; ``status`` is a woow.ai.call.log status."""

    def __init__(self, status, message, http_status=None):
        super().__init__(message)
        self.status = status
        self.http_status = http_status


def strip_think(text):
    """Remove reasoning blocks (<think>, <thinking>, also unclosed) and special tokens."""
    if not text:
        return ''
    text = _SPECIAL_TOKEN_RE.sub('', text)
    text = _THINK_RE.sub('', text)
    text = _THINK_OPEN_RE.sub('', text)
    text = _THINK_CLOSE_RE.sub('', text)
    return text.strip()


def redact(data):
    """Copy of a request / response payload without inline file data
    (data: URIs anywhere in strings, n8n ``data_base64``): only lengths stay."""
    if isinstance(data, str):
        return _DATA_URI_RE.sub(lambda m: '%s,<%d base64 characters omitted>' % (m.group(1), len(m.group(2))), data)
    if isinstance(data, dict):
        return {key: ('<%d base64 characters omitted>' % len(value)
                      if key == 'data_base64' and isinstance(value, str) else redact(value))
                for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [redact(value) for value in data]
    return data


def _turn_text(turn):
    content = turn['content']
    if isinstance(content, list):
        return '\n'.join(part.get('text') or '' for part in content if isinstance(part, dict))
    return content or ''


def normalize_turns(turns):
    """OpenAI chat turns (no system message) that start and end with a user turn.

    Hermes treats the last item as the new input, and Anthropic-style models
    would continue a trailing assistant turn or reject a leading one:

    - assistant turns before the first user turn (the welcome message) are dropped;
    - assistant turns after the last user turn (push, refusal or error
      messages) are moved just before it;
    - if that leaves assistant turns first (only one user turn), they become one
      user turn quoting them, so their content is not lost.
    """
    turns = list(turns)
    while turns and turns[0]['role'] != 'user':
        turns.pop(0)
    last_user = max((i for i, turn in enumerate(turns) if turn['role'] == 'user'), default=None)
    if last_user is None:
        return turns
    if last_user < len(turns) - 1:
        turns = turns[:last_user] + turns[last_user + 1:] + [turns[last_user]]
    lead = 0
    while turns[lead]['role'] != 'user':
        lead += 1
    if lead:
        quoted = '\n\n'.join(_turn_text(turn) for turn in turns[:lead])
        turns = [{'role': 'user', 'content': EARLIER_ASSISTANT_PREFIX + quoted}] + turns[lead:]
    return turns


def _json_dumps(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'), default=str)


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace('+00:00', 'Z')


def _signature_headers(secret, body, timestamp=None):
    """n8n request signature: hex HMAC-SHA256(secret, "<timestamp>.<body bytes>").
    The secret itself is never sent."""
    timestamp = str(int(timestamp if timestamp is not None else time.time()))
    signature = hmac.new(secret.encode(), timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
    return {'X-Woow-Timestamp': timestamp, 'X-Woow-Signature': 'sha256=%s' % signature}


def _is_read_timeout(error):
    """requests turns a read timeout while streaming into a ConnectionError."""
    if isinstance(error, requests.ReadTimeout):
        return True
    return any(isinstance(arg, ReadTimeoutError) for arg in getattr(error, 'args', ()))


def _int_or_zero(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class WoowAiContactBackend(models.Model):
    _inherit = 'woow.ai.contact'

    # --- Context ---

    @api.model
    def _session_id(self, channel):
        """Stable id of one conversation: ``woow-ai-<db uuid 8 hex>-<channel id>``."""
        uuid = self.env['ir.config_parameter'].sudo().get_param('database.uuid') or ''
        return 'woow-ai-%s-%s' % (uuid.replace('-', '')[:8], channel.id)

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
    def _identity_system_text(self, identity):
        """Identity block of the openai system message. Never the email (owner
        decision 27): what the model sees may stay in provider logs."""
        if identity.get('is_guest'):
            account = 'website visitor (guest)'
        elif identity.get('is_portal'):
            account = 'portal user (customer)'
        elif identity.get('user_id'):
            account = 'internal user (employee)'
        else:
            account = 'contact without user account'
        lines = ['You are chatting in Odoo with this person (information only, no login is delegated):']
        for label, value in (('name', identity.get('name')), ('language', identity.get('lang')),
                             ('timezone', identity.get('tz')), ('account', account),
                             ('user_id', identity.get('user_id')), ('partner_id', identity.get('partner_id'))):
            if value not in (None, '', False):
                lines.append('- %s: %s' % (label, value))
        return '\n'.join(lines)

    @api.model
    def _openai_metadata(self, session_id, identity):
        """OpenAI ``metadata``: string keys and string values only (at most 16
        keys, 512 characters each, as the OpenAI API requires). Informational:
        LiteLLM and Hermes do not pass it to the model."""
        metadata = {'session_id': session_id}
        for key, value in identity.items():
            if value in (None, '') or isinstance(value, (dict, list)):
                continue
            if isinstance(value, bool):
                value = 'true' if value else 'false'
            metadata['odoo_%s' % key] = str(value)[:METADATA_MAX_VALUE]
            if len(metadata) >= METADATA_MAX_KEYS:
                break
        return metadata

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

    @staticmethod
    def _inline_images(history):
        """Images to inline as data URIs: {(history position, attachment id): data URI}.

        Only images of the INLINE_IMAGE_MESSAGES most recent user messages,
        newest first, at most INLINE_IMAGE_MAX_COUNT images and
        INLINE_IMAGE_BUDGET characters in total. The type is sniffed from the
        content (the uploaded mimetype is not trusted).
        """
        positions = [i for i, entry in enumerate(history) if entry['role'] == 'user'][-INLINE_IMAGE_MESSAGES:]
        chosen = {}
        budget = INLINE_IMAGE_BUDGET
        for position in reversed(positions):
            for attachment in history[position]['attachments'].sudo():
                if len(chosen) >= INLINE_IMAGE_MAX_COUNT:
                    return chosen
                if (attachment.mimetype not in media.INLINE_IMAGE_TYPES
                        or (attachment.file_size or 0) > INLINE_IMAGE_MAX_BYTES):
                    continue
                raw = attachment.raw or b''
                if not raw or len(raw) > INLINE_IMAGE_MAX_BYTES:
                    continue
                mimetype = media.sniff_mimetype(raw)
                if mimetype not in media.INLINE_IMAGE_TYPES:
                    continue
                prefix = 'data:%s;base64,' % mimetype
                if len(prefix) + 4 * ((len(raw) + 2) // 3) > budget:
                    continue  # an older, smaller image may still fit
                uri = prefix + base64.b64encode(raw).decode()
                budget -= len(uri)
                chosen[(position, attachment.id)] = uri
        return chosen

    def _openai_messages(self, channel, history, identity, base_url):
        self.ensure_one()
        contact = self.sudo()
        system = [part for part in (channel._woow_ai_system_prompt(contact, history),
                                    contact._identity_system_text(identity)) if part]
        inline = self._inline_images(history)
        turns = []
        for position, entry in enumerate(history):
            lines = [entry['text']] if entry['text'] else []
            images = []
            for attachment in entry['attachments'].sudo():
                uri = inline.get((position, attachment.id))
                if uri:
                    images.append(uri)
                else:
                    lines.append(self._attachment_line(self._attachment_info(attachment, base_url)))
            text = '\n'.join(lines)
            if images:
                content = [{'type': 'text', 'text': text}] if text else []
                content += [{'type': 'image_url', 'image_url': {'url': uri}} for uri in images]
            else:
                content = text
            turns.append({'role': entry['role'], 'content': content})
        messages = [{'role': 'system', 'content': '\n\n'.join(system)}] if system else []
        return messages + normalize_turns(turns)

    def _build_request(self, channel, history, trigger_message=None):
        """Returns (url, headers, body bytes, payload for the call log)."""
        self.ensure_one()
        contact = self.sudo()
        api_key = contact._get_api_key()
        endpoint = (contact.endpoint_url or '').strip()
        if not endpoint or not api_key:
            raise BackendError('error', 'backend not configured (endpoint URL or API key missing)')
        base_url = channel.get_base_url()
        session_id = self._session_id(channel)
        identity = channel._woow_ai_identity(contact)
        headers = {'Content-Type': 'application/json'}
        if contact.backend_type == 'openai':
            url = endpoint.rstrip('/') + '/chat/completions'
            headers['Authorization'] = 'Bearer %s' % api_key
            payload = {
                'messages': contact._openai_messages(channel, history, identity, base_url),
                'user': session_id,
                'metadata': contact._openai_metadata(session_id, identity),
            }
            if (contact.model or '').strip():
                payload = {'model': contact.model.strip(), **payload}
            if contact.openai_stream:
                payload['stream'] = True
                headers['Accept'] = 'text/event-stream'
            body = _json_dumps(payload).encode()
        else:
            # n8n: the API key is only the signing secret, never sent (n8n
            # stores the webhook headers of every execution)
            url = endpoint
            payload = contact._n8n_payload(channel, history, identity, session_id, base_url, trigger_message)
            body = _json_dumps(payload).encode()
            headers.update(_signature_headers(api_key, body))
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
            'system_prompt': channel._woow_ai_system_prompt(contact, history) or '',
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
    def _sse_lines(response, deadline):
        """Lines (str) of a streamed response; checks the total deadline."""
        buffer = b''
        for chunk in response.iter_content(chunk_size=SSE_CHUNK_SIZE):
            if time.time() > deadline:
                raise BackendError('timeout', 'timeout: the streamed answer took too long')
            if not chunk:
                continue
            buffer += chunk
            *lines, buffer = buffer.split(b'\n')
            for line in lines:
                yield line.rstrip(b'\r').decode('utf-8', 'replace')
        if buffer:
            yield buffer.rstrip(b'\r').decode('utf-8', 'replace')

    @classmethod
    def _read_sse(cls, response, deadline):
        """Read an OpenAI streamed answer (server-sent events).

        Comment lines (``: keepalive``) and events other than the default
        ``message`` (e.g. ``hermes.tool.progress``) are ignored; ``delta.content``
        is accumulated, ``reasoning_content`` ignored, ``[DONE]`` ends it.

        Returns:
            dict: content (str), usage (dict or None), finish_reason, done (bool)
        """
        state = {'content': [], 'usage': None, 'finish_reason': None, 'done': False}
        event = {'type': None, 'data': []}

        def dispatch():
            data = '\n'.join(event['data'])
            kind = event['type']
            event['type'], event['data'] = None, []
            if not data or kind not in (None, '', 'message'):
                return
            if data.strip() == '[DONE]':
                state['done'] = True
                return
            try:
                chunk = json.loads(data)
            except ValueError:
                raise BackendError('invalid_response', 'invalid response: stream chunk is not JSON')
            if not isinstance(chunk, dict):
                return
            if chunk.get('error'):
                raise BackendError('invalid_response', 'invalid response: error in the stream')
            if isinstance(chunk.get('usage'), dict):
                state['usage'] = chunk['usage']
            for choice in chunk.get('choices') or []:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get('delta') or {}
                content = delta.get('content') if isinstance(delta, dict) else None
                if isinstance(content, list):
                    content = ''.join(part.get('text') or '' for part in content if isinstance(part, dict))
                if isinstance(content, str):
                    state['content'].append(content)
                if choice.get('finish_reason'):
                    state['finish_reason'] = choice['finish_reason']

        for line in cls._sse_lines(response, deadline):
            if not line:
                dispatch()
                if state['done']:
                    break
                continue
            if line.startswith(':'):
                continue  # comment / keepalive
            field, _sep, value = line.partition(':')
            if value.startswith(' '):
                value = value[1:]
            if field == 'data':
                event['data'].append(value)
            elif field == 'event':
                event['type'] = value
        if not state['done']:
            dispatch()
        if not state['done'] and not state['finish_reason']:
            raise BackendError('invalid_response', 'invalid response: the stream ended before [DONE]')
        return {'content': ''.join(state['content']), 'usage': state['usage'],
                'finish_reason': state['finish_reason'], 'done': state['done']}

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

    def _http_post(self, url, headers, body, read_timeout, stream=False, max_retries=1, deadline=None):
        """POST with explicit timeouts and bounded retries.

        Retried (at most ``max_retries`` times, after RETRY_WAITS seconds and
        only while the total ``deadline`` allows it): a failed connection (the
        request was not sent) and HTTP 429 / 503 (the backend refused before
        working). Never retried: read timeouts, 502 / 504 / 524 and every other
        answer, because the backend may already be running tools. Reads
        nothing from the database (no transaction is open here).
        """
        timeout = (CONNECT_TIMEOUT, read_timeout)
        attempt = 0
        while True:
            retry_reason = None
            try:
                response = requests.post(url, data=body, headers=headers, timeout=timeout, stream=stream)
            except requests.ConnectionError as e:
                # ConnectTimeout is both; a read timeout is never retried
                if isinstance(e, requests.ReadTimeout):
                    raise
                retry_reason, error = type(e).__name__, e
            else:
                if response.status_code not in RETRY_HTTP_STATUSES:
                    return response
                retry_reason, error = 'HTTP %s' % response.status_code, None
            wait = RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)]
            if attempt >= max_retries or (deadline and time.time() + wait >= deadline):
                if error is not None:
                    raise error
                return response
            if error is None:
                response.close()
            attempt += 1
            _logger.info("AI contact %s: %s, retry %s/%s in %s s", self._ids, retry_reason, attempt,
                         max_retries, wait)
            time.sleep(wait)

    def _call_backend(self, channel, history, job=None, before_call=None):
        """Call the backend once (bounded retries aside) and log the call.

        Returns:
            dict: replies (list of {'text', 'attachments'}), skip (bool),
                error (short text or None), call_log (woow.ai.call.log)
        """
        self.ensure_one()
        contact = self.sudo()
        trigger = job.message_id if job else None
        started = time.time()
        log = {'status': 'error', 'http_status': None, 'error': None, 'request': None, 'response': None,
               'usage': None}
        result = {'replies': [], 'skip': False, 'error': None}
        try:
            url, headers, body, payload = contact._build_request(channel, history, trigger)
            log['request'] = payload
            total_timeout = contact.timeout_seconds or 300
            backend_type = contact.backend_type
            stream = backend_type == 'openai' and bool(payload.get('stream'))
            max_retries = contact.max_retries
            contact_id = contact.id
            if before_call:
                before_call()  # commits: no transaction stays open during the call
            started = time.time()
            deadline = started + total_timeout
            read_timeout = min(STREAM_IDLE_TIMEOUT, total_timeout) if stream else total_timeout
            try:
                response = contact._http_post(url, headers, body, read_timeout, stream=stream,
                                              max_retries=max_retries, deadline=deadline)
            except requests.Timeout:
                raise BackendError('timeout', 'timeout: no answer within %s s' % read_timeout)
            except requests.RequestException as e:
                raise BackendError('error', 'connection failed (%s)' % type(e).__name__)
            try:
                log['http_status'] = response.status_code
                if response.status_code != 200:
                    _logger.warning("AI contact %s: backend HTTP %s", contact_id, response.status_code)
                    raise BackendError('http_error', 'HTTP %s' % response.status_code, response.status_code)
                content_type = (response.headers.get('Content-Type') or '').lower()
                if stream and 'text/event-stream' in content_type:
                    try:
                        streamed = contact._read_sse(response, deadline)
                    except requests.RequestException as e:
                        if _is_read_timeout(e):
                            raise BackendError('timeout', 'timeout: no data for %s s while streaming' % read_timeout)
                        raise BackendError('error', 'stream interrupted (%s)' % type(e).__name__)
                    log['response'] = {'stream': True, 'content': streamed['content'],
                                       'finish_reason': streamed['finish_reason'], 'usage': streamed['usage']}
                    log['usage'] = streamed['usage']
                    text = strip_think(streamed['content'])
                else:
                    try:
                        data = response.json()
                    except ValueError:
                        raise BackendError('invalid_response', 'invalid response: not JSON')
                    except requests.RequestException as e:
                        if _is_read_timeout(e):
                            raise BackendError('timeout', 'timeout: no data for %s s' % read_timeout)
                        raise BackendError('error', 'connection lost (%s)' % type(e).__name__)
                    log['response'] = data
                    if backend_type == 'openai':
                        log['usage'] = data.get('usage') if isinstance(data, dict) else None
                        text = contact._openai_text(data)
                    else:
                        text = None
                        result['replies'], result['skip'] = contact._n8n_replies(data)
                        if not result['skip'] and not any(r['text'] or r['attachments'] for r in result['replies']):
                            raise BackendError('invalid_response', 'empty reply')
                if text is not None:
                    if not text:
                        raise BackendError('invalid_response', 'empty reply')
                    result['replies'] = [{'text': text, 'attachments': []}]
            finally:
                close = getattr(response, 'close', None)
                if close:
                    close()
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
        result['call_log'] = contact._create_call_log(channel, job, log, duration_ms)
        return result

    # --- Test connection (system administrators) ---

    def action_test_connection(self):
        """Check the backend without running the AI: openai ``GET {endpoint}/models``
        (a chat completion would run a Hermes agent and its tools); n8n a signed
        ``{"event": "ping"}`` (any HTTP 200 is a success). Only the HTTP status
        and a short sentence are shown, never the upstream answer."""
        self.ensure_one()
        self._check_admin()
        contact = self.sudo()
        _ = self.env._
        api_key = contact._get_api_key()
        endpoint = (contact.endpoint_url or '').strip()
        status = None
        if not endpoint or not api_key:
            message, ok = _('Set the endpoint URL and the API key first.'), False
        else:
            try:
                if contact.backend_type == 'openai':
                    response = requests.get(endpoint.rstrip('/') + '/models',
                                            headers={'Authorization': 'Bearer %s' % api_key},
                                            timeout=(CONNECT_TIMEOUT, TEST_TIMEOUT), allow_redirects=False)
                else:
                    body = _json_dumps({'event': 'ping', 'contact': {'id': contact.id, 'name': contact.name}}).encode()
                    headers = {'Content-Type': 'application/json', **_signature_headers(api_key, body)}
                    response = requests.post(endpoint, data=body, headers=headers,
                                             timeout=(CONNECT_TIMEOUT, TEST_TIMEOUT), allow_redirects=False)
                status = response.status_code
                response.close()
            except requests.Timeout:
                message, ok = _('The backend did not answer within %s seconds.', TEST_TIMEOUT), False
            except requests.RequestException:
                message, ok = _('Could not connect to the backend. Check the URL and the network policy.'), False
            if status is not None:
                ok = status == 200
                if ok:
                    message = _('Connection OK (HTTP %s).', status)
                elif status in (401, 403):
                    message = _('The backend refused the key or the signature (HTTP %s).', status)
                elif status == 404:
                    message = _('Endpoint not found (HTTP %s). Check the URL.', status)
                else:
                    message = _('The backend answered HTTP %s.', status)
        _logger.info("AI contact %s: connection test by user %s: HTTP %s", contact.id, self.env.uid, status)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Test Connection'),
                'message': message,
                'type': 'success' if ok else 'danger',
                'sticky': False,
            },
        }

    # --- Call log ---

    @staticmethod
    def _redact_payload(payload):
        """Copy of a request payload without inline file data."""
        return redact(payload)

    @staticmethod
    def _redact_response(data):
        """Copy of a response without inline file data (data URIs, base64)."""
        return redact(data)

    def _create_call_log(self, channel, job, log, duration_ms, backend_type=None):
        self.ensure_one()
        contact = self.sudo()
        usage = log.get('usage') if isinstance(log.get('usage'), dict) else {}
        vals = {
            'contact_id': contact.id,
            'channel_id': channel.id if channel else False,
            'job_id': job.id if job else False,
            'backend_type': backend_type or contact.backend_type,
            'status': log['status'],
            'http_status': log['http_status'] or 0,
            'duration_ms': duration_ms,
            'error': (log['error'] or '')[:MAX_ERROR_LENGTH] or False,
            'prompt_tokens': _int_or_zero(usage.get('prompt_tokens')),
            'completion_tokens': _int_or_zero(usage.get('completion_tokens')),
            'total_tokens': _int_or_zero(usage.get('total_tokens')),
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
