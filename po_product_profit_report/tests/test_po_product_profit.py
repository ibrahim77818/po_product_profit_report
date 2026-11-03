# -*- coding: utf-8 -*-
import io
import zipfile
from datetime import datetime

from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, tagged
from odoo.tests.common import TransactionCase, new_test_user


@tagged('post_install', '-at_install', 'po_product_profit_report')
class TestPoProductProfit(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.env.user.tz = 'UTC'
        cls.engine = cls.env['po.product.profit.engine']
        cls.company = cls.env.company
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1)
        cls.warehouse2 = cls.env['stock.warehouse'].create(
            {'name': 'PPR Second WH', 'code': 'PPR2', 'company_id': cls.company.id})
        cls.uom_unit = cls.env.ref('uom.product_uom_unit')
        cls.uom_dozen = cls.env.ref('uom.product_uom_dozen')

        Partner = cls.env['res.partner']
        cls.vendor_a = Partner.create({'name': 'PPR Vendor A', 'is_company': True})
        cls.vendor_a_contact = Partner.create({'name': 'PPR A Buyer Desk',
                                               'parent_id': cls.vendor_a.id})
        cls.vendor_b = Partner.create({'name': 'PPR Vendor B', 'is_company': True})
        cls.customer = Partner.create({'name': 'PPR Customer'})

        Product = cls.env['product.product']
        cls.p1 = Product.create({'name': 'PPR Widget', 'detailed_type': 'product',
                                 'default_code': 'PPR-W', 'barcode': '6290000000011',
                                 'standard_price': 999.0, 'list_price': 5.0})
        cls.p2 = Product.create({'name': 'PPR Gadget', 'detailed_type': 'product',
                                 'default_code': 'PPR-G', 'barcode': '6290000000028'})
        cls.p3 = Product.create({'name': 'PPR Bolt', 'detailed_type': 'product',
                                 'default_code': 'PPR-B'})
        cls.products = cls.p1 | cls.p2 | cls.p3

        # --- purchases (period: March 2025) ---------------------------------
        # P1: the multi-tier example 10 @ 1 + 10 @ 2 from vendor A ...
        cls.po1 = cls._po(cls.vendor_a, [(cls.p1, 10, 1.0)], '2025-03-03 09:00:00')
        cls.po2 = cls._po(cls.vendor_a_contact, [(cls.p1, 10, 2.0)], '2025-03-10 09:00:00')
        # ... plus 5 @ 4 from vendor B
        cls.po3 = cls._po(cls.vendor_b, [(cls.p1, 5, 4.0)], '2025-03-20 09:00:00')
        # P3 bought in dozens: 2 Dz @ 30 = 24 units for 60
        cls.po4 = cls._po(cls.vendor_b, [(cls.p3, 2, 30.0, cls.uom_dozen)], '2025-03-21 09:00:00')
        for po in cls.po1 | cls.po2 | cls.po3 | cls.po4:
            po.button_confirm()
        # RFQ only (never confirmed): P2 3 @ 10
        cls.rfq = cls._po(cls.vendor_b, [(cls.p2, 3, 10.0)], '2025-03-12 09:00:00')
        # Outside the period / cancelled: must never show up
        cls.po_old = cls._po(cls.vendor_a, [(cls.p1, 100, 50.0)], '2025-02-27 09:00:00')
        cls.po_old.button_confirm()
        cls.po_cancel = cls._po(cls.vendor_a, [(cls.p1, 7, 3.0)], '2025-03-05 09:00:00')
        cls.po_cancel.button_cancel()

        # receipts: PO1 fully, PO2 partially (4 of 10), PO4 one dozen (12 units)
        cls._receive(cls.po1, 10)
        cls._receive(cls.po2, 4)
        cls._receive(cls.po4, 12)  # moves are in the product UoM (Units)

        # --- sales ------------------------------------------------------------
        cls.so = cls.env['sale.order'].create({
            'partner_id': cls.customer.id,
            'order_line': [
                (0, 0, {'product_id': cls.p1.id, 'product_uom_qty': 12, 'price_unit': 5.0}),
                (0, 0, {'product_id': cls.p3.id, 'product_uom_qty': 1,
                        'product_uom': cls.uom_dozen.id, 'price_unit': 48.0}),
            ],
        })
        cls.so.action_confirm()
        cls.so.date_order = datetime(2025, 3, 25, 9, 0)
        cls.so_draft = cls.env['sale.order'].create({
            'partner_id': cls.customer.id,
            'order_line': [(0, 0, {'product_id': cls.p1.id, 'product_uom_qty': 500,
                                   'price_unit': 5.0})],
        })
        cls.so_draft.date_order = datetime(2025, 3, 25, 9, 0)

    # ------------------------------------------------------------------ #
    @classmethod
    def _po(cls, partner, lines, date_order, warehouse=None, currency=None):
        vals = {
            'partner_id': partner.id,
            'date_order': date_order,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_qty': qty,
                'price_unit': price,
                'product_uom': (uom or product.uom_po_id).id,
                'date_planned': date_order,
            }) for product, qty, price, *uom_ in lines for uom in [uom_[0] if uom_ else None]],
        }
        if warehouse:
            vals['picking_type_id'] = warehouse.in_type_id.id
        if currency:
            vals['currency_id'] = currency.id
        return cls.env['purchase.order'].create(vals)

    @classmethod
    def _receive(cls, po, qty):
        picking = po.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))
        picking.ensure_one()
        for move in picking.move_ids:
            move.quantity_done = qty
        result = picking.button_validate()
        if isinstance(result, dict) and result.get('res_model') == 'stock.backorder.confirmation':
            Form(cls.env['stock.backorder.confirmation'].with_context(result['context'])) \
                .save().process()

    def _options(self, **kw):
        opts = {'date_from': '2025-03-01', 'date_to': '2025-03-31', 'doc_type': 'po',
                'product_ids': self.products.ids, 'mode': 'detailed'}
        opts.update(kw)
        return self.engine.sanitize_options(opts)

    def _row(self, data, product):
        rows = [r for r in data['rows'] if r['product_id'] == product.id]
        return rows[0] if rows else None

    # ------------------------------------------------------------------ #
    def test_01_actual_po_rates_multi_tier(self):
        """10 @ 1 + 10 @ 2 = 30 -- never the product cost (999)."""
        data = self.engine.fetch_summary(self._options(partner_ids=[self.vendor_a.id]))
        row = self._row(data, self.p1)
        self.assertAlmostEqual(row['qty'], 20)
        self.assertAlmostEqual(row['amount'], 30.0)
        self.assertAlmostEqual(row['avg_cost'], 1.5)
        self.assertEqual(row['po_count'], 2, 'contact PO grouped under its company vendor')
        self.assertFalse(row['multi_vendor'])

    def test_02_full_metrics_all_vendors(self):
        data = self.engine.fetch_summary(self._options())
        row = self._row(data, self.p1)
        self.assertAlmostEqual(row['qty'], 25)
        self.assertAlmostEqual(row['amount'], 50.0)        # 10 + 20 + 20
        self.assertEqual(row['po_count'], 3)                # old & cancelled excluded
        self.assertTrue(row['multi_vendor'])
        self.assertAlmostEqual(row['received'], 14)        # 10 + 4
        self.assertAlmostEqual(row['pending'], 11)         # 0 + 6 + 5
        self.assertAlmostEqual(row['sold_qty'], 12)        # draft SO ignored
        self.assertAlmostEqual(row['sales_amount'], 60.0)
        self.assertAlmostEqual(row['sale_rate'], 5.0)
        self.assertAlmostEqual(row['cogs'], 24.0)          # 12 x avg 2.0
        self.assertAlmostEqual(row['gross_profit'], 36.0)
        self.assertIsNone(self._row(data, self.p2), 'RFQ excluded from "Purchase Orders"')

    def test_03_uom_conversion(self):
        row = self._row(self.engine.fetch_summary(self._options()), self.p3)
        self.assertAlmostEqual(row['qty'], 24)             # 2 Dz
        self.assertAlmostEqual(row['amount'], 60.0)
        self.assertAlmostEqual(row['avg_cost'], 2.5)       # per unit
        self.assertAlmostEqual(row['received'], 12)        # 1 Dz received
        self.assertAlmostEqual(row['pending'], 12)
        self.assertAlmostEqual(row['sold_qty'], 12)        # 1 Dz sold
        self.assertAlmostEqual(row['sales_amount'], 48.0)
        self.assertAlmostEqual(row['gross_profit'], 48.0 - 12 * 2.5)

    def test_04_document_types(self):
        rfq = self.engine.fetch_summary(self._options(doc_type='rfq'))
        self.assertEqual([r['product_id'] for r in rfq['rows']], [self.p2.id])
        self.assertAlmostEqual(rfq['rows'][0]['amount'], 30.0)
        both = self.engine.fetch_summary(self._options(doc_type='both'))
        self.assertEqual({r['product_id'] for r in both['rows']}, set(self.products.ids))
        details = self.engine.fetch_details(self._options(doc_type='both'), [self.p2.id])
        self.assertTrue(details[self.p2.id][0]['lines'][0]['is_rfq'])

    def test_05_period_and_timezone_boundaries(self):
        po = self._po(self.vendor_a, [(self.p2, 1, 7.0)], '2025-03-31 22:30:00')
        po.button_confirm()
        opts = self._options(date_from='2025-03-01', date_to='2025-03-31')
        self.assertTrue(self._row(self.engine.fetch_summary(opts), self.p2),
                        'UTC user: 31 Mar 22:30 is in March')
        self.env.user.tz = 'Asia/Riyadh'                   # UTC+3 -> 1 Apr 01:30
        self.assertIsNone(self._row(self.engine.fetch_summary(opts), self.p2))
        april = self._options(date_from='2025-04-01', date_to='2025-04-30')
        self.assertTrue(self._row(self.engine.fetch_summary(april), self.p2))

    def test_06_warehouse_filter(self):
        po = self._po(self.vendor_b, [(self.p1, 1000, 1.0)], '2025-03-15 09:00:00',
                      warehouse=self.warehouse2)
        po.button_confirm()
        main = self._row(self.engine.fetch_summary(
            self._options(warehouse_ids=[self.warehouse.id])), self.p1)
        self.assertAlmostEqual(main['qty'], 25)
        other = self._row(self.engine.fetch_summary(
            self._options(warehouse_ids=[self.warehouse2.id])), self.p1)
        self.assertAlmostEqual(other['qty'], 1000)
        self.assertAlmostEqual(other['sold_qty'], 0, msg='SO belongs to the main warehouse')

    def test_07_foreign_currency_converted(self):
        foreign = self.env.ref('base.EUR') if self.company.currency_id != self.env.ref('base.EUR') \
            else self.env.ref('base.USD')
        foreign.active = True
        Rate = self.env['res.currency.rate']
        Rate.create({'name': '2025-03-01', 'rate': 1.0,
                     'currency_id': self.company.currency_id.id, 'company_id': self.company.id})
        Rate.create({'name': '2025-03-01', 'rate': 4.0,
                     'currency_id': foreign.id, 'company_id': self.company.id})
        po = self._po(self.vendor_b, [(self.p2, 10, 8.0)], '2025-03-16 09:00:00',
                      currency=foreign)
        po.button_confirm()
        self.assertAlmostEqual(po.currency_rate, 4.0)
        row = self._row(self.engine.fetch_summary(self._options()), self.p2)
        self.assertAlmostEqual(row['amount'], 20.0)       # 80 foreign / 4

    def test_08_totals_and_grand_totals(self):
        data = self.engine.fetch_summary(self._options(doc_type='both'))
        t = data['totals']
        self.assertEqual(t['count'], 3)
        self.assertEqual(t['po_count'], 5)                 # 4 POs + 1 RFQ, distinct
        self.assertEqual(t['vendor_count'], 2)
        self.assertAlmostEqual(t['amount'], 50 + 60 + 30)
        self.assertAlmostEqual(t['gross_profit'], 36 + 18)
        self.assertEqual(t['detail_count'], 5)
        # paging returns the same grand totals
        page2 = self.engine.fetch_summary(self._options(doc_type='both'), offset=2, limit=2)
        self.assertEqual(len(page2['rows']), 1)
        self.assertAlmostEqual(page2['totals']['amount'], t['amount'])

    def test_09_details_and_stream(self):
        opts = self._options()
        vendors = self.engine.fetch_details(opts, [self.p1.id])[self.p1.id]
        self.assertEqual([v['vendor_name'] for v in vendors], ['PPR Vendor A', 'PPR Vendor B'])
        self.assertEqual([l['po_name'] for l in vendors[0]['lines']],
                         [self.po1.name, self.po2.name])
        self.assertEqual([l['avg_cost'] for l in vendors[0]['lines']], [1.0, 2.0])
        self.assertAlmostEqual(vendors[0]['amount'], 30.0)
        streamed = list(self.engine.iter_detail_rows(opts))
        self.assertEqual(len(streamed), 4)
        self.assertEqual(streamed[0]['product_id'], self.p3.id, 'ordered by amount desc')

    def test_10_sorting_search_and_sale_basis(self):
        by_name = self.engine.fetch_summary(self._options(sort_by='name', sort_desc=False))
        self.assertEqual([r['product_id'] for r in by_name['rows']], [self.p3.id, self.p1.id])
        found = self.engine.fetch_summary(self._options(search='6290000000011'))
        self.assertEqual([r['product_id'] for r in found['rows']], [self.p1.id])
        self.assertEqual((found['totals']['count'], found['totals']['po_count']), (1, 3),
                         'grand totals follow the search')
        self.assertFalse(self.engine.fetch_summary(self._options(search='no-such-thing'))['rows'])
        delivered = self._row(self.engine.fetch_summary(
            self._options(sale_basis='delivered')), self.p1)
        self.assertAlmostEqual(delivered['sold_qty'], 0)  # nothing delivered yet

    def test_11_security_and_sanitizing(self):
        user = new_test_user(self.env, login='ppr_buyer', groups='purchase.group_purchase_user')
        with self.assertRaises(AccessError):
            self.env['po.product.profit.wizard'].with_user(user).get_web_data(self._options())
        other = self.env['res.company'].create({'name': 'PPR Other Co'})
        # as from the browser: only the current company is allowed
        engine = self.engine.with_context(allowed_company_ids=[self.company.id])
        opts = engine.sanitize_options({'company_ids': [other.id], 'doc_type': 'x; DROP',
                                             'sort_by': 'evil', 'partner_ids': ['1 OR 1=1']})
        self.assertNotIn(other.id, opts['company_ids'])
        self.assertEqual((opts['doc_type'], opts['sort_by'], opts['partner_ids']),
                         ('po', 'amount', []))
        with self.assertRaises(UserError):
            self.engine.sanitize_options({'date_from': '2025-04-01', 'date_to': '2025-03-01'})

    def test_12_wizard_actions(self):
        wizard = self.env['po.product.profit.wizard'].create({
            'period': 'custom', 'date_from': '2025-03-01', 'date_to': '2025-03-31',
            'product_ids': [(6, 0, self.products.ids)]})
        web = wizard.action_view_web()
        self.assertEqual(web['tag'], 'po_product_profit_report.web_view')
        data = self.env['po.product.profit.wizard'].get_web_data(web['params']['options'])
        self.assertTrue(all(r['product_label'] for r in data['rows']))
        self.assertEqual(wizard.action_export_xlsx()['tag'], 'po_product_profit_report.download_xlsx')
        pdf = wizard.action_print_pdf()
        self.assertEqual(pdf['report_name'], 'po_product_profit_report.report_po_product_profit')
        reopened = self.env['po.product.profit.wizard'].get_wizard_action(web['params']['options'])
        self.assertEqual(reopened['context']['default_product_ids'], [(6, 0, sorted(self.products.ids))])
        with Form(self.env['po.product.profit.wizard']) as f:
            f.period = 'last_year'
            self.assertEqual(f.date_from.month, 1)
            self.assertEqual(f.date_to.month, 12)

    def test_13_pdf_html_detailed_totals_rtl(self):
        report = 'po_product_profit_report.report_po_product_profit'
        Report = self.env['ir.actions.report']
        html, _fmt = Report._render_qweb_html(report, [], data={'options': self._options()})
        html = html.decode()
        for needle in ('PPR Widget', 'PPR Vendor A', self.po2.name, 'Grand Total', '<thead>'):
            self.assertIn(needle, html)
        html, _fmt = Report._render_qweb_html(
            report, [], data={'options': self._options(mode='totals')})
        self.assertIn('PPR Widget', html.decode())
        self.assertNotIn(self.po2.name, html.decode())
        self.env['res.lang']._activate_lang('ar_001')
        html, _fmt = Report.with_context(lang='ar_001')._render_qweb_html(
            report, [], data={'options': self._options()})
        self.assertIn('dir="rtl"', html.decode())

    def test_14_xlsx(self):
        for mode in ('detailed', 'totals'):
            name, content = self.env['po.product.profit.xlsx'].generate(self._options(mode=mode))
            self.assertTrue(name.endswith('.xlsx'))
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                xml = ''.join(zf.read(n).decode() for n in zf.namelist() if n.endswith('.xml'))
            self.assertIn('SUBTOTAL(9,', xml)
            self.assertIn('PPR Widget', xml)
            if mode == 'detailed':
                self.assertIn(self.po2.name, xml)
                self.assertIn('outlineLevel="2"', xml)

    def test_15_indexes_exist(self):
        from ..models.report_engine import REPORT_INDEXES
        self.env.cr.execute('SELECT indexname FROM pg_indexes WHERE indexname = ANY(%s)',
                            [[i[0] for i in REPORT_INDEXES]])
        self.assertEqual(len(self.env.cr.fetchall()), len(REPORT_INDEXES))

    def test_16_same_product_twice_in_one_po(self):
        """Two lines of one product in one PO: 1 PO, not 2; pending per line."""
        po = self._po(self.vendor_a, [(self.p2, 3, 1.0), (self.p2, 2, 1.5)],
                      '2025-03-18 09:00:00')
        po.button_confirm()
        row = self._row(self.engine.fetch_summary(self._options()), self.p2)
        self.assertEqual(row['po_count'], 1)
        self.assertAlmostEqual(row['qty'], 5)
        self.assertAlmostEqual(row['amount'], 6.0)
        self.assertAlmostEqual(row['pending'], 5)
        details = self.engine.fetch_details(self._options(), [self.p2.id])[self.p2.id]
        self.assertEqual(len(details[0]['lines']), 1, 'one detail row per (product, PO)')
        totals = self.engine.fetch_summary(self._options())['totals']
        self.assertEqual(totals['detail_count'],
                         sum(1 for _ in self.engine.iter_detail_rows(self._options())))
