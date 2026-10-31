# -*- coding: utf-8 -*-
import base64
from unittest.mock import patch

from odoo.tests.common import BaseCase, tagged

from odoo.addons.woow_ai_contact.tools import media

from .common import MEDIA_GET, TINY_PDF, TINY_PNG, http_response


@tagged('woow_ai_contact')
class TestMedia(BaseCase):

    domains = media.parse_domains('files.example.com\n*.cdn.example.com, https://other.example.org/x')

    def test_allowlist(self):
        self.assertEqual(self.domains, ['files.example.com', '*.cdn.example.com', 'other.example.org'])
        self.assertTrue(media.url_allowed('https://files.example.com/a.png', self.domains))
        self.assertTrue(media.url_allowed('https://a.b.cdn.example.com/a.png', self.domains))
        self.assertFalse(media.url_allowed('https://cdn.example.com/a.png', self.domains))  # apex not matched
        self.assertFalse(media.url_allowed('https://files.example.com.evil.com/a.png', self.domains))
        self.assertFalse(media.url_allowed('https://evilfiles.example.com/a.png', self.domains))
        self.assertFalse(media.url_allowed('ftp://files.example.com/a.png', self.domains))
        self.assertFalse(media.url_allowed('https://user@evil.com/a.png', self.domains))

    def test_download_ok(self):
        with patch(MEDIA_GET, return_value=http_response(200, chunks=[TINY_PNG[:10], TINY_PNG[10:]])) as get:
            raw, final = media.download('https://files.example.com/a.png', self.domains, 1000)
        self.assertEqual(raw, TINY_PNG)
        self.assertEqual(final, 'https://files.example.com/a.png')
        self.assertFalse(get.call_args.kwargs['allow_redirects'])
        self.assertTrue(get.call_args.kwargs['stream'])
        self.assertEqual(get.call_args.kwargs['timeout'], media.DOWNLOAD_TIMEOUT)

    def test_download_redirect_outside_allowlist(self):
        redirect = http_response(302, headers={'Location': 'https://evil.example.net/a.png'})
        with patch(MEDIA_GET, return_value=redirect) as get:
            with self.assertRaises(media.DownloadRefused):
                media.download('https://files.example.com/a.png', self.domains, 1000)
        self.assertEqual(get.call_count, 1)  # the outside URL is never requested

    def test_download_redirect_inside_allowlist(self):
        responses = [http_response(301, headers={'Location': '/b.png'}), http_response(200, chunks=[TINY_PNG])]
        with patch(MEDIA_GET, side_effect=responses) as get:
            raw, final = media.download('https://files.example.com/a.png', self.domains, 1000)
        self.assertEqual(final, 'https://files.example.com/b.png')
        self.assertEqual(get.call_count, 2)

    def test_download_too_large(self):
        with patch(MEDIA_GET, return_value=http_response(200, headers={'Content-Length': '5000'}, chunks=[b'x'])):
            with self.assertRaises(media.DownloadRefused):
                media.download('https://files.example.com/a.png', self.domains, 1000)
        # no / lying Content-Length: the stream is cut while reading
        big = http_response(200, chunks=[b'x' * 600, b'x' * 600, b'x' * 600])
        with patch(MEDIA_GET, return_value=big):
            with self.assertRaises(media.DownloadRefused):
                media.download('https://files.example.com/a.png', self.domains, 1000)
        self.assertTrue(big.close.called)

    def test_download_not_allowlisted(self):
        with patch(MEDIA_GET) as get:
            with self.assertRaises(media.DownloadRefused):
                media.download('https://evil.example.net/a.png', self.domains, 1000)
        get.assert_not_called()

    def test_mimetype_sniffed(self):
        self.assertEqual(media.safe_mimetype(TINY_PNG), 'image/png')
        self.assertEqual(media.safe_mimetype(TINY_PDF), 'application/pdf')
        # fake image: HTML / SVG content become octet-stream whatever the name says
        self.assertEqual(media.safe_mimetype(b'<html><body><script>alert(1)</script></body></html>'),
                         'application/octet-stream')
        self.assertEqual(media.safe_mimetype(b'<svg xmlns="http://www.w3.org/2000/svg" onload="x()"></svg>'),
                         'application/octet-stream')

    def test_base64(self):
        data = base64.b64encode(TINY_PNG).decode()
        self.assertEqual(media.decode_base64(data, 1000), TINY_PNG)
        self.assertEqual(media.decode_base64('data:image/png;base64,' + data, 1000), TINY_PNG)
        with self.assertRaises(media.DownloadRefused):
            media.decode_base64(data, 10)
        with self.assertRaises(media.DownloadRefused):
            media.decode_base64('not base64 !!', 1000)

    def test_signed_token(self):
        token, expires = media.make_token('secret', 12, 'abc', now=1000)
        self.assertEqual(expires, 1000 + media.SIGNED_URL_TTL)
        self.assertTrue(media.check_token('secret', 12, 'abc', token, now=1000))
        self.assertTrue(media.check_token('secret', 12, 'abc', token, now=expires))
        self.assertFalse(media.check_token('secret', 12, 'abc', token, now=expires + 1))  # expired
        self.assertFalse(media.check_token('secret', 13, 'abc', token, now=1000))  # other attachment
        self.assertFalse(media.check_token('secret', 12, 'abd', token, now=1000))  # content changed
        self.assertFalse(media.check_token('other', 12, 'abc', token, now=1000))  # other database
        exp, sig = token.split('.')
        self.assertFalse(media.check_token('secret', 12, 'abc', '%s.%s' % (int(exp) + 3600, sig), now=1000))
        self.assertFalse(media.check_token('secret', 12, 'abc', exp + '.' + sig[:-1] + '0', now=1000)
                         if sig[-1] != '0' else False)
        self.assertFalse(media.check_token('secret', 12, 'abc', 'garbage', now=1000))

    def test_filenames(self):
        self.assertEqual(media.filename_from_url('https://f.example.com/a/%E5%A0%B1%E5%83%B9.xlsx?x=1'), '報價.xlsx')
        self.assertEqual(media.clean_filename('../../etc/passwd'), 'passwd')
        self.assertEqual(media.clean_filename(''), 'file')
        self.assertTrue(media.looks_like_file('https://x.example.com/a.PDF'))
        self.assertFalse(media.looks_like_file('https://x.example.com/page.html'))
