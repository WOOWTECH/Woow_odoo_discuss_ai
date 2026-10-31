# -*- coding: utf-8 -*-
"""User documents for openai backends (document_mode): text extraction
(xlsx, xls, docx, csv, txt, PDF), PDF file parts, budgets, truncation and
the notes the model sees; n8n unchanged."""
import base64
import io
import json
import zipfile
from unittest.mock import patch

from odoo.tests.common import tagged

from odoo.addons.woow_ai_contact.models import ai_contact_backend
from odoo.addons.woow_ai_contact.tools import documents

from .common import BACKEND_POST, TINY_PNG, AiContactCase, http_response, openai_ok

BACKEND = 'odoo.addons.woow_ai_contact.models.ai_contact_backend'


def make_pdf(text):
    """One-page PDF with `text` in its text layer ('' = a page without text, like a scan)."""
    content = ('BT /F1 18 Tf 50 700 Td (%s) Tj ET' % text) if text else ''
    objs = ['<</Type/Catalog/Pages 2 0 R>>', '<</Type/Pages/Kids[3 0 R]/Count 1>>',
            '<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>',
            '<</Length %s>>\nstream\n%s\nendstream' % (len(content), content),
            '<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>']
    out = b'%PDF-1.4\n'
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += ('%s 0 obj\n%s\nendobj\n' % (i, obj)).encode()
    xref = len(out)
    out += ('xref\n0 %s\n0000000000 65535 f \n' % (len(objs) + 1)).encode()
    for offset in offsets:
        out += ('%010d 00000 n \n' % offset).encode()
    out += ('trailer<</Size %s/Root 1 0 R>>\nstartxref\n%s\n%%%%EOF' % (len(objs) + 1, xref)).encode()
    return out


def make_xlsx():
    import openpyxl
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = '報價'
    sheet['A1'], sheet['B1'] = 'Total', 12345
    sheet['A2'], sheet['B2'] = 'a|b', 1.5
    sheet['A4'] = 'after an empty row'
    second = workbook.create_sheet('Notes')
    second['C2'] = '備註'
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def make_docx(paragraphs=('規格書', 'Model A1 costs NT$1,520.'), table=(('型號', '價格'), ('A1', '1520'))):
    body = ''.join('<w:p><w:r><w:t>%s</w:t></w:r></w:p>' % p for p in paragraphs)
    rows = ''.join('<w:tr>%s</w:tr>' % ''.join('<w:tc><w:p><w:r><w:t>%s</w:t></w:r></w:p></w:tc>' % c for c in row)
                   for row in table)
    xml = '<?xml version="1.0"?><w:document %s><w:body>%s<w:tbl>%s</w:tbl></w:body></w:document>' % (W, body, rows)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        archive.writestr('word/document.xml', xml)
    return buffer.getvalue()


@tagged('woow_ai_contact')
class TestDocumentTools(AiContactCase):
    """tools/documents.py without the ORM."""

    def test_kinds_from_content(self):
        kind = documents.document_kind
        self.assertEqual(kind('a.pdf', make_pdf('x')), documents.PDF)
        self.assertEqual(kind('renamed.bin', make_pdf('x')), documents.PDF)  # content decides
        self.assertEqual(kind('a.xlsx', make_xlsx()), documents.XLSX)
        self.assertEqual(kind('a.docx', make_docx()), documents.DOCX)
        self.assertEqual(kind('a.csv', b'a,b\n1,2\n'), documents.TEXT)
        self.assertEqual(kind('a.md', b'# Title\n'), documents.TEXT)
        self.assertEqual(kind('a.json', b'{"a": 1}'), documents.TEXT)
        self.assertEqual(kind('notes', b'hello', 'text/plain'), documents.TEXT)
        # active types are never read, whatever the name
        self.assertIsNone(kind('page.txt', b'<html><body><script>x()</script></body></html>'))
        self.assertIsNone(kind('x.txt', b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'))
        self.assertIsNone(kind('a.png', TINY_PNG))
        self.assertIsNone(kind('a.zip', make_docx()))  # a zip that is not named docx
        self.assertIsNone(kind('empty.txt', b''))

    def test_xlsx_markdown_per_sheet(self):
        text, truncated = documents.xlsx_text(make_xlsx(), 10000)
        self.assertFalse(truncated)
        self.assertIn('## Sheet: 報價', text)
        self.assertIn('| | A | B |', text)
        self.assertIn('| 1 | Total | 12345 |', text)  # B1 keeps its address
        self.assertIn('| 2 | a\\|b | 1.5 |', text)  # pipes escaped
        self.assertIn('| 4 | after an empty row |  |', text)  # row 3 skipped, numbers kept
        self.assertIn('## Sheet: Notes', text)
        self.assertIn('| 2 |  |  | 備註 |', text)

    def test_xls(self):
        try:
            import xlwt
        except ImportError:
            self.skipTest('xlwt not installed (xls files are still read with xlrd)')
        book = xlwt.Workbook()
        book.add_sheet('S1').write(0, 1, 12345)
        buffer = io.BytesIO()
        book.save(buffer)
        raw = buffer.getvalue()
        self.assertEqual(documents.document_kind('old.xls', raw), documents.XLS)
        self.assertIn('| 1 |  | 12345 |', documents.xls_text(raw, 1000)[0])

    def test_docx_paragraphs_and_tables(self):
        text, truncated = documents.docx_text(make_docx(), 10000)
        self.assertFalse(truncated)
        self.assertIn('規格書\nModel A1 costs NT$1,520.', text)
        self.assertIn('| 型號 | 價格 |\n|---|---|\n| A1 | 1520 |', text)

    def test_docx_entities_not_resolved(self):
        xml = ('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
               '<w:document %s><w:body><w:p><w:r><w:t>&x;ok</w:t></w:r></w:p></w:body></w:document>' % W)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('word/document.xml', xml)
        try:
            text, _truncated = documents.docx_text(buffer.getvalue(), 1000)
        except documents.DocumentError:
            return
        self.assertNotIn('root:', text)

    def test_pdf_text_and_scan(self):
        text, truncated = documents.pdf_text(make_pdf('The secret code is PINEAPPLE-77.'), 1000)
        self.assertEqual((text, truncated), ('The secret code is PINEAPPLE-77.', False))
        self.assertEqual(documents.pdf_text(make_pdf(''), 1000), ('', False))
        with self.assertRaises(documents.DocumentError):
            documents.pdf_text(b'%PDF-1.4 broken', 1000)

    def test_pdf_reading_stops_at_the_budget(self):
        pages = 30
        objs = ['<</Type/Catalog/Pages 2 0 R>>',
                '<</Type/Pages/Kids[%s]/Count %s>>' % (' '.join('%s 0 R' % (3 + 2 * i) for i in range(pages)), pages)]
        for i in range(pages):
            content = 'BT /F1 12 Tf 50 700 Td (PAGE-%02d %s) Tj ET' % (i, 'x' * 60)
            objs.append('<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents %s 0 R'
                        '/Resources<</Font<</F1 %s 0 R>>>>>>' % (4 + 2 * i, 3 + 2 * pages))
            objs.append('<</Length %s>>\nstream\n%s\nendstream' % (len(content), content))
        objs.append('<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>')
        out, offsets = b'%PDF-1.4\n', []
        for i, obj in enumerate(objs, 1):
            offsets.append(len(out))
            out += ('%s 0 obj\n%s\nendobj\n' % (i, obj)).encode()
        xref = len(out)
        out += ('xref\n0 %s\n0000000000 65535 f \n' % (len(objs) + 1)).encode()
        out += b''.join(('%010d 00000 n \n' % o).encode() for o in offsets)
        out += ('trailer<</Size %s/Root 1 0 R>>\nstartxref\n%s\n%%%%EOF' % (len(objs) + 1, xref)).encode()
        full, truncated = documents.pdf_text(out, 100000)
        self.assertFalse(truncated)
        self.assertIn('PAGE-29', full)
        part, truncated = documents.pdf_text(out, 200)  # stops after a few pages
        self.assertTrue(truncated)
        self.assertIn('PAGE-00', part)
        self.assertLessEqual(len(part), 200)
        self.assertNotIn('PAGE-29', part)

    def test_extraction_cached_per_checksum(self):
        documents._cache.clear()
        with patch.object(documents, 'extract', wraps=documents.extract) as extract:
            for _i in range(3):
                self.assertEqual(documents.extract_cached('sum1', documents.TEXT, b'hello', 100, 'a.txt'),
                                 ('hello', False))
            self.assertEqual(extract.call_count, 1)
            documents.extract_cached('sum2', documents.TEXT, b'other', 100, 'b.txt')
            self.assertEqual(extract.call_count, 2)
            for _i in range(2):
                with self.assertRaises(documents.DocumentError):
                    documents.extract_cached('sum3', documents.TEXT, b'a\x00b', 100, 'c.txt')
            self.assertEqual(extract.call_count, 3)  # failures are cached too
        for i in range(documents.CACHE_SIZE + 5):
            documents.extract_cached('fill%s' % i, documents.TEXT, b'x', 100)
        self.assertEqual(len(documents._cache), documents.CACHE_SIZE)

    def test_text_encodings_and_cut(self):
        self.assertEqual(documents.plain_text('名稱,價格\n燈,1520\n'.encode('cp950'), 100, 'a.csv'),
                         ('名稱,價格\n燈,1520', False))
        self.assertEqual(documents.plain_text('\ufeffhello'.encode('utf-8'), 100), ('hello', False))
        self.assertEqual(documents.plain_text(b'0123456789', 4), ('0123', True))
        with self.assertRaises(documents.DocumentError):
            documents.plain_text(b'a\x00b', 10)
        text, truncated = documents.xlsx_text(make_xlsx(), 40)
        self.assertTrue(truncated)
        self.assertLessEqual(len(text), 40)

    def test_fence_longer_than_content(self):
        self.assertEqual(documents.fenced('a ``` b', 'md'), '````md\na ``` b\n````')
        self.assertEqual(documents.fenced('x'), '```\nx\n```')


@tagged('woow_ai_contact')
class TestDocumentRequests(AiContactCase):

    PDF = make_pdf('The secret code is PINEAPPLE-77.')

    def ask(self, *files, text='看附件', contact=None, user=None):
        user = user or self.internal
        chat = self.chat(user, contact)
        atts = self.env['ir.attachment']
        for name, raw in files:
            atts |= self.attachment(chat, user, name=name, raw=raw)
        self.post(chat, user, text, attachments=atts)
        return chat

    def body(self, chat):
        with patch(BACKEND_POST, return_value=openai_ok()) as post:
            self.jobs(chat)[-1]._run_job(commit=False)
        return json.loads(post.call_args.kwargs['data'])

    def last_user(self, chat):
        return self.body(chat)['messages'][-1]

    @staticmethod
    def text_of(content):
        if isinstance(content, str):
            return content
        return '\n'.join(part['text'] for part in content if part['type'] == 'text')

    def test_default_mode_is_file(self):
        self.assertEqual(self.contact.document_mode, 'file')

    def test_file_mode_pdf_part_and_xlsx_text(self):
        chat = self.ask(('secret.pdf', self.PDF), ('total.xlsx', make_xlsx()))
        content = self.last_user(chat)['content']
        files = [part for part in content if part['type'] == 'file']
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]['file']['filename'], 'secret.pdf')
        self.assertEqual(files[0]['file']['file_data'],
                         'data:application/pdf;base64,' + base64.b64encode(self.PDF).decode())
        text = self.text_of(content)
        self.assertTrue(text.startswith('看附件\n'))
        self.assertIn('[附件] secret.pdf（application/pdf，', text)
        self.assertIn('檔案已附在這則訊息', text)
        self.assertIn('[附件內容] total.xlsx', text)
        self.assertIn('```markdown\n## Sheet: 報價', text)
        self.assertIn('| 1 | Total | 12345 |', text)
        self.assertNotIn('/woow_ai/attachment/', text)  # fully included: no link

    def test_text_mode_reads_pdf_and_docx(self):
        self.contact.document_mode = 'text'
        chat = self.ask(('secret.pdf', self.PDF), ('spec.docx', make_docx()), ('list.csv', b'name,price\nA1,1520\n'))
        content = self.last_user(chat)['content']
        self.assertIsInstance(content, str)  # no file part: plain text for any server (Hermes)
        self.assertIn('[附件內容] secret.pdf（application/pdf，', content)
        self.assertIn('```\nThe secret code is PINEAPPLE-77.\n```', content)
        self.assertIn('[附件內容] spec.docx', content)
        self.assertIn('| A1 | 1520 |', content)
        self.assertIn('```csv\nname,price\nA1,1520\n```', content)

    def test_link_mode_is_the_old_behaviour(self):
        self.contact.document_mode = 'link'
        chat = self.ask(('secret.pdf', self.PDF), ('total.xlsx', make_xlsx()))
        content = self.last_user(chat)['content']
        self.assertIsInstance(content, str)
        self.assertEqual(content.count('/woow_ai/attachment/'), 2)
        self.assertNotIn('附件內容', content)
        self.assertNotIn('12345', content)

    def test_scanned_pdf_note(self):
        self.contact.document_mode = 'text'
        chat = self.ask(('scan.pdf', make_pdf('')))
        content = self.last_user(chat)['content']
        self.assertIn('[附件] scan.pdf', content)  # the link stays
        self.assertIn('[附件說明] scan.pdf：這個 PDF 沒有文字層', content)

    def test_unreadable_document_note(self):
        chat = self.ask(('broken.xlsx', b'PK\x03\x04 not really a zip'))
        content = self.last_user(chat)['content']
        self.assertIn('[附件] broken.xlsx', content)
        self.assertIn('[附件說明] broken.xlsx：無法讀取內容', content)

    def test_dangerous_and_other_files_stay_links(self):
        chat = self.ask(('page.txt', b'<html><script>alert(1)</script></html>'), ('a.zip', make_docx()))
        content = self.last_user(chat)['content']
        self.assertEqual(content.count('/woow_ai/attachment/'), 2)
        self.assertNotIn('附件內容', content)
        self.assertNotIn('附件說明', content)
        self.assertNotIn('alert(1)', content)

    def test_truncated_document_keeps_link(self):
        with patch(BACKEND + '.DOC_TEXT_MAX_CHARS', 1200), patch(BACKEND + '.DOC_TEXT_MIN_CHARS', 10):
            chat = self.ask(('long.txt', ('line %05d\n' % 1).encode() * 1000))
            content = self.last_user(chat)['content']
        self.assertIn('[附件內容] long.txt', content)
        self.assertIn('[附件說明] long.txt：內容太長，只附上前 1200 字', content)
        self.assertIn('[附件] long.txt', content)
        self.assertIn('/woow_ai/attachment/', content)

    def test_text_budget_newest_first(self):
        chat = self.chat(self.internal)
        for name in ('old.txt', 'new.txt'):
            att = self.attachment(chat, self.internal, name=name, raw=b'x' * 900)
            self.post(chat, self.internal, name, attachments=att)
        with patch(BACKEND + '.DOC_TEXT_BUDGET', 1000), patch(BACKEND + '.DOC_TEXT_MIN_CHARS', 500):
            messages = self.body(chat)['messages']
        self.assertIn('[附件內容] new.txt', messages[-1]['content'])
        self.assertIn('[附件說明] old.txt：內容沒有附上：這次請求的文件內容額度', messages[-2]['content'])
        self.assertIn('[附件] old.txt', messages[-2]['content'])

    def test_count_and_age_limits(self):
        chat = self.chat(self.internal)
        old = self.attachment(chat, self.internal, name='first.txt', raw=b'first')
        self.post(chat, self.internal, 'first', attachments=old)
        for i in range(ai_contact_backend.DOC_MESSAGES):
            self.post(chat, self.internal, 'filler %s' % i)
        many = self.env['ir.attachment']
        for i in range(ai_contact_backend.DOC_MAX_COUNT + 1):
            many |= self.attachment(chat, self.internal, name='f%s.txt' % i, raw=b'file %d' % i)
        self.post(chat, self.internal, 'many', attachments=many)
        messages = self.body(chat)['messages']
        last = messages[-1]['content']
        self.assertEqual(last.count('[附件內容] '), ai_contact_backend.DOC_MAX_COUNT)
        self.assertEqual(last.count('：內容沒有附上：每次最多附上 %s 個文件' % ai_contact_backend.DOC_MAX_COUNT), 1)
        first = next(m['content'] for m in messages if m['content'].startswith('first'))
        self.assertIn('[附件說明] first.txt：在較早的訊息中', first)
        self.assertNotIn('附件內容', first)

    def test_pdf_over_file_limit_sent_as_text(self):
        with patch(BACKEND + '.DOC_FILE_MAX_BYTES', 100):
            chat = self.ask(('secret.pdf', self.PDF))
            content = self.last_user(chat)['content']
        self.assertIsInstance(content, str)
        self.assertIn('PINEAPPLE-77', content)
        self.assertIn('[附件說明] secret.pdf：PDF 太大，無法直接附上檔案', content)

    def test_images_and_pdf_share_the_data_budget(self):
        uri = len('data:image/png;base64,') + len(base64.b64encode(TINY_PNG))
        with patch(BACKEND + '.INLINE_IMAGE_BUDGET', uri + 50):
            chat = self.ask(('a.png', TINY_PNG), ('secret.pdf', self.PDF))
            content = self.last_user(chat)['content']
        types = [part['type'] for part in content]
        self.assertEqual(types, ['text', 'image_url'])  # the PDF did not fit: its text instead
        self.assertIn('PINEAPPLE-77', content[0]['text'])

    def test_bot_attachments_never_read(self):
        chat = self.chat(self.internal)
        self.post(chat, self.internal, 'q1')
        att = self.env['ir.attachment'].create({'name': 'bot.txt', 'raw': b'BOT CONTENT', 'res_model': 'discuss.channel',
                                                'res_id': chat.id})
        chat.message_post(body='here', author_id=self.contact.partner_id.id, message_type='comment',
                          subtype_xmlid='mail.mt_comment', attachment_ids=att.ids)
        self.post(chat, self.internal, 'q2')
        body = json.dumps(self.body(chat), ensure_ascii=False)
        self.assertNotIn('BOT CONTENT', body)
        self.assertIn('[附件] bot.txt', body)

    def test_call_log_redacts_file_data(self):
        self.contact.log_payload = True
        chat = self.ask(('secret.pdf', self.PDF))
        self.body(chat)
        log = self.env['woow.ai.call.log'].search([('channel_id', '=', chat.id)], order='id desc', limit=1)
        self.assertIn('data:application/pdf;base64,<', log.request_payload)
        self.assertNotIn(base64.b64encode(self.PDF).decode()[:40], log.request_payload)

    def test_only_needed_files_are_read(self):
        chat = self.chat(self.internal)
        for i in range(4):
            att = self.attachment(chat, self.internal, name='z%s.zip' % i, raw=make_docx())
            self.post(chat, self.internal, 'zip %s' % i, attachments=att)
        doc = self.attachment(chat, self.internal, name='n.txt', raw=b'note')
        self.post(chat, self.internal, 'txt', attachments=doc)
        self.env.flush_all()
        self.env.invalidate_all()
        Attachment = type(self.env['ir.attachment'])
        original = Attachment._file_read
        read = []

        def file_read(attachment, fname):
            read.append(fname)
            return original(attachment, fname)

        with patch.object(Attachment, '_file_read', autospec=True, side_effect=file_read):
            self.body(chat)
        self.assertEqual(read, [doc.store_fname])  # not every attachment of the history

    def test_n8n_unchanged(self):
        chat = self.ask(('secret.pdf', self.PDF), contact=self.n8n)
        with patch(BACKEND_POST, return_value=http_response(200, {'reply': 'ok'})) as post:
            self.jobs(chat)[-1]._run_job(commit=False)
        payload = json.loads(post.call_args.kwargs['data'])
        attachment = payload['message']['attachments'][0]
        self.assertEqual(set(attachment), {'id', 'name', 'mimetype', 'size', 'url', 'expires_at'})
        self.assertNotIn('PINEAPPLE', json.dumps(payload))
