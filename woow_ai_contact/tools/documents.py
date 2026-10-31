# -*- coding: utf-8 -*-
"""Text of user documents, for OpenAI compatible backends (docs/PROTOCOL.md,
"Documents").

Models never open download links, so documents a user sends are read in Odoo:

- spreadsheets (xlsx / xlsm with openpyxl, xls with xlrd): one markdown table
  per sheet, with column letters and row numbers (``B1`` stays meaningful);
- docx (the zip's ``word/document.xml``, no entity resolution): paragraphs and
  tables;
- csv / tsv / txt / md / json / log: the text (UTF-8, else Big5 / cp950);
- PDF: the text layer (pdfminer, PyPDF2 as a fallback); a PDF without one
  (a scan) gives an empty text, which the caller reports.

Every extractor stops at ``max_chars`` and says whether it cut. The type comes
from the content (``media.sniff_mimetype``), the file name only decides
between look-alikes (an openpyxl xlsx sniffs as a plain zip); active types
(HTML, SVG, XML, JavaScript) are never read.
"""

import datetime
import io
import logging
import os.path
import re
import threading
import zipfile
from collections import OrderedDict

from . import media

_logger = logging.getLogger(__name__)

PDF = 'pdf'
XLSX = 'xlsx'
XLS = 'xls'
DOCX = 'docx'
TEXT = 'text'

XLSX_MIMETYPES = ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                  'application/vnd.ms-excel.sheet.macroenabled.12')
DOCX_MIMETYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
XLS_MIMETYPES = ('application/vnd.ms-excel', 'application/x-ole-storage', 'application/cdfv2',
                 'application/octet-stream')
ZIP_MIMETYPES = ('application/zip', 'application/x-zip-compressed', 'application/octet-stream')
# extension -> language of the fenced block
TEXT_EXTENSIONS = {'txt': 'text', 'text': 'text', 'log': 'text', 'md': 'markdown', 'markdown': 'markdown',
                   'csv': 'csv', 'tsv': 'tsv', 'json': 'json'}
TEXT_MIMETYPES = ('application/json', 'application/csv', 'application/x-ndjson')

PDF_MAX_PAGES = 100
SHEET_MAX_ROWS = 5000  # rows read per sheet (also bounds the time spent)
SHEET_MAX_COLUMNS = 50
DOCX_XML_MAX_BYTES = 30 * 1024 * 1024  # uncompressed word/document.xml (zip bomb guard)
CELL_MAX_CHARS = 500
CACHE_SIZE = 16  # extracted documents kept per process (each request reads the recent ones again)

_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
_SPACES_RE = re.compile(r'[ \t\r\f\v]+')
_BLANK_LINES_RE = re.compile(r'\n{3,}')


class DocumentError(Exception):
    """The document could not be read (corrupt, encrypted...); short reason."""


def extension(name):
    return os.path.splitext(name or '')[1].lower().lstrip('.')


DOCUMENT_EXTENSIONS = frozenset(('pdf', 'xlsx', 'xlsm', 'xls', 'docx')) | frozenset(TEXT_EXTENSIONS)
DOCUMENT_MIMETYPES = frozenset(('application/pdf', DOCX_MIMETYPE, 'application/vnd.ms-excel', 'text/plain',
                                'text/csv', 'text/markdown', 'text/tab-separated-values')
                               + XLSX_MIMETYPES + TEXT_MIMETYPES)


def is_candidate(name, mimetype):
    """Cheap test (no content read) of a file that may be a document; document_kind decides."""
    mimetype = (mimetype or '').split(';')[0].strip().lower()
    return extension(name) in DOCUMENT_EXTENSIONS or mimetype in DOCUMENT_MIMETYPES


def document_kind(name, raw, declared_mimetype=None):
    """PDF / XLSX / XLS / DOCX / TEXT, or None when the file is not a readable document."""
    if not raw:
        return None
    sniffed = media.sniff_mimetype(raw)
    if media.is_active_type(sniffed):
        return None
    ext = extension(name)
    if sniffed == 'application/pdf':
        return PDF
    if sniffed in XLSX_MIMETYPES or (ext in ('xlsx', 'xlsm') and sniffed in ZIP_MIMETYPES):
        return XLSX
    if sniffed == DOCX_MIMETYPE or (ext == 'docx' and sniffed in ZIP_MIMETYPES):
        return DOCX
    if ext == 'xls' and sniffed in XLS_MIMETYPES:
        return XLS
    declared = (declared_mimetype or '').split(';')[0].strip().lower()
    if (sniffed.startswith('text/') or sniffed in TEXT_MIMETYPES) and (
            ext in TEXT_EXTENSIONS or declared in ('text/plain', 'text/markdown', 'text/csv')):
        return TEXT
    return None


def text_language(name):
    return TEXT_EXTENSIONS.get(extension(name), 'text')


def fenced(text, language=''):
    """``text`` in a fenced block whose fence is longer than any backtick run inside."""
    longest = max((len(run) for run in re.findall(r'`+', text)), default=0)
    fence = '`' * max(3, longest + 1)
    return '%s%s\n%s\n%s' % (fence, language, text.rstrip('\n'), fence)


def decode_text(raw):
    """Bytes of a text file -> str: UTF-8 (BOM allowed), UTF-16 with a BOM, Big5 / cp950, else UTF-8 lossy."""
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        try:
            return raw.decode('utf-16')
        except UnicodeDecodeError:
            pass
    for encoding in ('utf-8-sig', 'cp950'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace')


class _Output:
    """Collects lines up to ``max_chars`` characters."""

    def __init__(self, max_chars):
        self.max_chars = max(0, max_chars)
        self.parts = []
        self.size = 0
        self.truncated = False

    @property
    def full(self):
        return self.truncated

    def add(self, line):
        if self.truncated:
            return False
        line = line + '\n'
        room = self.max_chars - self.size
        if len(line) > room:
            if room > 0:
                self.parts.append(line[:room])
                self.size += room
            self.truncated = True
            return False
        self.parts.append(line)
        self.size += len(line)
        return True

    def result(self):
        return ''.join(self.parts).rstrip('\n'), self.truncated


def _cut(text, max_chars):
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


# --- spreadsheets ---

def column_letter(index):
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ''
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _cell_text(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    if isinstance(value, datetime.datetime):
        return value.isoformat(sep=' ', timespec='seconds').replace(' 00:00:00', '')
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    text = str(value)
    if len(text) > CELL_MAX_CHARS:
        text = text[:CELL_MAX_CHARS] + '…'
    return text.replace('\\', '\\\\').replace('|', '\\|').replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')


def _sheet_markdown(out, title, rows, note=None):
    """rows: iterable of (row number, [values]); empty rows are skipped (row numbers kept)."""
    table = []
    width = 0
    more_rows = False
    for count, (number, values) in enumerate(rows):
        if count >= SHEET_MAX_ROWS:
            more_rows = True
            break
        cells = [_cell_text(v) for v in list(values)[:SHEET_MAX_COLUMNS]]
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue
        width = max(width, len(cells))
        table.append((number, cells))
    out.add('## %s' % title)
    if note:
        out.add(note)
    if not table:
        return out.add('(empty sheet)') and out.add('')
    if not out.add('| | %s |' % ' | '.join(column_letter(i) for i in range(width))):
        return False
    out.add('|---|%s' % ('---|' * width))
    for number, cells in table:
        cells = cells + [''] * (width - len(cells))
        if not out.add('| %s | %s |' % (number, ' | '.join(cells))):
            return False
    if more_rows:
        out.add('(only the first %s rows of this sheet are shown)' % SHEET_MAX_ROWS)
    return out.add('')


def xlsx_text(raw, max_chars):
    try:
        import openpyxl
    except ImportError:
        raise DocumentError('openpyxl is not installed')
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 - any parser failure is "unreadable"
        raise DocumentError('not a readable xlsx file (%s)' % type(e).__name__)
    out = _Output(max_chars)
    try:
        for sheet in workbook.worksheets:
            hidden = getattr(sheet, 'sheet_state', 'visible') != 'visible'
            rows = ((row[0].row if row and hasattr(row[0], 'row') else index + 1, [cell.value for cell in row])
                    for index, row in enumerate(sheet.iter_rows()))
            if not _sheet_markdown(out, 'Sheet: %s' % sheet.title, rows, note='(hidden sheet)' if hidden else None):
                break
    except Exception as e:  # noqa: BLE001
        raise DocumentError('not a readable xlsx file (%s)' % type(e).__name__)
    finally:
        workbook.close()
    return out.result()


def xls_text(raw, max_chars):
    try:
        import xlrd
    except ImportError:
        raise DocumentError('xlrd is not installed')
    try:
        book = xlrd.open_workbook(file_contents=raw, on_demand=True)
    except Exception as e:  # noqa: BLE001
        raise DocumentError('not a readable xls file (%s)' % type(e).__name__)
    out = _Output(max_chars)
    try:
        for index in range(book.nsheets):
            sheet = book.sheet_by_index(index)

            def rows(sheet=sheet):
                for r in range(sheet.nrows):
                    values = []
                    for c in range(min(sheet.ncols, SHEET_MAX_COLUMNS)):
                        cell = sheet.cell(r, c)
                        value = cell.value
                        if cell.ctype == xlrd.XL_CELL_DATE:
                            try:
                                value = xlrd.xldate.xldate_as_datetime(value, book.datemode)
                            except Exception:  # noqa: BLE001
                                pass
                        elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                            value = bool(value)
                        values.append(value)
                    yield r + 1, values

            if not _sheet_markdown(out, 'Sheet: %s' % sheet.name, rows()):
                break
    except Exception as e:  # noqa: BLE001
        raise DocumentError('not a readable xls file (%s)' % type(e).__name__)
    finally:
        book.release_resources()
    return out.result()


# --- docx ---

def _paragraph_text(paragraph):
    parts = []
    for node in paragraph.iter():
        tag = node.tag
        if tag == _W + 't' and node.text:
            parts.append(node.text)
        elif tag == _W + 'tab':
            parts.append('\t')
        elif tag in (_W + 'br', _W + 'cr'):
            parts.append('\n')
    return ''.join(parts)


def docx_text(raw, max_chars):
    from lxml import etree
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            info = archive.getinfo('word/document.xml')
            if info.file_size > DOCX_XML_MAX_BYTES:
                raise DocumentError('document too large to read')
            xml = archive.read(info)
    except DocumentError:
        raise
    except Exception as e:  # noqa: BLE001
        raise DocumentError('not a readable docx file (%s)' % type(e).__name__)
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, remove_comments=True)
    try:
        root = etree.fromstring(xml, parser=parser)
    except Exception as e:  # noqa: BLE001
        raise DocumentError('not a readable docx file (%s)' % type(e).__name__)
    body = root.find(_W + 'body')
    out = _Output(max_chars)
    for block in (body if body is not None else []):
        if block.tag == _W + 'p':
            if not out.add(_paragraph_text(block)):
                break
        elif block.tag == _W + 'tbl':
            rows = []
            for row in block.iter(_W + 'tr'):
                cells = [' '.join(_paragraph_text(p) for p in cell.iter(_W + 'p')).strip()
                         for cell in row.iter(_W + 'tc')]
                rows.append([c.replace('|', '\\|').replace('\n', ' ') for c in cells])
            if not rows:
                continue
            width = max(len(r) for r in rows)
            lines = ['| %s |' % ' | '.join(r + [''] * (width - len(r))) for r in rows]
            lines.insert(1, '|%s' % ('---|' * width))
            if not all(out.add(line) for line in lines + ['']):
                break
    text, truncated = out.result()
    return _BLANK_LINES_RE.sub('\n\n', text).strip(), truncated


# --- PDF ---

def _pdfminer_text(raw, max_chars):
    """(text, more): pages are read one by one and reading stops once
    ``max_chars`` is reached (a 60-page PDF: 2.1 s in full, 0.2 s for 10,000
    characters, measured in the sandbox)."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer
    parts, size = [], 0
    for page in extract_pages(io.BytesIO(raw), maxpages=PDF_MAX_PAGES):
        if size > max_chars:
            return ''.join(parts), True
        for element in page:
            if isinstance(element, LTTextContainer):
                text = element.get_text()
                parts.append(text)
                size += len(text)
        parts.append('\n')
    return ''.join(parts), False


def pdf_text(raw, max_chars):
    """Text layer of a PDF ('' for a scan without one)."""
    text = None
    more = False
    try:
        import pdfminer.high_level  # noqa: F401
        has_pdfminer = True
    except ImportError:
        has_pdfminer = False
    if has_pdfminer:
        try:
            text, more = _pdfminer_text(raw, max_chars)
        except Exception as e:  # noqa: BLE001 - encrypted, corrupt...
            _logger.info("PDF text extraction failed (pdfminer: %s)", type(e).__name__)
            text = None
    if text is None:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            raise DocumentError('no PDF reader installed')
        try:
            reader = PdfReader(io.BytesIO(raw))
            if reader.is_encrypted:
                raise DocumentError('the PDF is encrypted')
            text = '\n\n'.join((page.extract_text() or '') for page in reader.pages[:PDF_MAX_PAGES])
        except DocumentError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DocumentError('not a readable PDF file (%s)' % type(e).__name__)
    text = text.replace('\x0c', '\n')
    text = '\n'.join(_SPACES_RE.sub(' ', line).strip() for line in text.split('\n'))
    text, cut = _cut(_BLANK_LINES_RE.sub('\n\n', text).strip(), max_chars)
    return text, cut or more


# --- plain text ---

def plain_text(raw, max_chars, name=''):
    """Text files (csv / tsv included) are sent as they are: lossless, and models read CSV well."""
    text = decode_text(raw).replace('\r\n', '\n').replace('\r', '\n')
    if '\x00' in text:
        raise DocumentError('binary content')
    return _cut(text.strip('\n'), max_chars)


_cache = OrderedDict()
_cache_lock = threading.Lock()


def extract_cached(key, kind, raw, max_chars, name=''):
    """``extract`` memoised per process on ``key`` (the attachment checksum):
    every reply reads the documents of the recent messages again."""
    if not key:
        return extract(kind, raw, max_chars, name=name)
    cache_key = (key, kind, max_chars)
    with _cache_lock:
        if cache_key in _cache:
            _cache.move_to_end(cache_key)
            result = _cache[cache_key]
            if isinstance(result, DocumentError):
                raise DocumentError(str(result))
            return result
    try:
        result = extract(kind, raw, max_chars, name=name)
    except DocumentError as e:
        result = e
    with _cache_lock:
        _cache[cache_key] = result
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    if isinstance(result, DocumentError):
        raise DocumentError(str(result))
    return result


def extract(kind, raw, max_chars, name=''):
    """(text, truncated) of a document; raises DocumentError."""
    if kind == PDF:
        return pdf_text(raw, max_chars)
    if kind == XLSX:
        return xlsx_text(raw, max_chars)
    if kind == XLS:
        return xls_text(raw, max_chars)
    if kind == DOCX:
        return docx_text(raw, max_chars)
    if kind == TEXT:
        return plain_text(raw, max_chars, name=name)
    raise DocumentError('not a document')
