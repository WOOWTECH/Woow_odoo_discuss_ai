# -*- coding: utf-8 -*-
"""Backend call logs: status and timing only, readable by system administrators.

Request/response content is stored only while the AI contact's "Log Request
Content" is on (debugging); turning it off erases it. A daily cron deletes
logs older than LOG_RETENTION_DAYS.
"""

import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

LOG_RETENTION_DAYS = 30


class WoowAiCallLog(models.Model):
    _name = 'woow.ai.call.log'
    _description = 'AI Contact Call Log'
    _order = 'id desc'

    contact_id = fields.Many2one('woow.ai.contact', string='AI Contact', ondelete='cascade',
                                 index=True, readonly=True)
    channel_id = fields.Many2one('discuss.channel', string='Conversation', ondelete='set null',
                                 index=True, readonly=True)
    job_id = fields.Many2one('woow.ai.job', string='Job', ondelete='set null', readonly=True)
    backend_type = fields.Selection([('openai', 'OpenAI compatible'), ('n8n', 'n8n webhook'),
                                     ('push', 'Push API')], string='Backend', readonly=True)
    status = fields.Selection([
        ('ok', 'OK'),
        ('http_error', 'HTTP Error'),
        ('timeout', 'Timeout'),
        ('invalid_response', 'Invalid Response'),
        ('error', 'Error'),
    ], string='Status', required=True, index=True, readonly=True)
    http_status = fields.Integer(string='HTTP Status', readonly=True)
    duration_ms = fields.Integer(string='Duration (ms)', readonly=True)
    error = fields.Char(string='Error', readonly=True)
    prompt_tokens = fields.Integer(string='Prompt Tokens', readonly=True,
                                   help='From the "usage" of an OpenAI compatible answer (0 when not given).')
    completion_tokens = fields.Integer(string='Completion Tokens', readonly=True)
    total_tokens = fields.Integer(string='Total Tokens', readonly=True)
    request_payload = fields.Text(string='Request Content', readonly=True)
    response_payload = fields.Text(string='Response Content', readonly=True)

    @api.depends('contact_id')
    def _compute_display_name(self):
        for log in self:
            log.display_name = '%s #%s' % (log.contact_id.display_name or '?', log.id)

    @api.model
    def _erase_payloads(self, contact_ids=None):
        domain = ['|', ('request_payload', '!=', False), ('response_payload', '!=', False)]
        if contact_ids is not None:
            domain = [('contact_id', 'in', list(contact_ids))] + domain
        logs = self.sudo().search(domain)
        if logs:
            logs.write({'request_payload': False, 'response_payload': False})
        return len(logs)

    @api.model
    def _cleanup_old_logs(self, days=LOG_RETENTION_DAYS):
        cutoff = fields.Datetime.now() - timedelta(days=days)
        old = self.sudo().search([('create_date', '<', cutoff)])
        count = len(old)
        if count:
            old.unlink()
            _logger.info("Deleted %s AI contact call logs older than %s days", count, days)
        self.env['woow.ai.job'].sudo()._cleanup_old_jobs(days)
        return count
