# -*- coding: utf-8 -*-
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import format_date, format_datetime, formatLang

_RTL_CHARS = re.compile('[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufeff]')


def bidi_class(text):
    """wkhtmltopdf's WebKit predates ``unicode-bidi: plaintext``, so pick the
    direction server-side: text without any RTL letter (codes, Latin names,
    "[REF] Name") is embedded left-to-right, so its brackets and dashes do
    not get mirrored on Arabic pages."""
    return '' if text and _RTL_CHARS.search(str(text)) else 'ppr-ltr'



class PoProductProfitFormatter(models.AbstractModel):
    """Locale-aware number formatting shared by the PDF and the Excel export."""
    _name = 'po.product.profit.format'
    _description = 'PO Product Profit Report Formatting'

    @api.model
    def get_formatters(self, currency):
        env = self.env
        qty_digits = env['decimal.precision'].precision_get('Product Unit of Measure')
        amount_digits = currency.decimal_places

        def fmt_qty(value):
            return formatLang(env, value or 0.0, digits=qty_digits)

        def fmt_amount(value):
            return formatLang(env, value or 0.0, digits=amount_digits)

        def fmt_pct(value):
            return '%s%%' % formatLang(env, value or 0.0, digits=1)

        def fmt_date(value):
            return format_date(env, value) if value else ''

        return {'fmt_qty': fmt_qty, 'fmt_amount': fmt_amount,
                'fmt_pct': fmt_pct, 'fmt_date': fmt_date,
                'qty_digits': qty_digits, 'amount_digits': amount_digits}


class ReportPoProductProfit(models.AbstractModel):
    _name = 'report.po_product_profit_report.report_po_product_profit'
    _description = 'PO Product Profit Report (PDF)'

    @api.model
    def _get_report_values(self, docids, data=None):
        if not data or not data.get('options'):
            raise UserError(_('Open this report from Purchase > Reporting > Product Profit.'))
        engine = self.env['po.product.profit.engine']
        options = engine.sanitize_options(data['options'])
        company, currency = engine._report_currency(options)

        summary = engine.fetch_summary(options)
        rows = summary['rows']
        labels = engine.product_labels([r['product_id'] for r in rows])
        for row in rows:
            row['product_label'] = labels.get(row['product_id'], row['product_name'])
            row['vendors'] = []

        detailed = options['mode'] == 'detailed'
        if detailed and rows:
            by_id = {r['product_id']: r for r in rows}
            for line in engine.iter_detail_rows(options, product_rows=rows):
                vendors = by_id[line['product_id']]['vendors']
                if not vendors or vendors[-1]['vendor_id'] != line['vendor_id']:
                    vendors.append(engine._new_vendor_group(line))
                engine._add_to_vendor_group(vendors[-1], line)

        values = {
            'doc_ids': docids,
            'doc_model': 'po.product.profit.wizard',
            'docs': self.env['po.product.profit.wizard'],
            'company': company,
            'currency': currency,
            'options': options,
            'filters': engine.filter_labels(options),
            'products': rows,
            'totals': summary['totals'],
            'detailed': detailed,
            'printed_at': format_datetime(self.env, fields.Datetime.now(),
                                          dt_format='yyyy-MM-dd HH:mm'),
            'bidi': bidi_class,
            'lang': self.env.lang,
        }
        values.update(self.env['po.product.profit.format'].get_formatters(currency))
        return values
