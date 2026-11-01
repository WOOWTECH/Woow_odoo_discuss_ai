# -*- coding: utf-8 -*-
"""18.0.1.4.0: the document mode ``text`` is removed (decision 39).

Odoo no longer reads the text of user documents: they are sent as they are as
OpenAI file parts (``file``) or as signed links (``link``). Contacts on
``text`` were the ones whose server does not accept file parts (Hermes
answers HTTP 400): they get ``link``.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("UPDATE woow_ai_contact SET document_mode = 'link' WHERE document_mode = 'text'")
    _logger.info("woow_ai_contact 18.0.1.4.0: %s AI contact(s) moved from document mode 'text' to 'link'",
                 cr.rowcount)
