# -*- coding: utf-8 -*-
"""Turning a backend reply into a Discuss message posted by the AI.

Two steps, so that slow downloads never happen while the channel row is
locked or inside a transaction that may be dropped:

1. ``_prepare_reply`` (no database writes): markdown -> safe HTML; files
   linked in the text or listed in ``attachments`` are downloaded (allowlisted
   hosts only, size-capped) or decoded (base64, and ``data:image`` URIs in the
   text, which need no allowlist), their mimetype sniffed. Hermes ``MEDIA:``
   paths it could not turn into data are replaced by a note.
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

# data URIs in the text: ![alt](data:...) or bare (Hermes inlines images it made)
_DATA_URI = r'data:(?P<type>[a-z0-9.+-]+/[a-z0-9.+-]+)(?:;[a-z0-9.+=-]+)*;base64,(?P<data>[A-Za-z0-9+/]+=*)'
_DATA_URI_RE = re.compile(
    r'(?:!?\[(?P<label>[^\]\n]{0,500})\]\(\s*' + _DATA_URI + r'\s*(?:"[^"\n]*")?\)|' + _DATA_URI.replace(
        '?P<type>', '?P<type2>').replace('?P<data>', '?P<data2>') + ')',
    re.IGNORECASE)
_DATA_IMAGE_TYPES = ('image/png', 'image/jpeg', 'image/jpg', 'image/gif', 'image/webp')
_IMAGE_EXTENSIONS = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp'}

# Hermes "MEDIA:/path" left in the text (a file it could not send)
_MEDIA_RE = re.compile(r'`?(?<![\w/])MEDIA:[ \t]*[^\s`]+`?')


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

    def _reply_note(self, channel, source):
        """Short note put in the reply text, in the requester's language."""
        lang = None
        if channel:
            try:
                lang = channel._woow_ai_identity(self).get('lang')
            except Exception:  # noqa: BLE001 - a note is cosmetic
                lang = None
        env = self.with_context(lang=self._installed_lang(lang)).env
        notes = {
            'media': env._('(The AI produced a file that could not be delivered.)'),
            'image': env._('(An image in the reply could not be attached.)'),
        }
        return notes[source]

    def _prepare_reply(self, reply, channel=None):
        """{'text', 'attachments'} from a backend -> {'html': Markup, 'files': [...], 'warnings': [...]}

        Args:
            reply (dict): text (markdown) and attachments (n8n format)
            channel (discuss.channel): where it will be posted (link hosts and
                note language); None for the default rules
        """
        self.ensure_one()
        text = strip_think(reply.get('text') or '')
        files = []
        warnings = []
        domains = self._download_domains()
        max_bytes = self._max_download_bytes()

        def replace_data_uri(match):
            declared = (match.group('type') or match.group('type2') or '').lower()
            data = match.group('data') or match.group('data2') or ''
            label = match.group('label')
            reason = 'not an image'
            if declared in _DATA_IMAGE_TYPES:
                if len(files) >= MAX_FILES_PER_REPLY:
                    reason = 'too many files'
                else:
                    try:
                        raw = media.decode_base64(data, max_bytes)
                    except media.DownloadRefused as e:
                        reason = str(e)
                    else:
                        mimetype = media.safe_mimetype(raw)
                        if mimetype in media.INLINE_IMAGE_TYPES:
                            ext = _IMAGE_EXTENSIONS[mimetype]
                            stem = media.clean_filename(label, default='') if label else ''
                            name = media.clean_filename(
                                '%s.%s' % (stem or 'image-%s' % (len(files) + 1), ext))
                            files.append({'name': name, 'raw': raw, 'mimetype': mimetype})
                            return name
                        reason = 'content is %s' % mimetype
            warnings.append('inline %s not attached (%s)' % (declared or 'data', reason))
            return self._reply_note(channel, 'image')

        # decision 24: data:image needs no download, so no domain allowlist
        text = _DATA_URI_RE.sub(replace_data_uri, text)

        def replace_media(match):
            warnings.append('MEDIA file path in the reply (not delivered)')
            return self._reply_note(channel, 'media')

        text = _MEDIA_RE.sub(replace_media, text)

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
                    raw = media.decode_base64(item['data_base64'], max_bytes)
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
        link_hosts = channel._woow_ai_link_hosts(self) if channel else None
        return {'html': markdown_to_html(text, link_hosts=link_hosts), 'files': files, 'warnings': warnings}

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
