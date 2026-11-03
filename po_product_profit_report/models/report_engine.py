# -*- coding: utf-8 -*-
"""Set-based SQL engine for the PO Product Profit Report.

Design notes (why it stays fast on 1M+ purchase lines)
------------------------------------------------------
* One statement per output, aggregated in PostgreSQL: filters are pushed
  down to the order headers, lines are joined by ``order_id``, and the
  product-level aggregate runs in parallel (see ``_purchase_sql``). No ORM
  ``read`` over lines, no Python loops over lines.
* Currency conversion uses the ``currency_rate`` that Odoo already stores on
  ``purchase.order`` / ``sale.order`` (ratio order currency / company
  currency), so there are no per-row rate lookups. A tiny ``cf`` CTE then
  converts each company currency into the report currency (multi-company).
* Covering indexes (``INCLUDE``) let PostgreSQL answer the line aggregates
  with index-only scans. They are created ``IF NOT EXISTS`` in ``init()``
  and dropped by the uninstall hook.
* The web view pages over products and loads PO detail lazily per product;
  Excel streams detail rows through a server-side cursor, so memory use is
  bounded by the batch size, not the result size.

Business rules
--------------
* Purchased qty / amount: PO line ``product_uom_qty`` (product UoM) and
  ``price_subtotal`` (untaxed, the actual line rate x qty). 10 @ 1 plus
  10 @ 2 gives 20 units for 30, average rate 1.5. Product standard cost is
  never used.
* Received qty is converted from the line UoM to the product UoM; pending is
  ``max(purchased - received, 0)`` per PO line, so an over-receipt on one
  line never hides a shortfall on another.
* Sales: confirmed sale orders (state sale/done) dated in the period, for the
  products in scope, same companies/warehouses. Quantity basis is ordered,
  delivered or invoiced (wizard option).
* Gross profit = sales amount - sold qty x weighted average actual PO rate of
  the purchases in scope.
"""
from datetime import datetime, time, timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

# (index name, table, definition) -- PostgreSQL >= 11 (INCLUDE).
REPORT_INDEXES = [
    ('po_ppr_purchase_order_date_idx', 'purchase_order',
     '(date_order) INCLUDE (state, company_id, partner_id, picking_type_id, currency_rate)'),
    ('po_ppr_purchase_line_order_cover_idx', 'purchase_order_line',
     '(order_id, product_id) INCLUDE (id, product_qty, product_uom_qty, qty_received, price_subtotal) '
     'WHERE display_type IS NULL AND product_id IS NOT NULL'),
    ('po_ppr_sale_order_date_idx', 'sale_order',
     '(date_order) INCLUDE (state, company_id, warehouse_id, currency_rate)'),
    ('po_ppr_sale_line_order_cover_idx', 'sale_order_line',
     '(order_id, product_id) INCLUDE (product_uom_qty, qty_delivered, qty_invoiced, product_uom_id, price_subtotal) '
     'WHERE display_type IS NULL AND product_id IS NOT NULL'),
]

DOC_STATES = {
    'rfq': ['draft', 'sent', 'to approve'],
    'po': ['purchase', 'done'],
}
DOC_STATES['both'] = DOC_STATES['rfq'] + DOC_STATES['po']
RFQ_STATES = DOC_STATES['rfq']

SALE_BASIS = {
    # quantity expression, amount expression (sale order currency)
    'ordered': ('sl.product_uom_qty', 'sl.price_subtotal'),
    'delivered': ('sl.qty_delivered',
                  'sl.price_subtotal * sl.qty_delivered / NULLIF(sl.product_uom_qty, 0)'),
    'invoiced': ('sl.qty_invoiced',
                 'sl.price_subtotal * sl.qty_invoiced / NULLIF(sl.product_uom_qty, 0)'),
}

# Whitelisted sort keys -> product row field.
SORT_KEYS = {
    'amount': 'amount',
    'qty': 'qty',
    'pending': 'pending',
    'sold': 'sold_qty',
    'sales': 'sales_amount',
    'profit': 'gross_profit',
    'margin': 'margin',
    'name': 'product_name',
    'code': 'default_code',
    'barcode': 'barcode',
    'po_count': 'po_count',
}

MAX_LIMIT = 1000  # products per web page call
STREAM_DUPLICATES_ABOVE = 250000  # lines


class PoProductProfitEngine(models.AbstractModel):
    _name = 'po.product.profit.engine'
    _description = 'PO Product Profit Report Engine'

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #
    def init(self):
        for name, table, definition in REPORT_INDEXES:
            self.env.cr.execute(
                'CREATE INDEX IF NOT EXISTS "%s" ON "%s" %s' % (name, table, definition))

    # ------------------------------------------------------------------ #
    # Options
    # ------------------------------------------------------------------ #
    @api.model
    def _ppr_check_access(self):
        if not (self.env.su or self.env.user.has_group('purchase.group_purchase_manager')):
            raise AccessError(_('Only Purchase Administrators can run the PO Product Profit Report.'))

    @api.model
    def sanitize_options(self, options):
        """Validate and normalise options coming from the wizard or from the
        browser. Everything that reaches SQL passes through here."""
        options = dict(options or {})
        today = fields.Date.context_today(self)
        try:
            date_from = fields.Date.to_date(options.get('date_from')) or today.replace(day=1)
            date_to = fields.Date.to_date(options.get('date_to')) or today
        except (TypeError, ValueError):
            raise UserError(_('Invalid date range.'))
        if date_from > date_to:
            raise UserError(_('The start date must be on or before the end date.'))

        def _ids(key):
            return sorted({int(i) for i in (options.get(key) or []) if str(i).isdigit()})

        allowed = self.env.companies.ids or self.env.company.ids
        company_ids = [c for c in _ids('company_ids') if c in allowed] or allowed

        def _pick(key, allowed_values, default):
            value = options.get(key)
            return value if value in allowed_values else default

        search = (options.get('search') or '').strip()[:100]
        return {
            'date_from': fields.Date.to_string(date_from),
            'date_to': fields.Date.to_string(date_to),
            'doc_type': _pick('doc_type', DOC_STATES, 'po'),
            'mode': _pick('mode', ('detailed', 'totals'), 'detailed'),
            'sort_by': _pick('sort_by', SORT_KEYS, 'amount'),
            'sort_desc': bool(options.get('sort_desc', True)),
            'sale_basis': _pick('sale_basis', SALE_BASIS, 'ordered'),
            'partner_ids': _ids('partner_ids'),
            'product_ids': _ids('product_ids'),
            'warehouse_ids': _ids('warehouse_ids'),
            'company_ids': company_ids,
            'search': search,
        }

    @api.model
    def _report_currency(self, options):
        companies = self.env['res.company'].browse(options['company_ids'])
        company = self.env.company if self.env.company in companies else companies[:1]
        return company, company.currency_id

    @api.model
    def _get_params(self, options):
        tz_name = self.env.user.tz or self.env.context.get('tz') or 'UTC'
        try:
            tz = pytz.timezone(tz_name)
        except pytz.UnknownTimeZoneError:
            tz_name, tz = 'UTC', pytz.utc

        def _utc(day):
            local = tz.localize(datetime.combine(day, time.min))
            return local.astimezone(pytz.utc).replace(tzinfo=None)

        date_from = fields.Date.to_date(options['date_from'])
        date_to = fields.Date.to_date(options['date_to'])
        report_company, report_currency = self._report_currency(options)
        cf_ids, cf_factors = [], []
        for company in self.env['res.company'].browse(options['company_ids']):
            factor = 1.0
            if company.currency_id != report_currency:
                factor = self.env['res.currency']._get_conversion_rate(
                    company.currency_id, report_currency, report_company, date_to)
            cf_ids.append(company.id)
            cf_factors.append(factor)

        partner_ids = []
        if options['partner_ids']:
            partner_ids = self.env['res.partner'].with_context(active_test=False).search(
                [('id', 'child_of', options['partner_ids'])]).ids

        return {
            'dt_from': _utc(date_from),
            'dt_to': _utc(date_to + timedelta(days=1)),
            'tz': tz_name,
            'states': DOC_STATES[options['doc_type']],
            'rfq_states': RFQ_STATES,
            'cf_ids': cf_ids,
            'cf_factors': cf_factors,
            'partner_ids': partner_ids,
            'product_ids': options['product_ids'],
            'warehouse_ids': options['warehouse_ids'],
            'lang': self.env.lang or 'en_US',
            'search': '%%%s%%' % options['search'].replace('\\', '\\\\')
                      .replace('%', '\\%').replace('_', '\\_') if options['search'] else None,
        }

    # ------------------------------------------------------------------ #
    # SQL
    # ------------------------------------------------------------------ #
    # Two shapes, chosen by what each output needs:
    #
    # * Fast path (``_purchase_sql`` + ``_sales_sql`` + ``_grand_sql``): three
    #   small standalone statements, each a product-level aggregate read
    #   straight from the base tables, so PostgreSQL plans each one as a
    #   parallel partial aggregate. Python then merges and sorts the product
    #   rows (O(products), never O(lines)). The exact number of distinct POs per product is
    #   ``lines - lines repeating a (PO, product) pair``, answered by an index
    #   probe on the covering index instead of a COUNT(DISTINCT) sort.
    # * Detail path (``_lines_sql``): one row per (product, PO), only used for
    #   the lazy drill-down (always product-filtered) and detailed exports.
    # ------------------------------------------------------------------ #
    @api.model
    def _fragments(self, options):
        frag = {'wh_join': '', 'po_wh': '', 'so_wh': '', 'partner': '', 'product': '',
                'sale_product': '', 'search': ''}
        if options['warehouse_ids']:
            frag['wh_join'] = 'JOIN stock_picking_type spt ON spt.id = po.picking_type_id'
            frag['po_wh'] = 'AND spt.warehouse_id = ANY(%(warehouse_ids)s)'
            frag['so_wh'] = 'AND so.warehouse_id = ANY(%(warehouse_ids)s)'
        if options['partner_ids']:
            frag['partner'] = 'AND po.partner_id = ANY(%(partner_ids)s)'
        if options['product_ids']:
            frag['product'] = 'AND l.product_id = ANY(%(product_ids)s)'
            frag['sale_product'] = 'AND sl.product_id = ANY(%(product_ids)s)'
        frag['po_from'] = """purchase_order po
      JOIN unnest(%(cf_ids)s::int[], %(cf_factors)s::numeric[]) AS cf(company_id, factor)
        ON cf.company_id = po.company_id
      JOIN res_partner rp ON rp.id = po.partner_id
      {wh_join}""".format(**frag)
        frag['po_where'] = """po.date_order >= %(dt_from)s AND po.date_order < %(dt_to)s
       AND po.state = ANY(%(states)s)
       {partner} {po_wh}""".format(**frag)
        # float8 on purpose: numeric (arbitrary precision) arithmetic over a
        # million lines costs ~2x; float8 keeps ~15 significant digits, far
        # beyond what a report total shows. Stored columns stay numeric.
        frag['conv'] = 'cf.factor::float8 / COALESCE(NULLIF(po.currency_rate, 0), 1)::float8'
        frag['rcv'] = """(CASE WHEN l.product_qty <> 0
                    THEN l.qty_received::float8 * l.product_uom_qty / l.product_qty::float8
                    ELSE 0 END)"""
        return frag

    @api.model
    def _purchase_sql(self, options):
        """Product-level purchase aggregate (one row per product).

        Kept as a standalone statement on purpose: alone, PostgreSQL reliably
        plans it as a parallel partial aggregate. Embedded in a bigger
        statement the planner may fall back to a serial plan (seen at 1.2M
        lines: 0.45 s parallel vs 5.5 s serial)."""
        f = self._fragments(options)
        return f"""
SELECT p.*, p.first_vendor_id <> p.last_vendor_id AS multi_vendor,
       pp.default_code, pp.barcode,
       COALESCE(pt.name->>%(lang)s, pt.name->>'en_US') AS product_name,
       fv.name AS first_vendor_name
  FROM (
    SELECT l.product_id,
           SUM(l.product_uom_qty) AS qty,
           SUM(l.price_subtotal::float8 * {f['conv']}) AS amount,
           SUM({f['rcv']}) AS received,
           SUM(GREATEST(l.product_uom_qty - {f['rcv']}, 0)) AS pending,
           COUNT(*) AS line_count,
           MIN(rp.commercial_partner_id) AS first_vendor_id,
           MAX(rp.commercial_partner_id) AS last_vendor_id
      FROM {f['po_from']}
      JOIN purchase_order_line l ON l.order_id = po.id
     WHERE {f['po_where']}
       AND l.display_type IS NULL AND l.product_id IS NOT NULL {f['product']}
     GROUP BY l.product_id
  ) p
  JOIN product_product pp ON pp.id = p.product_id
  JOIN product_template pt ON pt.id = pp.product_tmpl_id
  JOIN res_partner fv ON fv.id = p.first_vendor_id
 WHERE TRUE {f['search']}"""

    @api.model
    def _duplicate_pairs_sql(self, options):
        """Lines repeating a (PO, product) pair, per product.

        ``No. of POs`` must count distinct POs, but a COUNT(DISTINCT) per
        product forces a sort of every line and blocks the parallel plan of
        the purchase aggregate. Instead that aggregate counts lines, this
        statement counts the surplus lines of repeated pairs, and
        po_count = lines - surplus. Measured on 1.2M lines: ~30 ms for a
        month, ~300 ms for three years."""
        f = self._fragments(options)
        return f"""
SELECT d.product_id, SUM(d.cnt - 1)::int
  FROM (SELECT l.order_id, l.product_id, COUNT(*) AS cnt
          FROM {f['po_from'].replace('JOIN res_partner rp ON rp.id = po.partner_id', '')}
          JOIN purchase_order_line l ON l.order_id = po.id
         WHERE {f['po_where']}
           AND l.display_type IS NULL AND l.product_id IS NOT NULL {f['product']}
         -- same column order as the covering index (PostgreSQL < 17 does
         -- not reorder GROUP BY keys to match an index)
         GROUP BY l.order_id, l.product_id
        HAVING COUNT(*) > 1) d
 GROUP BY d.product_id"""

    @api.model
    def _sales_sql(self, options):
        """Product-level sales aggregate for ``%(scope_ids)s`` (standalone,
        parallel for the same reason as ``_purchase_sql``)."""
        f = self._fragments(options)
        qty_expr, amount_expr = SALE_BASIS[options['sale_basis']]
        return f"""
SELECT sl.product_id,
       -- Odoo 19: uom factor = absolute quantity in reference units, so qty x from / to
       SUM({qty_expr}::float8 * lu.factor / pu.factor) AS qty,
       SUM(COALESCE(({amount_expr})::float8, 0)
           * cf.factor::float8 / COALESCE(NULLIF(so.currency_rate, 0), 1)::float8) AS amount
  FROM sale_order so
  JOIN unnest(%(cf_ids)s::int[], %(cf_factors)s::numeric[]) AS cf(company_id, factor)
    ON cf.company_id = so.company_id
  JOIN sale_order_line sl ON sl.order_id = so.id
  JOIN uom_uom lu ON lu.id = sl.product_uom_id
  JOIN product_product spp ON spp.id = sl.product_id
  JOIN product_template spt_ ON spt_.id = spp.product_tmpl_id
  JOIN uom_uom pu ON pu.id = spt_.uom_id
 WHERE so.date_order >= %(dt_from)s AND so.date_order < %(dt_to)s
   AND so.state IN ('sale', 'done') {f['so_wh']}
   AND sl.display_type IS NULL AND sl.product_id = ANY(%(scope_ids)s)
   AND NOT COALESCE(sl.is_downpayment, false)
 GROUP BY sl.product_id"""

    @api.model
    def _apply_search(self, options, params):
        """Turn the free-text search into a product filter *before* the
        aggregates run, so they only touch the matching products' lines.
        Returns False when nothing can match."""
        if not options['search']:
            return True
        self.env.cr.execute("""
            SELECT pp.id FROM product_product pp
              JOIN product_template pt ON pt.id = pp.product_tmpl_id
             WHERE COALESCE(pt.name->>%(lang)s, pt.name->>'en_US') ILIKE %(search)s
                OR pp.default_code ILIKE %(search)s OR pp.barcode ILIKE %(search)s""", params)
        ids = {r[0] for r in self.env.cr.fetchall()}
        if options['product_ids']:
            ids &= set(options['product_ids'])
        if not ids:
            return False
        options['product_ids'] = params['product_ids'] = sorted(ids)
        return True

    @api.model
    def _product_rows(self, options, params):
        """All product rows of the selection, sales merged, derived metrics
        computed, sorted. O(products) in Python; the O(lines) work is SQL.
        Call ``_apply_search`` first."""
        self.env.cr.execute(self._purchase_sql(options), params)
        rows = [self._clean_row(r) for r in self.env.cr.dictfetchall()]
        if not rows:
            return rows
        # The planner underestimates (PO, product) groups badly (40k estimated
        # vs 984k real at 1.2M lines) and builds a huge hash table. We know
        # the real line count from the aggregate above, so for large scopes
        # we make it stream the (order_id, product_id)-ordered index instead.
        large = sum(r['line_count'] for r in rows) > STREAM_DUPLICATES_ABOVE
        if large:
            self.env.cr.execute('SET LOCAL enable_hashagg = off')
        try:
            self.env.cr.execute(self._duplicate_pairs_sql(options), params)
            surplus = dict(self.env.cr.fetchall())
        finally:
            if large:
                self.env.cr.execute('SET LOCAL enable_hashagg = on')
        for row in rows:
            row['po_count'] = row.pop('line_count') - surplus.get(row['product_id'], 0)
        sales = {}
        self.env.cr.execute(self._sales_sql(options),
                            dict(params, scope_ids=[r['product_id'] for r in rows]))
        for product_id, qty, amount in self.env.cr.fetchall():
            sales[product_id] = (float(qty or 0.0), float(amount or 0.0))
        for row in rows:
            sold_qty, sales_amount = sales.get(row['product_id'], (0.0, 0.0))
            avg_cost = row['amount'] / row['qty'] if row['qty'] else None
            row.update(
                sold_qty=sold_qty,
                sales_amount=sales_amount,
                avg_cost=avg_cost,
                sale_rate=sales_amount / sold_qty if sold_qty else None,
                cogs=sold_qty * (avg_cost or 0.0),
            )
            row['gross_profit'] = row['sales_amount'] - row['cogs']
            row['margin'] = (row['gross_profit'] / row['sales_amount'] * 100.0
                             if row['sales_amount'] else None)
        return self._sort_rows(rows, options)

    @staticmethod
    def _sort_rows(rows, options):
        key = SORT_KEYS[options['sort_by']]
        desc = options['sort_desc']

        def sort_key(row):
            value = row.get(key)
            if isinstance(value, str):
                value = value.lower()
            missing = value is None or value == ''
            # missing values always last, ties broken by product id
            return (missing, (-value if desc and not missing and not isinstance(value, str)
                              else value) if not missing else 0, row['product_id'])

        if key in ('product_name', 'default_code', 'barcode') and desc:
            present = [r for r in rows if r.get(key)]
            absent = [r for r in rows if not r.get(key)]
            present.sort(key=lambda r: (r[key].lower(), r['product_id']), reverse=True)
            return present + sorted(absent, key=lambda r: r['product_id'])
        return sorted(rows, key=sort_key)

    @api.model
    def _grand_sql(self, options):
        """Distinct POs and vendors behind the shown products (one row)."""
        f = self._fragments(options)
        product_scope = f['product']
        return f"""
SELECT COUNT(*) AS po_count, COUNT(DISTINCT rp.commercial_partner_id) AS vendor_count
  FROM {f['po_from']}
 WHERE {f['po_where']}
   AND EXISTS (SELECT 1 FROM purchase_order_line l
                WHERE l.order_id = po.id AND l.display_type IS NULL
                  AND l.product_id IS NOT NULL {product_scope})"""

    @api.model
    def _tune_session(self):
        """JIT compilation costs ~0.5 s on these plans and buys nothing for
        an aggregate that runs once; hash aggregates get room to stay in
        memory. SET LOCAL ends with the transaction (the request)."""
        self.env.cr.execute("SET LOCAL jit = off")
        self.env.cr.execute("SET LOCAL work_mem = '64MB'")

    @api.model
    def _lines_sql(self, options, prefix='WITH'):
        """CTE ``po_lines``: one row per (product, PO) -- detail path."""
        f = self._fragments(options)
        return f"""
{prefix} po_lines AS (
    SELECT l.product_id, po.id AS order_id, rp.commercial_partner_id AS vendor_id,
           po.date_order, po.state = ANY(%(rfq_states)s) AS is_rfq,
           SUM(l.product_uom_qty) AS qty,
           SUM(l.price_subtotal::float8 * {f['conv']}) AS amount,
           SUM({f['rcv']}) AS received,
           SUM(GREATEST(l.product_uom_qty - {f['rcv']}, 0)) AS pending
      FROM {f['po_from']}
      JOIN purchase_order_line l ON l.order_id = po.id
     WHERE {f['po_where']}
       AND l.display_type IS NULL AND l.product_id IS NOT NULL {f['product']}
     GROUP BY l.product_id, po.id, rp.commercial_partner_id, po.date_order, po.state
)"""

    # ------------------------------------------------------------------ #
    # Public data API
    # ------------------------------------------------------------------ #
    @api.model
    def fetch_summary(self, options, offset=0, limit=None):
        """Product rows (one page) + grand totals over the whole selection."""
        self._ppr_check_access()
        self.env.flush_all()
        self._tune_session()
        params = self._get_params(options)
        options = dict(options)
        rows = self._product_rows(options, params) if self._apply_search(options, params) else []
        totals = self._empty_totals()
        if rows:
            for key in ('qty', 'amount', 'received', 'pending', 'sold_qty',
                        'sales_amount', 'cogs'):
                totals[key] = sum(r[key] or 0.0 for r in rows)
            totals['count'] = len(rows)
            totals['detail_count'] = sum(r['po_count'] for r in rows)
            self.env.cr.execute(self._grand_sql(options), params)
            totals['po_count'], totals['vendor_count'] = self.env.cr.fetchone()
        offset = max(int(offset or 0), 0)
        page = rows[offset:offset + min(int(limit), MAX_LIMIT)] if limit else rows[offset:]
        return {'rows': page, 'totals': self._finalize_totals(totals)}

    @api.model
    def fetch_details(self, options, product_ids):
        """Vendor > PO breakdown for the given products (lazy drill-down).

        Restricting ``product_ids`` at the line level keeps this an index
        lookup however large the period is."""
        self._ppr_check_access()
        self.env.flush_all()
        wanted = [int(p) for p in product_ids or []]
        if not wanted:
            return {}
        scoped = dict(options)
        scoped['product_ids'] = sorted(set(wanted) & set(options['product_ids'])) \
            if options['product_ids'] else sorted(set(wanted))
        if not scoped['product_ids']:
            return {}
        params = self._get_params(scoped)
        self._tune_session()
        query = self._lines_sql(scoped) + """
SELECT l.product_id, l.vendor_id, rp.name AS vendor_name,
       l.order_id, po.name AS po_name, l.is_rfq,
       (l.date_order AT TIME ZONE 'UTC' AT TIME ZONE %(tz)s)::date AS po_date,
       l.qty, l.amount, l.received, l.pending
  FROM po_lines l
  JOIN res_partner rp ON rp.id = l.vendor_id
  JOIN purchase_order po ON po.id = l.order_id
 ORDER BY l.product_id, lower(rp.name), l.vendor_id, l.date_order, l.order_id"""
        self.env.cr.execute(query, params)
        result = {}
        for row in self.env.cr.dictfetchall():
            row = self._clean_row(row)
            vendors = result.setdefault(row['product_id'], [])
            if not vendors or vendors[-1]['vendor_id'] != row['vendor_id']:
                vendors.append(self._new_vendor_group(row))
            self._add_to_vendor_group(vendors[-1], row)
        return result

    @api.model
    def iter_detail_rows(self, options, batch_size=5000, product_rows=None):
        """Stream every PO detail row in report order, product totals
        attached. Uses a server-side cursor so a million rows never sit in
        Python memory at once. ``product_rows`` (sorted, from
        ``fetch_summary``) can be passed to avoid recomputing them."""
        self._ppr_check_access()
        self.env.flush_all()
        self._tune_session()
        params = self._get_params(options)
        options = dict(options)
        if not self._apply_search(options, params):
            return
        if product_rows is None:
            product_rows = self._product_rows(options, params)
        if not product_rows:
            return
        by_id = {r['product_id']: r for r in product_rows}
        params['ordered_ids'] = [r['product_id'] for r in product_rows]
        query = self._lines_sql(options) + """,
pv AS (SELECT product_id, COUNT(DISTINCT vendor_id) AS vendor_count
         FROM po_lines GROUP BY product_id)
SELECT l.product_id, pv.vendor_count,
       l.vendor_id, rp.name AS vendor_name, l.order_id, po.name AS po_name, l.is_rfq,
       (l.date_order AT TIME ZONE 'UTC' AT TIME ZONE %(tz)s)::date AS po_date,
       l.qty, l.amount, l.received, l.pending
  FROM unnest(%(ordered_ids)s::int[]) WITH ORDINALITY AS o(product_id, rank)
  JOIN po_lines l ON l.product_id = o.product_id
  JOIN pv ON pv.product_id = l.product_id
  JOIN res_partner rp ON rp.id = l.vendor_id
  JOIN purchase_order po ON po.id = l.order_id
 ORDER BY o.rank, lower(rp.name), l.vendor_id, l.date_order, l.order_id"""
        cnx = self.env.cr._cnx
        with cnx.cursor(name='po_ppr_stream_%s' % id(self)) as cur:
            cur.itersize = batch_size
            cur.execute(query, params)
            columns = None
            while True:
                chunk = cur.fetchmany(batch_size)
                if columns is None:
                    columns = [d[0] for d in cur.description]
                if not chunk:
                    break
                for values in chunk:
                    row = self._clean_row(dict(zip(columns, values)))
                    product = by_id[row['product_id']]
                    for key in ('default_code', 'barcode', 'product_name'):
                        row[key] = product[key]
                    for key in ('qty', 'amount', 'received', 'pending', 'po_count',
                                'sold_qty', 'sales_amount', 'avg_cost', 'sale_rate',
                                'cogs', 'gross_profit'):
                        row['p_' + key] = product[key]
                    row['p_vendor_count'] = row.pop('vendor_count')
                    yield row

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clean_row(row):
        """Decimal -> float, drop the window helper columns."""
        out = {}
        for key, value in row.items():
            if key.startswith('g_'):
                continue
            if value is not None and type(value).__name__ == 'Decimal':
                value = float(value)
            out[key] = value
        return out

    @staticmethod
    def _empty_totals():
        return dict(count=0, detail_count=0, qty=0.0, amount=0.0, received=0.0,
                    pending=0.0, sold_qty=0.0, sales_amount=0.0, cogs=0.0,
                    po_count=0, vendor_count=0)

    @staticmethod
    def _finalize_totals(totals):
        totals = {k: (float(v) if type(v).__name__ == 'Decimal' else (v or 0))
                  for k, v in totals.items()}
        totals['gross_profit'] = totals['sales_amount'] - totals['cogs']
        totals['avg_cost'] = totals['amount'] / totals['qty'] if totals['qty'] else 0.0
        totals['sale_rate'] = (totals['sales_amount'] / totals['sold_qty']
                               if totals['sold_qty'] else 0.0)
        totals['margin'] = (totals['gross_profit'] / totals['sales_amount'] * 100.0
                            if totals['sales_amount'] else 0.0)
        return totals

    @staticmethod
    def _new_vendor_group(row):
        return {'vendor_id': row['vendor_id'], 'vendor_name': row['vendor_name'],
                'qty': 0.0, 'amount': 0.0, 'received': 0.0, 'pending': 0.0,
                'po_count': 0, 'lines': []}

    @staticmethod
    def _add_to_vendor_group(group, row):
        for key in ('qty', 'amount', 'received', 'pending'):
            group[key] += row[key] or 0.0
        group['po_count'] += 1
        row['avg_cost'] = row['amount'] / row['qty'] if row['qty'] else 0.0
        group['avg_cost'] = group['amount'] / group['qty'] if group['qty'] else 0.0
        group['lines'].append(row)

    @api.model
    def product_labels(self, product_ids):
        """Full display names (variants included), batched."""
        labels = {}
        Product = self.env['product.product'].with_context(active_test=False)
        ids = list(product_ids)
        for start in range(0, len(ids), 5000):
            for product in Product.browse(ids[start:start + 5000]):
                labels[product.id] = product.display_name
        return labels

    @api.model
    def filter_labels(self, options):
        """Human readable filter summary, shared by web / PDF / Excel."""
        def names(model, ids, limit=6):
            records = self.env[model].with_context(active_test=False).browse(ids).exists()
            shown = ', '.join(records[:limit].mapped('display_name'))
            if len(records) > limit:
                shown += _(' (+%s more)', len(records) - limit)
            return shown

        doc_types = dict(self.env['po.product.profit.wizard']._fields['doc_type']
                         ._description_selection(self.env))
        modes = dict(self.env['po.product.profit.wizard']._fields['mode']
                     ._description_selection(self.env))
        bases = dict(self.env['po.product.profit.wizard']._fields['sale_basis']
                     ._description_selection(self.env))
        lang = self.env['res.lang']._lang_get(self.env.lang or 'en_US')
        fmt = lang.date_format if lang else '%m/%d/%Y'
        _company, currency = self._report_currency(options)
        return {
            'period': '%s - %s' % (
                fields.Date.to_date(options['date_from']).strftime(fmt),
                fields.Date.to_date(options['date_to']).strftime(fmt)),
            'doc_type': doc_types.get(options['doc_type']),
            'mode': modes.get(options['mode']),
            'sale_basis': bases.get(options['sale_basis']),
            'vendors': names('res.partner', options['partner_ids']) or _('All'),
            'products': names('product.product', options['product_ids']) or _('All'),
            'warehouses': names('stock.warehouse', options['warehouse_ids']) or _('All'),
            'companies': names('res.company', options['company_ids']),
            'currency': currency.name,
            'search': options['search'],
        }
