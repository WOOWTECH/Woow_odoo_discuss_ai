# -*- coding: utf-8 -*-
"""User documents for openai backends (document_mode, decision 39): every
document is sent as it is, as an OpenAI ``file`` part with its own type (no
text extraction in Odoo), or as a signed link only; budgets shared with the
inline images, the notes the model sees, the migration of the removed
``text`` mode; n8n unchanged."""
import base64
import importlib.util
import io
import json
import os
import zipfile
from unittest.mock import patch

from odoo.tests.common import tagged

from odoo.addons.woow_ai_contact.models import ai_contact_backend
from odoo.addons.woow_ai_contact.tools import media

from .common import BACKEND_POST, TINY_PNG, AiContactCase, http_response, openai_ok

BACKEND = 'odoo.addons.woow_ai_contact.models.ai_contact_backend'
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
CSV = b'name,price\nA1,1520\n'


def load_migration(version):
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'migrations', version, 'post-migrate.py')
    spec = importlib.util.spec_from_file_location('woow_ai_contact_migrate_%s' % version.replace('.', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
class TestDocumentTypes(AiContactCase):
    """media.document_mimetype: the type a document is sent with (content first, the name for look-alikes)."""

    def test_document_mimetype(self):
        kind = media.document_mimetype
        self.assertEqual(kind('a.pdf', make_pdf('x')), 'application/pdf')
        self.assertEqual(kind('renamed.bin', make_pdf('x')), 'application/pdf')  # content decides
        self.assertEqual(kind('a.xlsx', make_xlsx()), XLSX)  # openpyxl output may sniff as a plain zip
        self.assertEqual(kind('a.docx', make_docx()), DOCX)
        self.assertEqual(kind('a.csv', CSV), 'text/csv')
        self.assertEqual(kind('a.md', b'# Title\n'), 'text/markdown')
        self.assertEqual(kind('a.json', b'{"a": 1}'), 'application/json')
        self.assertEqual(kind('a.txt', '中文'.encode()), 'text/plain')
        # active types are never sent as files, whatever the name
        self.assertIsNone(kind('page.txt', b'<html><body><script>x()</script></body></html>'))
        self.assertIsNone(kind('x.txt', b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'))
        self.assertIsNone(kind('a.png', TINY_PNG))  # images are inlined as images
        self.assertIsNone(kind('a.zip', make_docx()))  # a zip that is not named docx
        self.assertIsNone(kind('fake.docx', b'plain text, not a zip'))
        self.assertIsNone(kind('empty.txt', b''))
        self.assertTrue(media.is_document('a.XLSX', 'application/octet-stream'))
        self.assertTrue(media.is_document('scan', 'application/pdf'))
        self.assertFalse(media.is_document('movie.mp4', 'video/mp4'))

    def test_xls(self):
        try:
            import xlwt
        except ImportError:
            self.skipTest('xlwt not installed')
        book = xlwt.Workbook()
        book.add_sheet('S1').write(0, 1, 12345)
        buffer = io.BytesIO()
        book.save(buffer)
        self.assertEqual(media.document_mimetype('old.xls', buffer.getvalue()), 'application/vnd.ms-excel')


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

    def file_parts(self, content):
        return [part['file'] for part in content if isinstance(part, dict) and part['type'] == 'file']

    def test_default_mode_is_file(self):
        self.assertEqual(self.contact.document_mode, 'file')
        selection = self.env['woow.ai.contact']._fields['document_mode'].selection
        self.assertEqual([value for value, _label in selection], ['file', 'link'])

    def test_file_mode_sends_documents_as_they_are(self):
        files = (('secret.pdf', self.PDF), ('total.xlsx', make_xlsx()), ('spec.docx', make_docx()),
                 ('list.csv', CSV))
        chat = self.ask(*files)
        content = self.last_user(chat)['content']
        parts = self.file_parts(content)
        self.assertEqual([p['filename'] for p in parts], ['secret.pdf', 'total.xlsx', 'spec.docx', 'list.csv'])
        for part, (name, raw), mimetype in zip(parts, files, ('application/pdf', XLSX, DOCX, 'text/csv')):
            # the file itself with its own type: no conversion, no text read by Odoo
            self.assertEqual(part['file_data'], 'data:%s;base64,%s' % (mimetype, base64.b64encode(raw).decode()))
        text = self.text_of(content)
        self.assertTrue(text.startswith('看附件\n'))
        self.assertIn('[附件] secret.pdf（application/pdf，', text)
        self.assertIn('[附件] total.xlsx（%s，' % XLSX, text)
        self.assertEqual(text.count('檔案已附在這則訊息'), 4)
        self.assertNotIn('/woow_ai/attachment/', text)  # fully included: no link
        self.assertNotIn('PINEAPPLE', text)  # nothing extracted
        self.assertNotIn('附件內容', text)

    def test_link_mode_only_links(self):
        self.contact.document_mode = 'link'  # e.g. Hermes: HTTP 400 for file parts
        chat = self.ask(('secret.pdf', self.PDF), ('total.xlsx', make_xlsx()), ('a.png', TINY_PNG))
        content = self.last_user(chat)['content']
        self.assertFalse(self.file_parts(content))
        text = self.text_of(content)
        self.assertEqual(text.count('/woow_ai/attachment/'), 2)
        self.assertNotIn('12345', text)
        # images are inline in both modes
        self.assertEqual([p['type'] for p in content], ['text', 'image_url'])

    def test_dangerous_and_other_files_stay_links(self):
        chat = self.ask(('page.txt', b'<html><script>alert(1)</script></html>'), ('a.zip', make_docx()),
                        ('logo.svg', b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'))
        content = self.last_user(chat)['content']
        self.assertIsInstance(content, str)
        self.assertEqual(content.count('/woow_ai/attachment/'), 3)
        self.assertNotIn('附件說明', content)
        self.assertNotIn('alert(1)', content)

    def test_file_over_size_limit_is_a_link(self):
        with patch(BACKEND + '.DOC_FILE_MAX_BYTES', 100):
            chat = self.ask(('secret.pdf', self.PDF))
            content = self.last_user(chat)['content']
        self.assertIsInstance(content, str)
        self.assertIn('[附件] secret.pdf', content)
        self.assertIn('/woow_ai/attachment/', content)
        self.assertIn('[附件說明] secret.pdf：檔案太大', content)

    def test_images_and_files_share_the_data_budget(self):
        uri = len('data:image/png;base64,') + len(base64.b64encode(TINY_PNG))
        with patch(BACKEND + '.INLINE_IMAGE_BUDGET', uri + 50):
            chat = self.ask(('a.png', TINY_PNG), ('secret.pdf', self.PDF))
            content = self.last_user(chat)['content']
        self.assertEqual([part['type'] for part in content], ['text', 'image_url'])  # the PDF did not fit
        self.assertIn('[附件說明] secret.pdf：這次請求的檔案額度', content[0]['text'])
        self.assertIn('/woow_ai/attachment/', content[0]['text'])

    def test_budget_newest_first(self):
        chat = self.chat(self.internal)
        for name in ('old.csv', 'new.csv'):
            att = self.attachment(chat, self.internal, name=name, raw=b'x,y\n' * 200)
            self.post(chat, self.internal, name, attachments=att)
        one = len('data:text/csv;base64,') + len(base64.b64encode(b'x,y\n' * 200))
        with patch(BACKEND + '.INLINE_IMAGE_BUDGET', one + 10):
            messages = self.body(chat)['messages']
        self.assertEqual([p['filename'] for p in self.file_parts(messages[-1]['content'])], ['new.csv'])
        self.assertIsInstance(messages[-2]['content'], str)
        self.assertIn('[附件說明] old.csv：這次請求的檔案額度', messages[-2]['content'])
        self.assertIn('[附件] old.csv', messages[-2]['content'])

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
        self.assertEqual(len(self.file_parts(last)), ai_contact_backend.DOC_MAX_COUNT)
        self.assertEqual(self.text_of(last).count('：每次最多附上 %s 個文件' % ai_contact_backend.DOC_MAX_COUNT), 1)
        first = next(m['content'] for m in messages if isinstance(m['content'], str) and m['content'].startswith('first'))
        self.assertIn('[附件說明] first.txt：在較早的訊息中', first)

    def test_migration_text_mode_becomes_link(self):
        hermes = self.env['woow.ai.contact'].create({
            'name': 'Hermes', 'endpoint_url': 'https://hermes.example.com/v1', 'api_key': 'x'})
        self.env.flush_all()
        cr = self.env.cr
        cr.execute("UPDATE woow_ai_contact SET document_mode = 'text' WHERE id = %s", (hermes.id,))
        load_migration('18.0.1.4.0').migrate(cr, '18.0.1.3.0')
        hermes.invalidate_recordset()
        self.assertEqual(hermes.document_mode, 'link')
        self.assertEqual(self.contact.document_mode, 'file')
        # 18.0.1.2.0: contacts without a model (Hermes) get link, not the removed text mode
        cr.execute("UPDATE woow_ai_contact SET document_mode = 'file' WHERE id = %s", (hermes.id,))
        load_migration('18.0.1.2.0').migrate(cr, '18.0.1.1.0')
        hermes.invalidate_recordset()
        self.contact.invalidate_recordset()
        self.assertEqual((hermes.document_mode, self.contact.document_mode), ('link', 'file'))

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
