# -*- coding: utf-8 -*-
from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# A detailed PDF beyond this many PO rows is a phone book, not a report;
# Excel (streamed) and the web view (lazy) have no such limit.
PDF_MAX_ROWS = 20000

SORT_OPTIONS = {
    'amount': ('amount', True),
    'profit': ('profit', True),
    'margin': ('margin', True),
    'qty': ('qty', True),
    'sold': ('sold', True),
    'pending': ('pending', True),
    'name': ('name', False),
}


class PoProductProfitWizard(models.TransientModel):
    _name = 'po.product.profit.wizard'
    _description = 'PO Product Profit Report Wizard'

    period = fields.Selection([
        ('this_month', 'This Month'),
        ('last_month', 'Last Month'),
        ('this_quarter', 'This Quarter'),
        ('last_quarter', 'Last Quarter'),
        ('this_year', 'This Year'),
        ('last_year', 'Last Year'),
        ('custom', 'Custom Range'),
    ], string='Period', default='this_month', required=True)
    date_from = fields.Date(string='From', required=True,
                            default=lambda self: fields.Date.context_today(self).replace(day=1))
    date_to = fields.Date(string='To', required=True,
                          default=lambda self: fields.Date.context_today(self))
    doc_type = fields.Selection([
        ('rfq', 'RFQs'),
        ('po', 'Purchase Orders'),
        ('both', 'Both'),
    ], string='Document Type', default='po', required=True,
        help='RFQs: draft, sent and waiting-approval requests.\n'
             'Purchase Orders: confirmed and locked orders.\n'
             'Cancelled orders are always excluded.')
    partner_ids = fields.Many2many(
        'res.partner', 'po_ppr_wizard_partner_rel', 'wizard_id', 'partner_id',
        string='Vendors', help='Leave empty for all vendors. Contacts of a selected '
                               'company are included.')
    product_ids = fields.Many2many(
        'product.product', 'po_ppr_wizard_product_rel', 'wizard_id', 'product_id',
        string='Products', domain=[('purchase_ok', '=', True)],
        help='Leave empty for all purchased products.')
    warehouse_ids = fields.Many2many(
        'stock.warehouse', 'po_ppr_wizard_warehouse_rel', 'wizard_id', 'warehouse_id',
        string='Warehouses',
        help='Purchases by the warehouse of their receipt operation type; sales by the '
             'warehouse of the sale order. Leave empty for all.')
    company_ids = fields.Many2many(
        'res.company', 'po_ppr_wizard_company_rel', 'wizard_id', 'company_id',
        string='Companies', required=True,
        default=lambda self: self.env.companies,
        domain=lambda self: [('id', 'in', self.env.companies.ids)])
    mode = fields.Selection([
        ('detailed', 'Detailed'),
        ('totals', 'Totals Only'),
    ], string='Report Mode', default='detailed', required=True,
        help='Detailed: every product broken down by vendor and purchase order.\n'
             'Totals Only: one consolidated line per product.')
    sale_basis = fields.Selection([
        ('ordered', 'Ordered Quantity'),
        ('delivered', 'Delivered Quantity'),
        ('invoiced', 'Invoiced Quantity'),
    ], string='Sold Quantity Basis', default='ordered', required=True,
        help='Which quantity of the confirmed sale orders dated in the period counts as sold.')
    sort_by = fields.Selection([
        ('amount', 'Purchased Amount (high to low)'),
        ('profit', 'Gross Profit (high to low)'),
        ('margin', 'Margin % (high to low)'),
        ('qty', 'Purchased Quantity (high to low)'),
        ('sold', 'Sold Quantity (high to low)'),
        ('pending', 'Pending Quantity (high to low)'),
        ('name', 'Product Name (A to Z)'),
    ], string='Sort Products By', default='amount', required=True)
    currency_id = fields.Many2one('res.currency', compute='_compute_currency_id',
                                  string='Report Currency')

    @api.depends('company_ids')
    def _compute_currency_id(self):
        for wizard in self:
            companies = wizard.company_ids._origin or self.env.companies
            company = self.env.company if self.env.company in companies else companies[:1]
            wizard.currency_id = company.currency_id

    @api.onchange('period')
    def _onchange_period(self):
        if self.period == 'custom':
            return
        self.date_from, self.date_to = self._period_dates(self.period,
                                                          fields.Date.context_today(self))

    @api.onchange('date_from', 'date_to')
    def _onchange_dates(self):
        if self.period != 'custom' and self.date_from and self.date_to and \
                (self.date_from, self.date_to) != self._period_dates(
                    self.period, fields.Date.context_today(self)):
            self.period = 'custom'

    @api.model
    def _period_dates(self, period, today):
        if period == 'this_month':
            return today.replace(day=1), today
        if period == 'last_month':
            start = today.replace(day=1) - relativedelta(months=1)
            return start, today.replace(day=1) - relativedelta(days=1)
        quarter_start = today.replace(month=((today.month - 1) // 3) * 3 + 1, day=1)
        if period == 'this_quarter':
            return quarter_start, today
        if period == 'last_quarter':
            return quarter_start - relativedelta(months=3), quarter_start - relativedelta(days=1)
        if period == 'this_year':
            return today.replace(month=1, day=1), today
        if period == 'last_year':
            return (today.replace(year=today.year - 1, month=1, day=1),
                    today.replace(year=today.year - 1, month=12, day=31))
        return today.replace(day=1), today

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for wizard in self:
            if wizard.date_from > wizard.date_to:
                raise UserError(_('The start date must be on or before the end date.'))

    # ------------------------------------------------------------------ #
    # Options
    # ------------------------------------------------------------------ #
    def _get_options(self):
        self.ensure_one()
        sort_key, sort_desc = SORT_OPTIONS[self.sort_by]
        return self.env['po.product.profit.engine'].sanitize_options({
            'date_from': self.date_from,
            'date_to': self.date_to,
            'doc_type': self.doc_type,
            'mode': self.mode,
            'sale_basis': self.sale_basis,
            'sort_by': sort_key,
            'sort_desc': sort_desc,
            'partner_ids': self.partner_ids.ids,
            'product_ids': self.product_ids.ids,
            'warehouse_ids': self.warehouse_ids.ids,
            'company_ids': self.company_ids.ids,
        })

    # ------------------------------------------------------------------ #
    # Buttons
    # ------------------------------------------------------------------ #
    def action_view_web(self):
        options = self._get_options()
        return self._web_action(options)

    def action_print_pdf(self):
        return self._pdf_action(self._get_options())

    def action_export_xlsx(self):
        options = self._get_options()
        self.env['po.product.profit.engine']._check_access()
        return {
            'type': 'ir.actions.client',
            'tag': 'po_product_profit_report.download_xlsx',
            'params': {'options': options},
        }

    @api.model
    def _web_action(self, options):
        return {
            'type': 'ir.actions.client',
            'tag': 'po_product_profit_report.web_view',
            'name': _('PO Product Profit Report'),
            'params': {'options': options},
            'target': 'current',
        }

    @api.model
    def _pdf_action(self, options):
        engine = self.env['po.product.profit.engine']
        probe = engine.fetch_summary(options, 0, 1)['totals']
        if not probe['count']:
            raise UserError(_('No purchases match these filters.'))
        rows = probe['detail_count'] if options['mode'] == 'detailed' else probe['count']
        if rows > PDF_MAX_ROWS:
            raise UserError(_(
                'This selection produces %(rows)s rows, too many for a readable PDF '
                '(limit %(limit)s). Narrow the filters, switch to "Totals Only", or '
                'use the Excel export, which has no such limit.',
                rows=rows, limit=PDF_MAX_ROWS))
        report = self.env.ref('po_product_profit_report.action_report_po_product_profit')
        # config=False: this report has its own header/footer, so never divert
        # the user to the company "document layout" configurator.
        return report.report_action(self.env['po.product.profit.wizard'],
                                    data={'options': options}, config=False)

    # ------------------------------------------------------------------ #
    # RPC endpoints for the web view (stateless: options travel with the
    # page so a reload or a vacuumed wizard never breaks it)
    # ------------------------------------------------------------------ #
    @api.model
    def get_web_data(self, options, offset=0, limit=200):
        engine = self.env['po.product.profit.engine']
        options = engine.sanitize_options(options)
        data = engine.fetch_summary(options, offset, limit)
        labels = engine.product_labels([r['product_id'] for r in data['rows']])
        for row in data['rows']:
            row['product_label'] = labels.get(row['product_id'], row['product_name'])
        company, currency = engine._report_currency(options)
        data.update({
            'options': options,
            'qty_digits': self.env['decimal.precision'].precision_get('Product Unit of Measure'),
            'amount_digits': currency.decimal_places,
            'filters': engine.filter_labels(options),
            'currency_id': currency.id,
            'company_name': company.name,
        })
        return data

    @api.model
    def get_web_details(self, options, product_ids):
        engine = self.env['po.product.profit.engine']
        options = engine.sanitize_options(options)
        return {str(k): v for k, v in engine.fetch_details(options, product_ids).items()}

    @api.model
    def get_pdf_action(self, options):
        return self._pdf_action(self.env['po.product.profit.engine'].sanitize_options(options))

    @api.model
    def get_wizard_action(self, options):
        """Re-open the wizard pre-filled with the options of the web view."""
        options = self.env['po.product.profit.engine'].sanitize_options(options)
        sort_by = next((k for k, v in SORT_OPTIONS.items()
                        if v == (options['sort_by'], options['sort_desc'])), 'amount')
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'name': _('PO Product Profit Report'),
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {
                'default_period': 'custom',
                'default_date_from': options['date_from'],
                'default_date_to': options['date_to'],
                'default_doc_type': options['doc_type'],
                'default_mode': options['mode'],
                'default_sale_basis': options['sale_basis'],
                'default_sort_by': sort_by,
                'default_partner_ids': [(6, 0, options['partner_ids'])],
                'default_product_ids': [(6, 0, options['product_ids'])],
                'default_warehouse_ids': [(6, 0, options['warehouse_ids'])],
                'default_company_ids': [(6, 0, options['company_ids'])],
            },
        }
