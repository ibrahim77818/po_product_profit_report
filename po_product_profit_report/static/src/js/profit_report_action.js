/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { download } from "@web/core/network/download";
import { formatFloat } from "@web/views/fields/formatters";
import { deserializeDate, formatDate } from "@web/core/l10n/dates";
import { _t } from "@web/core/l10n/translation";

const WIZARD = "po.product.profit.wizard";
const PAGE_SIZE = 200;

/**
 * Interactive web view of the PO Product Profit Report.
 *
 * Stateless on the server: the sanitized options travel with the action, so
 * paging, sorting and drill-down are plain RPCs that never depend on a
 * (vacuumable) wizard record. Product rows are paged; vendor/PO detail is
 * fetched lazily per product, so the page stays light on any data volume.
 */
export class PoProductProfitReport extends Component {
    setup() {
        this.orm = useService("orm");
        this.actionService = useService("action");
        this.ui = useService("ui");
        this.notification = useService("notification");
        const params = (this.props.action && this.props.action.params) || {};
        this.state = useState({
            options: params.options || {},
            rows: [],
            totals: {},
            filters: {},
            currencyId: false,
            qtyDigits: 2,
            amountDigits: 2,
            loading: true,
            loadingMore: false,
            expanded: {},
            details: {},
            detailLoading: {},
            search: (params.options && params.options.search) || "",
        });
        this.columns = [
            { key: "qty", label: _t("Purchased Qty"), kind: "qty", sort: "qty" },
            { key: "amount", label: _t("Purchased Amount"), kind: "amount", sort: "amount" },
            { key: "avg_cost", label: _t("Avg. PO Rate"), kind: "amount" },
            { key: "po_count", label: _t("No. of POs"), kind: "int", sort: "po_count" },
            { key: "received", label: _t("Received Qty"), kind: "qty" },
            { key: "pending", label: _t("Pending Qty"), kind: "qty", sort: "pending" },
            { key: "sold_qty", label: _t("Sold Qty"), kind: "qty", sort: "sold" },
            { key: "sale_rate", label: _t("Sale Rate"), kind: "amount" },
            { key: "sales_amount", label: _t("Sales Amount"), kind: "amount", sort: "sales" },
            { key: "gross_profit", label: _t("Gross Profit"), kind: "amount", sort: "profit" },
            { key: "margin", label: _t("Margin %"), kind: "pct", sort: "margin" },
        ];
        onWillStart(() => this.load());
    }

    // ------------------------------------------------------------------
    // Data
    // ------------------------------------------------------------------
    async load() {
        this.state.loading = true;
        try {
            const data = await this.orm.call(WIZARD, "get_web_data", [
                this.state.options, 0, PAGE_SIZE,
            ]);
            this.applyData(data, false);
        } finally {
            this.state.loading = false;
        }
    }

    async loadMore() {
        this.state.loadingMore = true;
        try {
            const data = await this.orm.call(WIZARD, "get_web_data", [
                this.state.options, this.state.rows.length, PAGE_SIZE,
            ]);
            this.applyData(data, true);
        } finally {
            this.state.loadingMore = false;
        }
    }

    applyData(data, append) {
        const rows = data.rows.map((r) => ({
            ...r,
            margin: r.sales_amount ? (r.gross_profit / r.sales_amount) * 100 : 0,
        }));
        this.state.rows = append ? [...this.state.rows, ...rows] : rows;
        if (!append) {
            this.state.expanded = {};
            this.state.details = {};
        }
        this.state.totals = data.totals;
        this.state.filters = data.filters;
        this.state.options = data.options;
        this.state.currencyId = data.currency_id;
        this.state.qtyDigits = data.qty_digits;
        this.state.amountDigits = data.amount_digits;
    }

    async fetchDetails(productIds) {
        const missing = productIds.filter((id) => !this.state.details[id]);
        if (!missing.length) {
            return;
        }
        missing.forEach((id) => (this.state.detailLoading[id] = true));
        try {
            const result = await this.orm.call(WIZARD, "get_web_details", [
                this.state.options, missing,
            ]);
            for (const id of missing) {
                this.state.details[id] = result[String(id)] || [];
            }
        } finally {
            missing.forEach((id) => delete this.state.detailLoading[id]);
        }
    }

    // ------------------------------------------------------------------
    // Interaction
    // ------------------------------------------------------------------
    get isDetailed() {
        return this.state.options.mode === "detailed";
    }

    get hasMore() {
        return this.state.rows.length < (this.state.totals.count || 0);
    }

    async toggleProduct(row) {
        if (!this.isDetailed) {
            return;
        }
        const id = row.product_id;
        if (this.state.expanded[id]) {
            delete this.state.expanded[id];
            return;
        }
        this.state.expanded[id] = true;
        await this.fetchDetails([id]);
    }

    async expandAll() {
        const ids = this.state.rows.map((r) => r.product_id);
        ids.forEach((id) => (this.state.expanded[id] = true));
        await this.fetchDetails(ids);
    }

    collapseAll() {
        this.state.expanded = {};
    }

    setMode(mode) {
        this.state.options = { ...this.state.options, mode };
        if (mode === "totals") {
            this.collapseAll();
        }
    }

    async sortBy(column) {
        if (!column.sort) {
            return;
        }
        const options = { ...this.state.options };
        if (options.sort_by === column.sort) {
            options.sort_desc = !options.sort_desc;
        } else {
            options.sort_by = column.sort;
            options.sort_desc = column.sort !== "name";
        }
        this.state.options = options;
        await this.load();
    }

    sortIcon(sortKey) {
        if (!sortKey || this.state.options.sort_by !== sortKey) {
            return "";
        }
        return this.state.options.sort_desc ? "fa fa-sort-desc" : "fa fa-sort-asc";
    }

    onSearchKeydown(ev) {
        if (ev.key === "Enter") {
            this.applySearch();
        }
    }

    async applySearch() {
        this.state.options = { ...this.state.options, search: this.state.search.trim() };
        await this.load();
    }

    async clearSearch() {
        this.state.search = "";
        await this.applySearch();
    }

    openRecord(model, id) {
        this.actionService.doAction({
            type: "ir.actions.act_window",
            res_model: model,
            res_id: id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    async changeFilters() {
        const action = await this.orm.call(WIZARD, "get_wizard_action", [this.state.options]);
        await this.actionService.doAction(action);
    }

    async printPdf() {
        const action = await this.orm.call(WIZARD, "get_pdf_action", [this.state.options]);
        await this.actionService.doAction(action);
    }

    async exportXlsx() {
        await downloadXlsx(this.ui, this.state.options);
    }

    // ------------------------------------------------------------------
    // Formatting
    // ------------------------------------------------------------------
    fmt(value, kind) {
        if (value === null || value === undefined || value === false) {
            value = 0;
        }
        switch (kind) {
            case "qty":
                return formatFloat(value, { digits: [69, this.state.qtyDigits] });
            case "amount":
                return formatFloat(value, { digits: [69, this.state.amountDigits] });
            case "pct":
                return formatFloat(value, { digits: [69, 1] }) + "%";
            default:
                return formatFloat(value, { digits: [69, 0] });
        }
    }

    fmtDate(value) {
        return value ? formatDate(deserializeDate(value)) : "";
    }

    isNegative(value) {
        return typeof value === "number" && value < -0.0000001;
    }
}

PoProductProfitReport.template = "po_product_profit_report.ProfitReport";

async function downloadXlsx(ui, options) {
    ui.block();
    try {
        await download({
            url: "/po_product_profit_report/xlsx",
            data: { options: JSON.stringify(options) },
        });
    } finally {
        ui.unblock();
    }
}

/** Wizard "Export Excel" button: download without leaving the page, so
 * errors surface as normal Odoo dialogs. */
async function downloadXlsxAction(env, action) {
    await downloadXlsx(env.services.ui, (action.params && action.params.options) || {});
    return { type: "ir.actions.act_window_close" };
}

registry.category("actions").add("po_product_profit_report.web_view", PoProductProfitReport);
registry.category("actions").add("po_product_profit_report.download_xlsx", downloadXlsxAction);
