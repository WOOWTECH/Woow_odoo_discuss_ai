"""Legacy recovery cleanup must include cron records disabled by administrators."""
from copy import deepcopy
from io import BytesIO

from lxml import etree

from odoo.tests import TransactionCase, tagged
from odoo.tools import file_open
from odoo.tools.convert import convert_xml_import


def exercise_legacy_cleanup(env):
    cron = env['ir.cron'].with_context(active_test=False)
    model = env['ir.model']._get_id('woow.ai.job')
    retired = cron.create([
        {'name': 'Upgrade cleanup active', 'model_id': model, 'state': 'code',
         'code': 'model._cron_recover_jobs()', 'active': True},
        {'name': 'Upgrade cleanup inactive', 'model_id': model, 'state': 'code',
         'code': 'model._cron_recover_jobs()', 'active': False},
    ])
    actions = retired.ir_actions_server_id
    retained = cron.create({'name': 'Upgrade cleanup retained', 'model_id': model,
                           'state': 'code', 'code': 'model._cron_run_jobs()', 'active': False})
    with file_open('woow_ai_contact/data/data.xml', 'rb') as source:
        document = etree.parse(source)
    root = etree.Element('odoo')
    for node in document.xpath('/odoo/delete'):
        root.append(deepcopy(node))
    assert len(root) == 2, 'Expected both legacy cleanup operations'
    for _ in range(2):  # A second module update must also be safe.
        source = BytesIO(etree.tostring(root))
        source.name = 'legacy-cron-cleanup.xml'
        convert_xml_import(env, 'woow_ai_contact', source, {}, mode='update')
        assert not retired.exists(), 'Legacy active and inactive crons must be removed'
        assert not actions.exists(), 'Legacy server actions must be removed'
        assert retained.exists(), 'Unrelated dispatcher must be retained'


@tagged('post_install', '-at_install')
class TestUpgradeCleanup(TransactionCase):
    def test_active_and_inactive_legacy_recovery(self):
        exercise_legacy_cleanup(self.env)
