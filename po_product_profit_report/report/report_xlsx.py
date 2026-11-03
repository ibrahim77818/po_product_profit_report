# -*- coding: utf-8 -*-
"""Excel export.

* ``constant_memory`` mode: xlsxwriter flushes each row to disk as soon as the
  next one starts, and detail rows are streamed from a server-side cursor, so
  memory stays flat even for hundreds of thousands of PO rows.
* Subtotals and grand totals are live ``SUBTOTAL(9, ...)`` formulas (with the
  computed value cached so previewers show numbers too). ``SUBTOTAL`` ignores
  nested subtotals and respects AutoFilter, so filtering the sheet keeps the
  grand total right.
* Detailed mode uses Excel outline levels: collapse to product totals with
  the [1]/[2]/[3] buttons on the left.
"""
import os
import tempfile

import xlsxwriter
from xlsxwriter.utility import xl_col_to_name, xl_rowcol_to_cell

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import format_datetime

EXCEL_MAX_ROWS = 1048000


class PoProductProfitXlsx(models.AbstractModel):
    _name = 'po.product.profit.xlsx'
    _description = 'PO Product Profit Report (Excel)'

    # ------------------------------------------------------------------ #
    @api.model
    def _columns(self, detailed):
        """(key, label, width, kind) -- kind drives the number format."""
        if detailed:
            return [
                ('vendor', _('Vendor'), 28, 'text'),
                ('po', _('Purchase Order'), 15, 'text'),
                ('doc', _('Document'), 10, 'text'),
                ('date', _('Purchase Date'), 13, 'date'),
                ('item', _('Item'), 40, 'text'),
                ('barcode', _('Barcode'), 16, 'text'),
                ('qty', _('Purchased Qty'), 14, 'qty'),
                ('amount', _('Purchased Amount'), 17, 'amount'),
                ('avg_cost', _('Avg. PO Rate'), 13, 'amount'),
                ('po_count', _('No. of POs'), 10, 'int'),
                ('received', _('Received Qty'), 13, 'qty'),
                ('pending', _('Pending Qty'), 13, 'qty'),
                ('sold_qty', _('Sold Qty'), 13, 'qty'),
                ('sale_rate', _('Sale Rate'), 13, 'amount'),
                ('sales_amount', _('Sales Amount'), 16, 'amount'),
                ('cogs', _('Cost of Sold Qty'), 16, 'amount'),
                ('gross_profit', _('Gross Profit'), 16, 'amount'),
                ('margin', _('Margin %'), 10, 'pct'),
            ]
        return [
            ('item', _('Item'), 42, 'text'),
            ('barcode', _('Barcode'), 16, 'text'),
            ('vendor', _('Vendor'), 28, 'text'),
            ('qty', _('Purchased Qty'), 14, 'qty'),
            ('amount', _('Purchased Amount'), 17, 'amount'),
            ('avg_cost', _('Avg. PO Rate'), 13, 'amount'),
            ('po_count', _('No. of POs'), 10, 'int'),
            ('received', _('Received Qty'), 13, 'qty'),
            ('pending', _('Pending Qty'), 13, 'qty'),
            ('sold_qty', _('Sold Qty'), 13, 'qty'),
            ('sale_rate', _('Sale Rate'), 13, 'amount'),
            ('sales_amount', _('Sales Amount'), 16, 'amount'),
            ('cogs', _('Cost of Sold Qty'), 16, 'amount'),
            ('gross_profit', _('Gross Profit'), 16, 'amount'),
            ('margin', _('Margin %'), 10, 'pct'),
        ]

    @api.model
    def _formats(self, workbook, qty_digits, amount_digits, rtl):
        def num(digits):
            return '#,##0' + ('.' + '0' * digits if digits else '')

        align = 'right' if rtl else 'left'
        base = {'font_name': 'Arial', 'font_size': 9, 'valign': 'vcenter',
                'reading_order': 2 if rtl else 1}
        styles = {
            'title': {'bold': True, 'font_size': 15, 'font_color': '#1F3864'},
            'subtitle': {'font_color': '#595959'},
            'flabel': {'bold': True, 'font_color': '#404040', 'bg_color': '#F2F2F2',
                       'border': 1, 'border_color': '#D9D9D9'},
            'fvalue': {'border': 1, 'border_color': '#D9D9D9', 'text_wrap': True,
                       'align': align},
            'header': {'bold': True, 'font_color': '#FFFFFF', 'bg_color': '#1F3864',
                       'text_wrap': True, 'align': 'center', 'border': 1,
                       'border_color': '#1F3864'},
            'product': {'bold': True, 'bg_color': '#DDEBF7', 'top': 1, 'top_color': '#9BC2E6'},
            'vendor': {'italic': True, 'bg_color': '#F7F9FC', 'font_color': '#404040'},
            'line': {},
            'total': {'bold': True, 'bg_color': '#FCE4D6', 'top': 6, 'bottom': 1},
        }
        kinds = {'text': {'align': align}, 'date': {'num_format': 'yyyy-mm-dd', 'align': 'center'},
                 'qty': {'num_format': num(qty_digits)},
                 'amount': {'num_format': num(amount_digits)},
                 'int': {'num_format': '#,##0'},
                 'pct': {'num_format': '0.0%'}}
        fmt = {}
        for name, props in styles.items():
            fmt[name] = workbook.add_format(dict(base, **props))
            if name in ('product', 'vendor', 'line', 'total'):
                for kind, kprops in kinds.items():
                    fmt[(name, kind)] = workbook.add_format(dict(base, **props, **kprops))
                    neg = dict(base, **props, **kprops)
                    neg['font_color'] = '#C00000'
                    fmt[(name, kind, 'neg')] = workbook.add_format(neg)
        return fmt

    # ------------------------------------------------------------------ #
    @api.model
    def generate(self, options):
        """Return (filename, xlsx bytes)."""
        engine = self.env['po.product.profit.engine']
        options = engine.sanitize_options(options)
        company, currency = engine._report_currency(options)
        detailed = options['mode'] == 'detailed'
        summary = engine.fetch_summary(options)
        totals = summary['totals']
        rows_needed = (totals['detail_count'] + 3 * totals['count']) if detailed else totals['count']
        if rows_needed + 20 > EXCEL_MAX_ROWS:
            raise UserError(_('This selection exceeds the Excel row limit. Narrow the filters '
                              'or use "Totals Only".'))
        lang = self.env['res.lang']._lang_get(self.env.lang or 'en_US')
        rtl = bool(lang and lang.direction == 'rtl')
        formatters = self.env['po.product.profit.format'].get_formatters(currency)
        labels = engine.product_labels([r['product_id'] for r in summary['rows']])
        filters = engine.filter_labels(options)

        fd, path = tempfile.mkstemp(suffix='.xlsx', prefix='po_ppr_')
        os.close(fd)
        try:
            workbook = xlsxwriter.Workbook(path, {'constant_memory': True,
                                                  'strings_to_numbers': False,
                                                  'default_date_format': 'yyyy-mm-dd'})
            fmt = self._formats(workbook, formatters['qty_digits'],
                                formatters['amount_digits'], rtl)
            sheet = workbook.add_worksheet(_('Product Profit')[:31])
            if rtl:
                sheet.right_to_left()
            columns = self._columns(detailed)
            col = {c[0]: i for i, c in enumerate(columns)}
            for i, (_key, _label, width, _kind) in enumerate(columns):
                sheet.set_column(i, i, width)
            sheet.outline_settings(True, False, False, False)  # summary rows above detail

            # --- header block -------------------------------------------
            last_col = len(columns) - 1
            sheet.set_row(0, 24)
            sheet.merge_range(0, 0, 0, last_col, _('PO Product Profit Report'), fmt['title'])
            sheet.merge_range(1, 0, 1, last_col, '%s · %s · %s %s' % (
                company.name, filters['period'], _('Printed'),
                format_datetime(self.env, fields.Datetime.now())), fmt['subtitle'])
            filter_rows = [
                (_('Period'), filters['period'], _('Document Type'), filters['doc_type']),
                (_('Vendors'), filters['vendors'], _('Products'), filters['products']),
                (_('Warehouses'), filters['warehouses'], _('Companies'), filters['companies']),
                (_('Report Mode'), filters['mode'], _('Sold Quantity Basis'), filters['sale_basis']),
                (_('Currency'), filters['currency'], _('Search'), filters['search'] or '-'),
            ]
            # labels span two columns on the right half so long ones
            # ("Sold Quantity Basis") are never clipped by a narrow column
            half = max(last_col // 2, 3)
            r = 3
            for l1, v1, l2, v2 in filter_rows:
                sheet.write(r, 0, l1, fmt['flabel'])
                sheet.merge_range(r, 1, r, half - 1, v1, fmt['fvalue'])
                sheet.merge_range(r, half, r, half + 1, l2, fmt['flabel'])
                sheet.merge_range(r, half + 2, r, last_col, v2, fmt['fvalue'])
                r += 1
            r += 1
            header_row = r
            sheet.set_row(header_row, 30)
            for i, (_key, label, _w, _k) in enumerate(columns):
                sheet.write(header_row, i, label, fmt['header'])
            sheet.freeze_panes(header_row + 1, 0)
            first_data = header_row + 1

            writer = _RowWriter(sheet, fmt, columns, col)
            if detailed:
                end = self._write_detailed(writer, engine, options, summary, labels, first_data)
            else:
                end = self._write_totals(writer, summary, labels, first_data)
            self._write_grand_total(writer, totals, first_data, end, detailed)
            sheet.autofilter(header_row, 0, max(end - 1, header_row), last_col)
            sheet.set_landscape()
            sheet.set_paper(9)
            sheet.fit_to_pages(1, 0)
            sheet.repeat_rows(header_row)
            workbook.close()
            with open(path, 'rb') as handle:
                content = handle.read()
        finally:
            if os.path.exists(path):
                os.unlink(path)
        filename = 'po_product_profit_%s_%s.xlsx' % (options['date_from'], options['date_to'])
        return filename, content

    # ------------------------------------------------------------------ #
    @api.model
    def _write_totals(self, w, summary, labels, row):
        for p in summary['rows']:
            vendor = _('Multiple vendors') if p['multi_vendor'] else p['first_vendor_name']
            w.text(row, 'item', labels.get(p['product_id'], p['product_name']), 'line')
            w.text(row, 'barcode', p['barcode'] or '', 'line')
            w.text(row, 'vendor', vendor, 'line')
            for key in ('qty', 'amount', 'po_count', 'received', 'pending',
                        'sold_qty', 'sales_amount'):
                w.value(row, key, p[key], 'line')
            w.ratio(row, 'avg_cost', 'amount', 'qty', p['avg_cost'], 'line')
            w.ratio(row, 'sale_rate', 'sales_amount', 'sold_qty', p['sale_rate'], 'line')
            w.product(row, 'cogs', 'sold_qty', 'avg_cost', p['cogs'], 'line')
            w.diff(row, 'gross_profit', 'sales_amount', 'cogs', p['gross_profit'], 'line')
            w.ratio(row, 'margin', 'gross_profit', 'sales_amount',
                    p['gross_profit'] / p['sales_amount'] if p['sales_amount'] else 0, 'line')
            row += 1
        return row

    @api.model
    def _write_detailed(self, w, engine, options, summary, labels, row):
        """Layout per product:
            product total (level 0, SUBTOTAL over the block below)
              PO rows of vendor A (level 2)
              vendor A subtotal (level 1)
              ...
        Every row count is known up front from the summary, so the product
        row can carry its SUBTOTAL range before the detail is streamed."""
        sales_keys = ('sold_qty', 'sales_amount')
        purchase_keys = ('qty', 'amount', 'po_count', 'received', 'pending')
        current_product = current_vendor = None
        vendor_start = None
        vendor_acc = None

        def close_vendor(at_row):
            w.sheet.set_row(at_row, None, None, {'level': 1})
            for key in purchase_keys:
                w.subtotal(at_row, key, vendor_start, at_row - 1, vendor_acc[key], 'vendor')
            w.text(at_row, 'vendor', self.env._('Subtotal · %s', vendor_acc['name']), 'vendor')
            w.ratio(at_row, 'avg_cost', 'amount', 'qty',
                    vendor_acc['amount'] / vendor_acc['qty'] if vendor_acc['qty'] else 0,
                    'vendor')
            w.fill(at_row, 'vendor')
            return at_row + 1

        for line in engine.iter_detail_rows(options, product_rows=summary['rows']):
            if line['product_id'] != current_product:
                if current_vendor is not None:
                    row = close_vendor(row)
                current_product, current_vendor = line['product_id'], None
                # product total row
                block = int(line['p_po_count']) + int(line['p_vendor_count'])
                label = labels.get(current_product, line['product_name'])
                w.text(row, 'item', label, 'product')
                w.text(row, 'barcode', line['barcode'] or '', 'product')
                for key in purchase_keys:
                    w.subtotal(row, key, row + 1, row + block, line['p_' + key], 'product')
                for key in sales_keys:
                    w.value(row, key, line['p_' + key], 'product')
                w.ratio(row, 'avg_cost', 'amount', 'qty', line['p_avg_cost'] or 0, 'product')
                w.ratio(row, 'sale_rate', 'sales_amount', 'sold_qty',
                        line['p_sale_rate'] or 0, 'product')
                w.product(row, 'cogs', 'sold_qty', 'avg_cost', line['p_cogs'], 'product')
                w.diff(row, 'gross_profit', 'sales_amount', 'cogs',
                       line['p_gross_profit'], 'product')
                margin = (line['p_gross_profit'] / line['p_sales_amount']
                          if line['p_sales_amount'] else 0)
                w.ratio(row, 'margin', 'gross_profit', 'sales_amount', margin, 'product')
                w.fill(row, 'product')
                row += 1
                product_label = label
            if line['vendor_id'] != current_vendor:
                if current_vendor is not None:
                    row = close_vendor(row)
                current_vendor = line['vendor_id']
                vendor_start = row
                vendor_acc = dict.fromkeys(purchase_keys, 0.0, )
                vendor_acc['name'] = line['vendor_name']
            for key in ('qty', 'amount', 'received', 'pending'):
                vendor_acc[key] += line[key] or 0.0
            vendor_acc['po_count'] += 1
            w.sheet.set_row(row, None, None, {'level': 2})
            w.text(row, 'vendor', line['vendor_name'], 'line')
            w.text(row, 'po', line['po_name'], 'line')
            w.text(row, 'doc', _('RFQ') if line['is_rfq'] else _('PO'), 'line')
            w.date(row, 'date', line['po_date'], 'line')
            w.text(row, 'item', product_label, 'line')
            w.text(row, 'barcode', line['barcode'] or '', 'line')
            for key in ('qty', 'amount', 'received', 'pending'):
                w.value(row, key, line[key], 'line')
            w.value(row, 'po_count', 1, 'line')
            w.ratio(row, 'avg_cost', 'amount', 'qty',
                    line['amount'] / line['qty'] if line['qty'] else 0, 'line')
            w.fill(row, 'line')
            row += 1
        if current_vendor is not None:
            row = close_vendor(row)
        return row

    @api.model
    def _write_grand_total(self, w, totals, first, end, detailed):
        row = end
        label_key = 'vendor' if detailed else 'item'
        w.text(row, label_key, _('Grand Total'), 'total')
        last = max(end - 1, first)
        for key in ('qty', 'amount', 'received', 'pending', 'sold_qty', 'sales_amount', 'cogs'):
            w.subtotal(row, key, first, last, totals[key], 'total')
        # distinct POs, not product x PO pairs
        w.value(row, 'po_count', totals['po_count'], 'total')
        w.ratio(row, 'avg_cost', 'amount', 'qty', totals['avg_cost'], 'total')
        w.ratio(row, 'sale_rate', 'sales_amount', 'sold_qty', totals['sale_rate'], 'total')
        w.diff(row, 'gross_profit', 'sales_amount', 'cogs', totals['gross_profit'], 'total')
        w.ratio(row, 'margin', 'gross_profit', 'sales_amount', totals['margin'] / 100.0, 'total')
        w.fill(row, 'total')


class _RowWriter:
    """Small helper that knows column positions, kinds and styles, and
    writes formulas with their cached result."""

    def __init__(self, sheet, fmt, columns, col):
        self.sheet, self.fmt, self.col = sheet, fmt, col
        self.kind = {c[0]: c[3] for c in columns}
        self.written = set()

    def _fmt(self, style, key, value=None):
        kind = self.kind[key]
        if kind == 'text':
            return self.fmt[(style, 'text')]
        if isinstance(value, (int, float)) and value < 0 and key in ('gross_profit', 'margin'):
            return self.fmt[(style, kind, 'neg')]
        return self.fmt[(style, kind)]

    def _ref(self, row, key):
        return xl_rowcol_to_cell(row, self.col[key])

    def _mark(self, row, key):
        self.written.add((row, self.col[key]))

    def fill(self, row, style):
        """Blank styled cells for unwritten columns so row bands are continuous."""
        for key, idx in self.col.items():
            if (row, idx) not in self.written:
                self.sheet.write_blank(row, idx, None, self._fmt(style, key))
        self.written = {cell for cell in self.written if cell[0] > row}

    def text(self, row, key, value, style):
        if key not in self.col:
            return
        self.sheet.write_string(row, self.col[key], value or '', self._fmt(style, key))
        self._mark(row, key)

    def date(self, row, key, value, style):
        if value:
            self.sheet.write_datetime(row, self.col[key],
                                      _to_datetime(value), self._fmt(style, key))
        else:
            self.sheet.write_blank(row, self.col[key], None, self._fmt(style, key))
        self._mark(row, key)

    def value(self, row, key, value, style):
        self.sheet.write_number(row, self.col[key], float(value or 0.0),
                                self._fmt(style, key, value))
        self._mark(row, key)

    def subtotal(self, row, key, first, last, cached, style):
        letter = xl_col_to_name(self.col[key])
        self.sheet.write_formula(row, self.col[key],
                                 '=SUBTOTAL(9,%s%d:%s%d)' % (letter, first + 1, letter, last + 1),
                                 self._fmt(style, key, cached), float(cached or 0.0))
        self._mark(row, key)

    def ratio(self, row, key, num_key, den_key, cached, style):
        self.sheet.write_formula(
            row, self.col[key],
            '=IFERROR(%s/%s,0)' % (self._ref(row, num_key), self._ref(row, den_key)),
            self._fmt(style, key, cached), float(cached or 0.0))
        self._mark(row, key)

    def product(self, row, key, a_key, b_key, cached, style):
        self.sheet.write_formula(
            row, self.col[key], '=%s*%s' % (self._ref(row, a_key), self._ref(row, b_key)),
            self._fmt(style, key, cached), float(cached or 0.0))
        self._mark(row, key)

    def diff(self, row, key, a_key, b_key, cached, style):
        self.sheet.write_formula(
            row, self.col[key], '=%s-%s' % (self._ref(row, a_key), self._ref(row, b_key)),
            self._fmt(style, key, cached), float(cached or 0.0))
        self._mark(row, key)


def _to_datetime(value):
    from datetime import date, datetime
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return datetime.strptime(str(value)[:10], '%Y-%m-%d')
