# -*- coding: utf-8 -*-
"""Turning a backend reply into a Discuss message posted by the AI.

Two steps, so that slow downloads never happen while the channel row is
locked or inside a transaction that may be dropped:

1. ``_prepare_reply`` (no database writes): markdown -> safe HTML; files
   linked in the text or listed in ``attachments`` are downloaded (allowlisted
   hosts only, size-capped) or decoded (base64), their mimetype sniffed.
2. ``_post_prepared``: create the ``ir.attachment`` records and post.
"""

import logging
import re

from markupsafe import Markup

from odoo import models

from ..tools import media
from ..tools.markdown import markdown_to_html
from .ai_contact_backend import strip_think

_logger = logging.getLogger(__name__)

MAX_FILES_PER_REPLY = 10

# ![alt](url) and [text](url) with an optional "title"
_MD_LINK_RE = re.compile(
    r'(?P<bang>!?)\[(?P<label>[^\]\n]{0,500})\]\((?P<url>https?://[^\s()<>]{1,2000})(?:\s+"[^"\n]*")?\)')


class WoowAiContactReply(models.Model):
    _inherit = 'woow.ai.contact'

    def _max_download_bytes(self):
        self.ensure_one()
        return max(1, self.sudo().max_download_mb or 20) * 1024 * 1024

    def _download_domains(self):
        self.ensure_one()
        return media.parse_domains(self.sudo().download_domains)

    def _fetch_file(self, url, name=None):
        """Download an allowlisted file -> {'name', 'raw', 'mimetype'}; raises DownloadRefused."""
        self.ensure_one()
        raw, final_url = media.download(url, self._download_domains(), self._max_download_bytes())
        filename = media.clean_filename(name) if name else media.filename_from_url(final_url)
        return {'name': filename, 'raw': raw, 'mimetype': media.safe_mimetype(raw)}

    def _prepare_reply(self, reply):
        """{'text', 'attachments'} from a backend -> {'html': Markup, 'files': [...]}"""
        self.ensure_one()
        text = strip_think(reply.get('text') or '')
        files = []
        domains = self._download_domains()

        def replace_link(match):
            url = match.group('url')
            is_image = bool(match.group('bang'))
            if not (is_image or media.looks_like_file(url)):
                return match.group(0)
            if len(files) >= MAX_FILES_PER_REPLY or not media.url_allowed(url, domains):
                return match.group(0)
            try:
                file = self._fetch_file(url)
            except media.DownloadRefused as e:
                _logger.info("AI contact %s: file not attached (%s)", self.id, e)
                return match.group(0)
            files.append(file)
            return file['name']

        text = _MD_LINK_RE.sub(replace_link, text)

        extra_links = []
        for item in reply.get('attachments') or []:
            if not isinstance(item, dict) or len(files) >= MAX_FILES_PER_REPLY:
                continue
            name = item.get('name') if isinstance(item.get('name'), str) else None
            if isinstance(item.get('data_base64'), str):
                try:
                    raw = media.decode_base64(item['data_base64'], self._max_download_bytes())
                except media.DownloadRefused as e:
                    _logger.info("AI contact %s: base64 attachment dropped (%s)", self.id, e)
                    continue
                files.append({
                    'name': media.clean_filename(name, default='file'),
                    'raw': raw,
                    'mimetype': media.safe_mimetype(raw),
                })
            elif isinstance(item.get('url'), str) and item['url'].lower().startswith(('http://', 'https://')):
                url = item['url']
                try:
                    if not media.url_allowed(url, domains):
                        raise media.DownloadRefused('domain not allowed')
                    files.append(self._fetch_file(url, name=name))
                except media.DownloadRefused as e:
                    _logger.info("AI contact %s: attachment kept as a link (%s)", self.id, e)
                    extra_links.append('[%s](%s)' % ((name or media.filename_from_url(url)).replace(']', ''), url))
        if extra_links:
            text = (text + '\n\n' + '\n'.join('- %s' % link for link in extra_links)).strip()
        return {'html': markdown_to_html(text), 'files': files}

    def _post_prepared(self, channel, prepared):
        """Post a prepared reply in `channel` as the AI; returns the message (or empty)."""
        self.ensure_one()
        contact = self.sudo()
        channel = channel.sudo()
        html = prepared.get('html') or Markup('')
        files = prepared.get('files') or []
        if not str(html).strip() and not files:
            return self.env['mail.message']
        attachments = self.env['ir.attachment'].sudo()
        for file in files:
            attachment = attachments.create({
                'name': file['name'],
                'raw': file['raw'],
                'mimetype': file['mimetype'],
                'res_model': 'discuss.channel',
                'res_id': channel.id,
            })
            if attachment.mimetype != file['mimetype']:
                # ir.attachment re-sniffs "application/octet-stream" from the
                # content (giving back text/html, image/svg+xml...): force it
                # without the content, which keeps the given type
                attachment.write({'mimetype': file['mimetype']})
            attachments |= attachment
        return channel.with_context(mail_create_nosubscribe=True).message_post(
            body=Markup(html),
            author_id=contact.partner_id.id,
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            attachment_ids=attachments.ids,
        )

    def _post_replies(self, channel, prepared_list):
        messages = self.env['mail.message']
        for prepared in prepared_list:
            messages |= self._post_prepared(channel, prepared)
        return messages
