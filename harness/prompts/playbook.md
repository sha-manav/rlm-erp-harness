# Odoo 19 procurement and manufacturing playbook

Everything here is Odoo behaviour, verified against this image. It is generic ERP practice,
not guidance about any particular task.

## Flows

**Purchasing.** Create the order with lines carrying `price_unit` *and* `date_planned`
(Odoo's onchange defaults do not fire over RPC — an omitted price silently becomes 0.00),
then `button_confirm`. **Do not validate the receipt.** Goods arrive at
`order date + supplierinfo.delay`, not when you click; a receipt validated today for a
vendor with a 19-day lead time fabricates stock that does not exist, and the plan fails on
timing however correct everything else is. `date_planned` must be on or after that
arrival date *and* on or before the customer due date it serves — if no vendor can meet
both, say so in the summary rather than inventing a date.

```python
po = erp.create_po(vendor_id, [(product_id, qty)],
                   date_planned="2026-09-21 08:00:00",   # >= order date + delay
                   origin="S00030")                       # price looked up from supplierinfo
erp.confirm_po(po)                                       # leave it confirmed; no receive()
```

**One purchase order per supplier offer.** A second need from a vendor who already has
an open order for that product goes onto that order — `erp.add_po_lines(po_id, [(product_id,
qty)])` works on a confirmed PO — never onto a second PO; `create_po` refuses one.

**A finished-goods purchase must be absorbed entirely by the sales orders it names.** When
the vendor's minimum exceeds the shortfall (8 for an order needing 6), name additional
orders due on or after the arrival date to absorb the extra units — orders you deliver
from stock still count — and never leave units that no named order can take. Only a
component purchase at the vendor's minimum may exceed the MOs it names.

`origin` is the audit trail — *this purchase is for that order*. Name **only** the orders
this purchase feeds: exact references (`S00004`, or `S00003, S00004`), and only those
whose due date the arrival meets. An order due before the goods land is not fed by this
purchase, whatever else it says; naming it is refused, and the refusal lists the
references that do qualify. Orders you deliver from stock are not fed by the purchase.

`erp.receive(po)` exists for goods whose planned date has already passed — not for goods
that *will* arrive in time. "Arrives before the due date" means the order is correct as it
stands; it does not mean you receive it now. **`force=True` never makes a check pass**: a
forced early receipt is refused at finish as fabricated stock. If a delivery cannot be
covered from stock plus purchases that arrive in time, that is the answer — report it,
do not manufacture it.

**Manufacturing.** An MO is a plan only when it carries four things: `date_start`;
`date_deadline` (the due date — Odoo leaves it **empty**, and it must be on or after
start + the BOM's lead time); `origin` (the sales order it builds for; a sub-assembly MO
names the parent MO); and a work centre on its work order (choose with
`erp.workcenter_options`; every centre's Internal Notes state a horizon-wide minute
limit that all products sharing it count against — `qty × min/unit`). Components not on
hand are bought first, on POs whose `origin` is the MO reference and whose arrival is on
or before the MO's start. Confirm the MO; produce only if the task says the goods are
needed now and the components are actually on hand.

```python
plan = erp.earliest_build(product_id, qty)          # date_start the components allow,
                                                     # date_deadline, what to buy from whom,
                                                     # work-centre options with minutes free
mo = erp.create_mo(product_id, qty, date_start=plan["date_start"],
                   date_deadline=plan["date_deadline"],      # >= start + lead time
                   origin="S00030", workcenter_id=1)         # the order it is for; the centre
erp.confirm_mo(mo)
po = erp.create_po(vendor_id, [(component_id, need)],        # components first
                   date_planned=plan["date_start"], origin=erp.get("mrp.production", [mo], ["name"])[0]["name"])
```

When one centre's minutes cannot hold the whole quantity, split it: two MOs on two
centres (`units_fit` says how many each takes), or make part and buy the rest. An
**alternate** centre has no stated rate for the product — Odoo shows the primary's
minutes there, and that number is not the truth. The helpers assume an alternate is
slower (`rate` column says so); use their `units_fit`, never the primary's rate, and
leave that headroom rather than filling an alternate to its limit.
`erp.produce` exists for goods needed today: it reserves, sets `qty_producing`,
**writes the component consumption explicitly** and marks done — without that Odoo
finishes the order having consumed nothing.

**Sales.** Confirm, and set `commitment_date`. Deliver **only what is on hand now** —
a delivery draws from stock, and stock that is still on a purchase order is not on hand.
Invoice what you delivered and post it; an order delivered but not invoiced, or invoiced
but left in draft, is unfinished work. **When the task states an invoicing policy** —
"after confirming each retained order, create and post exactly one linked invoice", a
down payment, named payment terms — that policy applies to **every** retained order,
delivered or not: `erp.invoice(so_id, payment_term="Immediate Payment")` (the wizard
invoices ordered quantities under an order-based invoice policy). Write the policy into
`plan.set` as a rule; the soft `so_invoiced` check lists what is still uninvoiced. Orders waiting on incoming goods stay confirmed
with their delivery pending.

```python
erp.call("sale.order", "action_confirm", [[so_id]])
erp.deliver(so_id)
erp.post(erp.invoice(so_id))
```

## Planning before acting

Enumerate every feasible option for each shortage before choosing one, then rank them by
the task's stated objective (`plan.objective`) — lowest spend only when that *is* the
objective or the tie-breaker:

* buy from each vendor that lists the product, at each `min_qty` tier;
* make it, if a BOM exists — then recurse onto its components.

Do not do the date arithmetic by hand — it is where plans go wrong. Two helpers do it
against the live data:

```python
erp.feasible_vendors(product_id, qty, need_by)   # only vendors who can land it in time:
                                                  # arrival date (use as date_planned), MOQ,
                                                  # line total, vendor notes; cheapest first
erp.earliest_build(product_id, qty)              # when an MO can start and finish: per-
                                                  # component stock, what to buy from whom,
                                                  # lead time, work-centre options
erp.workcenter_options(product_id)               # centres that assemble it: min/unit, cost,
                                                  # minutes free, units that fit
erp.cheapest_buy(product_id, qty, need_by)       # the min-cost split across vendors that
                                                  # land in time (MOQ, note-stated maxima):
                                                  # PO lines to write, total in the title
erp.earliest_build(product_id, qty, need_by=due) # with a due date: components sourced the
                                                  # cheapest feasible way (buy_lines), not
                                                  # the fastest
```

Buy exactly the shortfall (`needed - free_now`), split the way `cheapest_buy` says —
**every purchase line comes from `cheapest_buy`, never from picking rows off the
`feasible_vendors` table by hand**: two plans that met every constraint lost on spend
alone (66 + 7 where 63 + 10 was cheaper; 0.35% over on a hand-picked split). Pass the
MO's start as `need_by` for components (`earliest_build(..., need_by=)` does this).

An empty `feasible_vendors` table means no listed vendor can make the date — cover the
demand from stock or manufacturing, or say so in the summary. Never invent a
`date_planned` earlier than the arrival these return.

Dates are where these plans go wrong. A plan that is correct in vendor, quantity and price
but whose receipt lands one day after the commitment date covers nothing. Check, for every
demand: does what I ordered arrive before it is needed, and are an MO's components on hand
or on a receipt landing before the MO starts?

Cheapest-per-unit is not cheapest overall once minimum quantities and tier prices are in,
but a cheap infeasible plan scores nothing at all.

## Data model

| model | fields that matter |
|---|---|
| `product.product` / `product.template` | `default_code`, `list_price`, `standard_price`, `qty_available` |
| `product.supplierinfo` | `partner_id`, `min_qty`, `price`, `delay` — **no `max_qty` exists**; a vendor's ceiling, if a task states one, is in `res.partner.comment` (Internal Notes) |
| `mrp.bom` / `mrp.bom.line` | `product_qty` (the batch size the components are quoted for), `bom_line_ids` |
| `sale.order` / `.line` | `commitment_date` is the customer due date; `delivery_status`, `invoice_status` |
| `purchase.order` / `.line` | `date_planned` on both header and line; `origin` carries the source reference; `receipt_status` |
| `mrp.production` | `product_qty`, `bom_id`, `date_start`, `qty_producing`, `components_availability` |
| `stock.picking` / `stock.move` | receipts vs deliveries by `picking_type_id.code`; multi-step routes make several pickings per order |
| `stock.quant` | `quantity`, `reserved_quantity` — **internal locations only**, or you will count stock that is not yours |
| `account.move` | `move_type`, `state`, `invoice_origin` |

## Gotchas

* **Underscore methods are not RPC-callable.** Use the wizard, or the `erp` helper.
* **Odoo 19 has no `quantity_done`.** A picking needs `quantity` + `picked = True` on each
  move before `button_validate`, or it validates as a zero-quantity transfer that still
  reads `state = done`.
* **Many2one writes take an id; one2many writes take command tuples** `(0, 0, vals)`.
* **Datetimes are UTC strings**, `"%Y-%m-%d %H:%M:%S"`.
* **Names come from `ir.sequence`** — never invent `S00031`; read it back after create.
* **Confirming a PO adds that vendor to the product's supplier list.** So "is this vendor
  listed?" is not a test you can apply after the fact — decide before ordering.
* **Components must exist before an MO starts**: on hand, or on a receipt landing earlier.
* **Receipt date = order date + `supplierinfo.delay`.** A receipt that lands after the
  customer due date it feeds does not cover that order, however correct the quantities are.
* **Never hardcode ids obtained during a rehearsal.** A snapshot database numbers records
  its own way; look records up by name or domain so the same function runs on both.

## Working economically

Every tool result is re-sent with every later turn. Reads return `Table`, which caps at 40
rows — call `.all()` only when you need the rest, and `show("h3", 2)` to page long output.
Do the arithmetic in the kernel and print the answer, not the raw rows.
