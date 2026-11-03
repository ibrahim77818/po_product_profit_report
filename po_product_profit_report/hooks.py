# -*- coding: utf-8 -*-
"""Install/uninstall hooks.

The indexes this module adds live on core tables (purchase/sale orders and
lines). They are created by ``po.product.profit.engine.init()`` on every
install/upgrade (idempotent, ``IF NOT EXISTS``) and dropped on uninstall so
the module leaves no trace behind.
"""
import logging

from .models.report_engine import REPORT_INDEXES

_logger = logging.getLogger(__name__)


def post_init_hook(cr, registry):
    # Fresh statistics so the planner picks the new indexes immediately.
    for table in sorted({idx[1] for idx in REPORT_INDEXES}):
        cr.execute('ANALYZE "%s"' % table)


def uninstall_hook(cr, registry):
    for name, _table, _definition in REPORT_INDEXES:
        _logger.info('po_product_profit_report: dropping index %s', name)
        cr.execute('DROP INDEX IF EXISTS "%s"' % name)
