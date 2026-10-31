# -*- coding: utf-8 -*-
import re

from odoo.tests.common import BaseCase, tagged

from odoo.addons.woow_ai_contact.tools.markdown import markdown_to_html


@tagged('woow_ai_contact')
class TestMarkdown(BaseCase):

    def html(self, text):
        return str(markdown_to_html(text))

    def test_inline_formatting(self):
        html = self.html('**粗體** 與 *斜體*、`a<b`、~~刪除~~ 與 snake_case_name\n第二行')
        self.assertIn('<strong>粗體</strong>', html)
        self.assertIn('<em>斜體</em>', html)
        self.assertIn('<code>a&lt;b</code>', html)
        self.assertIn('<del>刪除</del>', html)
        self.assertIn('snake_case_name', html)
        self.assertIn('<br>', html)

    def test_headings_lists_code(self):
        html = self.html('# H1\n### H3\n\n- a\n- b\n  - b1\n\n1. one\n2. two\n\n```py\nx = "<b>"\n```')
        self.assertIn('<h3>H1</h3>', html)
        self.assertIn('<h5>H3</h5>', html)
        self.assertIn('<ul><li>a</li><li>b<ul><li>b1</li></ul></li></ul>', html)
        self.assertIn('<ol><li>one</li><li>two</li></ol>', html)
        self.assertIn('<pre><code>x = "&lt;b&gt;"</code></pre>', html.replace('&#34;', '"').replace('&quot;', '"'))

    def test_gfm_table(self):
        html = self.html('| 品名 | 價格 |\n|:---|---:|\n| A | 100 |\n| B **新** | 2\\|3 |')
        self.assertIn('<table class="table table-sm table-bordered">', html)
        self.assertIn('<th style="text-align:left">品名</th>', html)
        self.assertIn('<td style="text-align:right">100</td>', html)
        self.assertIn('<strong>新</strong>', html)
        self.assertIn('2|3', html)

    def test_links_only_safe_schemes(self):
        html = self.html('[ok](https://example.com/a?x=1&y=2) [js](javascript:alert(1)) '
                         '[mail](mailto:a@example.com) https://foo.example.com/p.')
        self.assertIn('href="https://example.com/a?x=1&amp;y=2" target="_blank" rel="noopener noreferrer"', html)
        self.assertIn('href="mailto:a@example.com"', html)
        self.assertIn('href="https://foo.example.com/p"', html)
        self.assertNotIn('javascript:', html.replace('[js](javascript:alert(1))', ''))
        self.assertNotIn('href="javascript', html)

    def test_malicious_input_is_escaped(self):
        for evil in (
            '<script>alert(1)</script>',
            '<img src=x onerror=alert(1)>',
            '[x](https://e.com/" onmouseover="alert(1))',
            '| a |\n|---|\n| <iframe src="https://evil"></iframe> |',
            '<a href="javascript:alert(1)">x</a>',
            '**<svg onload=alert(1)>**',
        ):
            html = self.html(evil)
            self.assertNotIn('<script', html, evil)
            self.assertNotIn('<img', html, evil)
            self.assertNotIn('<iframe', html, evil)
            self.assertNotIn('<svg', html, evil)
            self.assertFalse(re.search(r'<[^>]*\son\w+=', html), evil)  # no event handler attribute
            self.assertFalse(re.search(r'<a\s[^>]*href="javascript', html), evil)

    def test_quotes_are_not_mail_quotes(self):
        html = self.html('> quoted\n\n---\n\nafter')
        self.assertNotIn('data-o-mail-quote', html)
        self.assertIn('quoted', html)
        self.assertIn('after', html)

    def test_empty(self):
        self.assertEqual(self.html(''), '')
        self.assertEqual(self.html('   \n '), '')

    def test_link_hosts(self):
        text = ('[shop](https://shop.example.com/p) [cdn](https://a.cdn.example.com/x) '
                '[evil](https://evil.example.net/login) https://evil.example.net/bare '
                '<mailto:a@example.com> ![img](https://shop.example.com/i.png)')
        # default: every safe link
        self.assertEqual(self.html(text).count('<a '), 6)
        html = str(markdown_to_html(text, link_hosts=['shop.example.com', '*.cdn.example.com']))
        self.assertEqual(html.count('<a '), 3)
        self.assertIn('href="https://shop.example.com/p"', html)
        self.assertIn('href="https://a.cdn.example.com/x"', html)
        self.assertNotIn('href="https://evil.example.net', html)
        self.assertNotIn('href="mailto:', html)
        self.assertIn('evil', html)  # the label stays as text
        # the same text in a table cell
        table = str(markdown_to_html('| a |\n|---|\n| [x](https://evil.example.net) |', link_hosts=[]))
        self.assertNotIn('<a ', table)
        # string allowlist (download-domain syntax) and the hook default afterwards
        self.assertEqual(str(markdown_to_html('[s](https://shop.example.com)', link_hosts='shop.example.com'))
                         .count('<a '), 1)
        self.assertEqual(self.html('[e](https://evil.example.net)').count('<a '), 1)
