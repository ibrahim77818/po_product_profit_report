# -*- coding: utf-8 -*-
import json

from werkzeug.exceptions import InternalServerError, NotFound

from odoo import http
from odoo.http import content_disposition, request
from odoo.tools import html_escape

XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


class PoProductProfitController(http.Controller):

    @http.route('/po_product_profit_report/xlsx/<int:wizard_id>', type='http', auth='user')
    def xlsx_from_wizard(self, wizard_id, **kwargs):
        # Transient records are only visible to their creator.
        wizard = request.env['po.product.profit.wizard'].browse(wizard_id).exists()
        if not wizard:
            raise NotFound()
        return self._xlsx_response(lambda: wizard._get_options())

    @http.route('/po_product_profit_report/xlsx', type='http', auth='user', methods=['POST'])
    def xlsx_from_options(self, options='{}', **kwargs):
        return self._xlsx_response(lambda: json.loads(options))

    def _xlsx_response(self, get_options):
        try:
            filename, content = request.env['po.product.profit.xlsx'].generate(get_options())
        except Exception as exc:  # same error contract as /report/download
            error = {'code': 200, 'message': 'Odoo Server Error',
                     'data': http.serialize_exception(exc)}
            response = request.make_response(html_escape(json.dumps(error)))
            raise InternalServerError(response=response) from exc
        return request.make_response(content, headers=[
            ('Content-Type', XLSX_MIME),
            ('Content-Length', len(content)),
            ('Content-Disposition', content_disposition(filename)),
        ])
