# Schema card — the twelve models these tasks touch

Field names are Odoo 19 as verified against this image. When you need a field that is not
here, ask Odoo rather than guessing: `erp.fields("stock.move", "qty")`.

**product.product** — `id`, `default_code` (the SKU in task text), `name`, `list_price`
(selling), `standard_price` (cost), `qty_available`, `product_tmpl_id`, `seller_ids`

**product.template** — `id`, `name`, `list_price`, `standard_price`, `seller_ids`.
Vendor offers and BOMs usually hang off the *template*, not the variant.

**product.supplierinfo** — `partner_id` (vendor), `product_tmpl_id`, `product_id` (often
`False`), `min_qty` (MOQ / tier threshold), `price` (unit price at that tier), `delay`
(lead time in days), `currency_id`

**res.partner** — `id`, `name`, `ref`, `customer_rank`, `supplier_rank`, `comment`
(Internal Notes — vendor ceilings and policy notes live here, as HTML)

**sale.order** — `name`, `partner_id`, `state` (`draft`/`sale`/`cancel`),
`commitment_date` (customer due date), `amount_total`, `order_line`, `picking_ids`,
`invoice_ids`, `delivery_status`, `invoice_status`

**sale.order.line** — `order_id`, `product_id`, `product_uom_qty`, `qty_delivered`,
`qty_invoiced`, `price_unit`

**purchase.order** — `name`, `partner_id`, `state` (`draft`/`purchase`/`cancel`),
`date_planned`, `amount_total`, `order_line`, `picking_ids`, `origin`, `receipt_status`

**purchase.order.line** — `order_id`, `product_id`, `product_qty`, `price_unit`,
`date_planned`, `qty_received`

**mrp.bom** — `product_tmpl_id`, `product_id`, `product_qty` (batch size the component
quantities are stated for), `bom_line_ids`, `type`
**mrp.bom.line** — `bom_id`, `product_id`, `product_qty`

**mrp.production** — `name`, `product_id`, `product_qty`, `bom_id`, `date_start`,
`date_finished`, `qty_producing`, `state`, `components_availability`, `move_raw_ids`

**stock.picking** — `name`, `picking_type_id` (`.code` is `incoming`/`outgoing`/`internal`),
`origin`, `state`, `scheduled_date`, `move_ids`
**stock.move** — `picking_id`, `product_id`, `product_uom_qty` (demand), `quantity` (done),
`picked`, `state`

**stock.quant** — `product_id`, `location_id`, `quantity`, `reserved_quantity`. Filter to
`location_id.usage = 'internal'`.

**account.move** — `name`, `move_type` (`out_invoice` for customer invoices), `state`
(`draft`/`posted`), `amount_total`, `invoice_origin`, `partner_id`
