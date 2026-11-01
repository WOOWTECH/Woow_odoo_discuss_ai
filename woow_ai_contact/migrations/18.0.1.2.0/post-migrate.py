# -*- coding: utf-8 -*-
"""18.0.1.2.0: document_mode for existing AI contacts.

The new column is filled with the default ``file`` (documents as OpenAI file
parts). Contacts without a model are Hermes ones (README: Hermes ignores the
model, leave it empty), and Hermes refuses file parts (HTTP 400): they get
``link``. (Until 18.0.1.4.0 they got ``text``, Odoo-side text extraction,
removed by decision 39; the 18.0.1.4.0 migration turns ``text`` into
``link``.) Administrators can change it on the form.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        UPDATE woow_ai_contact SET document_mode = 'link'
         WHERE backend_type = 'openai' AND COALESCE(TRIM(model), '') = ''
    """)
    _logger.info("woow_ai_contact 18.0.1.2.0: %s AI contact(s) without a model set to document mode 'link'",
                 cr.rowcount)
