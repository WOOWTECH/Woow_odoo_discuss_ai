# -*- coding: utf-8 -*-
"""Files going to and coming from the AI backends.

- domain allowlist matching (``host`` or ``*.example.com`` per line)
- bounded download of files linked in AI replies (no redirect outside the
  allowlist, streamed with a size cap; plain http only to internal hosts)
- mimetype from the content, never from the backend; active types forced to
  ``application/octet-stream``
- signed, expiring download URLs for user attachments
- the type a user document is sent with to OpenAI compatible backends (as a
  ``file`` part, unchanged: Odoo converts nothing, decision 39)
"""

import base64
import binascii
import hashlib
import hmac
import logging
import os.path
import re
import time
from urllib.parse import unquote, urljoin, urlsplit

import requests

from odoo.tools.mimetypes import guess_mimetype

_logger = logging.getLogger(__name__)

SIGNED_URL_TTL = 3600  # seconds
SIGN_SCOPE = 'woow_ai'
MAX_REDIRECTS = 3
DOWNLOAD_TIMEOUT = (10, 60)  # connect, read (seconds)
CHUNK_SIZE = 64 * 1024
MAX_FILENAME_LENGTH = 120

# Inline image types for OpenAI-compatible backends
INLINE_IMAGE_TYPES = ('image/png', 'image/jpeg', 'image/gif', 'image/webp')

# Links in replies that are downloaded as attachments (when allowlisted)
FILE_EXTENSIONS = (
    'png', 'jpg', 'jpeg', 'gif', 'webp', 'mp4', 'webm', 'mov', 'pdf',
    'xlsx', 'xls', 'csv', 'docx', 'pptx', 'zip',
)

# Types a browser may execute or render as a document: stored as
# application/octet-stream. Exact names (plus any "+xml" suffix): a substring
# test on "xml" would also hit the OOXML office types (xlsx, docx, pptx).
ACTIVE_TYPES = frozenset((
    'text/html', 'application/xhtml+xml', 'image/svg+xml', 'text/xml', 'application/xml',
    'text/javascript', 'application/javascript', 'application/x-javascript',
    'text/ecmascript', 'application/ecmascript',
))

# User documents sent as OpenAI file parts (docs/PROTOCOL.md "Documents"):
# extension -> mimetype sent. The content decides (PDF, OOXML, OLE, text);
# the name only tells look-alikes apart (an openpyxl xlsx sniffs as a zip,
# csv / md / json sniff as text).
DOCX_MIMETYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
XLSX_MIMETYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
XLSM_MIMETYPE = 'application/vnd.ms-excel.sheet.macroenabled.12'
XLS_MIMETYPE = 'application/vnd.ms-excel'
OOXML_DOCUMENT_TYPES = {'docx': DOCX_MIMETYPE, 'xlsx': XLSX_MIMETYPE, 'xlsm': XLSM_MIMETYPE}
TEXT_DOCUMENT_TYPES = {
    'txt': 'text/plain', 'text': 'text/plain', 'log': 'text/plain', 'md': 'text/markdown',
    'markdown': 'text/markdown', 'csv': 'text/csv', 'tsv': 'text/tab-separated-values', 'json': 'application/json',
}
DOCUMENT_EXTENSIONS = frozenset(('pdf', 'xls')) | frozenset(OOXML_DOCUMENT_TYPES) | frozenset(TEXT_DOCUMENT_TYPES)
ZIP_LIKE_TYPES = ('application/zip', 'application/x-zip-compressed', 'application/octet-stream')
OLE_TYPES = (XLS_MIMETYPE, 'application/x-ole-storage', 'application/cdfv2', 'application/octet-stream')
TEXT_LIKE_TYPES = ('application/json', 'application/csv', 'application/x-ndjson')

# Hosts reachable with plain http (in-cluster / local services only)
PLAIN_HTTP_HOSTS = ('localhost', '127.0.0.1', '::1')
PLAIN_HTTP_SUFFIXES = ('.svc.cluster.local',)


class DownloadRefused(Exception):
    """The file was not downloaded (not allowlisted, too large, HTTP error...)."""


def parse_domains(text):
    """Allowlist text (one host or ``*.host`` per line, ``,`` also accepted) -> list."""
    domains = []
    for raw in re.split(r'[\s,]+', text or ''):
        raw = raw.strip().lower().rstrip('.')
        if not raw:
            continue
        if '://' in raw:
            raw = urlsplit(raw).hostname or ''
        if raw:
            domains.append(raw)
    return domains


def url_host(url):
    try:
        return (urlsplit(url).hostname or '').lower().rstrip('.')
    except ValueError:
        return ''


def host_allowed(host, domains):
    """Exact host match, or ``*.example.com`` matching any subdomain (not the apex)."""
    host = (host or '').lower().rstrip('.')
    if not host:
        return False
    for domain in domains:
        if domain.startswith('*.'):
            suffix = domain[1:]  # ".example.com"
            if host.endswith(suffix) and len(host) > len(suffix):
                return True
        elif host == domain:
            return True
    return False


def is_internal_host(host):
    """localhost or an in-cluster ``*.svc.cluster.local`` host."""
    host = (host or '').lower().rstrip('.')
    return bool(host) and (host in PLAIN_HTTP_HOSTS or host.endswith(PLAIN_HTTP_SUFFIXES))


def url_allowed(url, domains):
    """https to an allowlisted host; plain http only to an allowlisted internal host."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if not host_allowed(parts.hostname, domains):
        return False
    if parts.scheme == 'https':
        return True
    return parts.scheme == 'http' and is_internal_host(parts.hostname)


def looks_like_file(url):
    """True when the URL path ends in one of FILE_EXTENSIONS."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    ext = os.path.splitext(path)[1].lower().lstrip('.')
    return ext in FILE_EXTENSIONS


def is_active_type(mimetype):
    mimetype = (mimetype or '').split(';')[0].strip().lower()
    return mimetype in ACTIVE_TYPES or mimetype.endswith('+xml') or 'javascript' in mimetype \
        or 'ecmascript' in mimetype


def file_extension(name):
    return os.path.splitext(name or '')[1].lower().lstrip('.')


def is_document(name, mimetype=None):
    """Cheap test (no content read): a file that may be sent as a document; document_mimetype decides."""
    mimetype = (mimetype or '').split(';')[0].strip().lower()
    return file_extension(name) in DOCUMENT_EXTENSIONS or mimetype == 'application/pdf'


def document_mimetype(name, raw):
    """Mimetype a user document is sent with as an OpenAI file part, or None
    when it is not one (images, archives, media, active types: a link only)."""
    if not raw:
        return None
    sniffed = sniff_mimetype(raw)
    if is_active_type(sniffed):
        return None
    if sniffed == 'application/pdf':
        return sniffed
    if sniffed in OOXML_DOCUMENT_TYPES.values():
        return sniffed
    ext = file_extension(name)
    if ext in OOXML_DOCUMENT_TYPES and sniffed in ZIP_LIKE_TYPES and raw[:4] == b'PK\x03\x04':
        return OOXML_DOCUMENT_TYPES[ext]
    if ext == 'xls' and sniffed in OLE_TYPES and raw[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        return XLS_MIMETYPE
    if ext in TEXT_DOCUMENT_TYPES and (sniffed.startswith('text/') or sniffed in TEXT_LIKE_TYPES):
        return TEXT_DOCUMENT_TYPES[ext]
    return None


def sniff_mimetype(data):
    """Mimetype guessed from the content only (lowercase, no parameters)."""
    mimetype = guess_mimetype(data or b'', default='application/octet-stream') or 'application/octet-stream'
    return mimetype.split(';')[0].strip().lower()


def safe_mimetype(data):
    """Mimetype sniffed from the content; active content becomes octet-stream."""
    mimetype = sniff_mimetype(data)
    if is_active_type(mimetype):
        return 'application/octet-stream'
    return mimetype


def clean_filename(name, default='file'):
    name = os.path.basename(unquote(name or '').replace('\\', '/')).strip()
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', '_', name).strip(' .')
    if not name:
        name = default
    if len(name) > MAX_FILENAME_LENGTH:
        root, ext = os.path.splitext(name)
        name = root[:MAX_FILENAME_LENGTH - len(ext)] + ext
    return name


def filename_from_url(url, default='file'):
    try:
        path = urlsplit(url).path
    except ValueError:
        path = ''
    return clean_filename(path.rsplit('/', 1)[-1], default=default)


def download(url, domains, max_bytes, session=None):
    """Download an allowlisted URL; returns (bytes, final_url).

    Redirects are followed by hand (at most MAX_REDIRECTS), each hop must be
    allowlisted too. The body is streamed and the download is stopped as soon
    as it exceeds ``max_bytes``.

    Raises:
        DownloadRefused: with a short reason
    """
    http = session or requests
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        if not url_allowed(current, domains):
            raise DownloadRefused('domain not allowed: %s' % url_host(current))
        try:
            response = http.get(current, stream=True, allow_redirects=False, timeout=DOWNLOAD_TIMEOUT)
        except requests.RequestException as e:
            raise DownloadRefused('request failed: %s' % type(e).__name__)
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get('Location')
                if not location:
                    raise DownloadRefused('redirect without location')
                current = urljoin(current, location)
                continue
            if response.status_code != 200:
                raise DownloadRefused('HTTP %s' % response.status_code)
            length = response.headers.get('Content-Length')
            if length and length.isdigit() and int(length) > max_bytes:
                raise DownloadRefused('too large (%s bytes)' % length)
            chunks, total = [], 0
            for chunk in response.iter_content(CHUNK_SIZE):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise DownloadRefused('too large (> %s bytes)' % max_bytes)
                chunks.append(chunk)
            return b''.join(chunks), current
        finally:
            close = getattr(response, 'close', None)
            if close:
                close()
    raise DownloadRefused('too many redirects')


def decode_base64(data, max_bytes):
    """Decode a base64 attachment from a backend, bounded by max_bytes."""
    if not isinstance(data, str) or not data:
        raise DownloadRefused('empty data')
    if ',' in data[:100] and data.startswith('data:'):
        data = data.split(',', 1)[1]  # tolerate a data URI
    data = re.sub(r'\s+', '', data)
    if len(data) * 3 // 4 > max_bytes + 3:
        raise DownloadRefused('too large')
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise DownloadRefused('invalid base64')
    if len(raw) > max_bytes:
        raise DownloadRefused('too large')
    return raw


# --- Signed URLs for user attachments ---

def _signature(secret, attachment_id, checksum, expires):
    message = '%s|%s|%s|%s' % (SIGN_SCOPE, attachment_id, checksum or '', expires)
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def make_token(secret, attachment_id, checksum, now=None, ttl=SIGNED_URL_TTL):
    expires = int(now if now is not None else time.time()) + ttl
    return '%s.%s' % (expires, _signature(secret, attachment_id, checksum, expires)), expires


def check_token(secret, attachment_id, checksum, token, now=None):
    """True when the token was signed for this attachment and has not expired."""
    if not secret or not token or '.' not in token:
        return False
    expires, signature = token.split('.', 1)
    if not expires.isdigit():
        return False
    if int(expires) < int(now if now is not None else time.time()):
        return False
    expected = _signature(secret, attachment_id, checksum, int(expires))
    return hmac.compare_digest(expected, signature)


def human_size(size):
    size = float(size or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return ('%d %s' % (size, unit)) if unit == 'B' else ('%.1f %s' % (size, unit))
        size /= 1024
    return '%d B' % size
