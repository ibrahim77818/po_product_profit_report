# -*- coding: utf-8 -*-
{
    'name': 'PO Product Profit Report',
    'version': '17.0.1.0.0',
    'summary': 'Purchased vs sold profitability per product at actual PO rates '
               '(web, PDF, Excel), built for 1M+ line databases',
    'description': """
PO Product Profit Report
========================
A Purchase > Reporting analysis that answers, per product, vendor and purchase
order: how much was bought, at what actual PO rates, how much has been
received, what is still pending, what was sold in the same period, and the
resulting gross profit.

* Amounts at actual PO line rates (10 @ 1 + 10 @ 2 = 30), never product cost
* RFQs, Purchase Orders or both; vendor, product, warehouse and company scope
* Detailed (product > vendor > PO) or Totals Only mode
* Interactive web view with lazy drill-down, sorting and search
* PDF (landscape, repeated table headers, Arabic RTL) and formatted Excel
* Set-based SQL (CTEs) with covering indexes: no ORM loops over order lines
* Does not override or modify any standard Odoo report
""",
    'author': 'Ibrahim Yahia',
    'website': 'https://www.linkedin.com/in/ibrahim-yahia',
    'category': 'Inventory/Purchase',
    'license': 'LGPL-3',
    'depends': ['purchase_stock', 'sale_stock'],
    'data': [
        'security/ir.model.access.csv',
        'report/po_product_profit_report_templates.xml',
        'wizard/po_product_profit_wizard_views.xml',
        'views/menu_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'po_product_profit_report/static/src/scss/profit_report.scss',
            'po_product_profit_report/static/src/js/profit_report_action.js',
            'po_product_profit_report/static/src/xml/profit_report_action.xml',
        ],
        'web.report_assets_common': [
            'po_product_profit_report/static/src/scss/profit_report_pdf.scss',
        ],
    },
    'images': ['static/description/banner.png'],
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'application': False,
}
