# Preloaded library

Every name below is already bound in your Python kernel — do not import anything to
use it, and do not reimplement it. Generated from the source, so it cannot describe a
function that does not exist.

## `erp`

Typed Odoo client over XML-RPC: reads, planning helpers, and write helpers that refuse

Bound in your kernel: `erp`, `Erp`, `OdooError`.

**class `OdooError`** — An Odoo-side failure, carrying Odoo's own message rather than our traceback.

**class `Erp`** — One authenticated connection to one database.
- `uid()`
- `models()`
- `on(db)` — A client for another database on the same server (used by `state`).
- `call(model, method, args=(), kwargs=None)` — Raw `execute_kw`. Every non-read call is appended to `write_log`.
- `search_read(model, domain, fields, limit=80, order=None)`
- `get(model, ids, fields=None)`
- `fields(model, like=None)` — What fields a model really has — the antidote to guessing names.
- `count(model, domain=None)`
- `sales_orders(state=None, due_before=None, ids=None)`
- `so_lines(so_ids)`
- `stock(product_ids=None)` — On-hand / reserved / free per product, summed over internal locations only.
- `boms(product_ids=None)`
- `suppliers(product_ids=None)` — Vendor offers from `product.supplierinfo`.
- `purchase_orders(state=None, ids=None)`
- `po_lines(po_ids)`
- `productions(state=None, ids=None)`
- `pickings(picking_type=None, state=None, origin=None)`
- `workcenters()` — Work centres with the numbers any assembly-capacity rule needs.
- `workcenter_options(product_id)` — Where this product can be assembled, with per-unit minutes and cost, and the
- `invoices(move_type='out_invoice', state=None)`
- `today()` — The server's idea of now, as an Odoo datetime string (UTC).
- `feasible_vendors(product_id, qty, need_by, order_date=None)` — Vendors who can land `qty` of a product by `need_by`, with the arrival date.
- `cheapest_buy(product_id, qty, need_by=None, order_date=None)` — The cheapest way to buy `qty` of a product: which vendors, how many from each.
- `earliest_build(product_id, qty, order_date=None, need_by=None, _depth=0)` — When an MO for `qty` could start, given components on hand, purchasable, or
- `create_po(vendor_id, lines, date_planned=None, origin=None, force=False)` — Create a purchase order. `lines` = [(product_id, qty[, price_unit[,
- `add_po_lines(po_id, lines)` — Add lines to an existing purchase order (draft or confirmed).
- `origin_capacity(product_id, tokens)` — How much of `product_id` each named order can absorb: a sales order's line
- `vendor_price(vendor_id, product_id, qty)` — The supplierinfo price for this vendor/product at this quantity tier.
- `confirm_po(po_id)` — `purchase.order.button_confirm`.
- `receive(po_id, force=False)` — Validate every incoming picking of a PO. Returns the picking ids validated.
- `bom_for(product_id, bom_id=None)` — The BOM Odoo would use for this product: id, lead time (`produce_delay`, days),
- `create_mo(product_id, qty, bom_id=None, date_start=None, date_deadline=None, origin=None, workcenter_id=None, force=False)` — `mrp.production.create`. Odoo picks the BOM if one is not named.
- `confirm_mo(mo_id)` — `mrp.production.action_confirm`.
- `produce(mo_id, qty=None)` — Reserve, set `qty_producing`, consume the components, and mark done.
- `deliver(so_id)` — Validate every outgoing picking of a sales order.
- `payment_term_id(name)` — The `account.payment.term` whose name contains `name` (e.g. "Immediate").
- `invoice(so_id, method='delivered', amount=None, payment_term=None)` — Create customer invoice(s) for a sales order via `sale.advance.payment.inv`.
- `post(invoice_ids)` — `account.move.action_post`.
- `cancel(model, ids)` — Cancel records with whichever cancel method the model exposes.

## `check`

End-state invariants derived from ERP practice, never from grader code. Hard checks

Bound in your kernel: `Check`, `Rule` (and the module itself as `check`).

**class `Check`**

**class `Rule`** — A task-specific rule the agent writes from the instruction.

- `invariants(client=None)` — Run the generic end-state invariants.
- `rules(client=None, rule_list=None)` — Run the agent's task-specific rules.
- `register(rule)` — Keep a rule for every later `check.all()`; re-registering an id replaces it.
- `registered()`
- `clear()`
- `all(client=None)` — Invariants plus every registered rule, in one table.
- `failures(client=None, hard_only=True)` — The rows that failed — what `finish` gates on.

## `plan`

A plan ledger with the task's objective, summarised for periodic reinjection.

Bound in your kernel: `plan`, `Plan`.

**class `Plan`** — An ordered list of steps with a status each. Ids are 1-based and stable.
- `set(texts, objective='')` — Replace the whole plan. Use once, at the start.
- `add(text)` — Append one step and return its id.
- `update(item_id, status, note='')`
- `show()`
- `summary()` — At most `SUMMARY_LINES` lines, for re-injection into the conversation.
- `open_items()`
- `is_complete()`
- `__str__()`

## `finish`

The finish tool: runs the checks and refuses (up to three times) while a hard check

Bound in your kernel: `finish`, `FINISH_SENTINEL`.

- `finish(summary='', client=None, gate=True)` — End the episode, if the hard checks agree.

## `db`

Read-only SQL against the task database, enforced by a SELECT-only Postgres role.

Bound in your kernel: `db`.

## `state`

Database snapshots (CREATE DATABASE ... TEMPLATE) and dry runs of a plan on a clone.

Bound in your kernel: `state`.

## `delegate`

Sub-agents (container side): the read-only erp proxy, the namespace lockdown, and the

Bound in your kernel: `delegate`, `ErpReadOnly`.

**class `ErpReadOnly`** — A read-only `Erp`: reads and planning helpers pass through, writes raise

- `delegate(task, max_steps=DEFAULT_MAX_STEPS)` — Hand a self-contained question to a fresh read-only sub-agent; its ~400-token report

## `fmt`

Tables capped at 40 rows and a page store for oversized tool output.

Bound in your kernel: `Table`, `PageStore`, `pages`, `show`, `paginate` (and the module itself as `fmt`).

**class `Table`** — A list of dicts that prints as a fixed-width table and stays small.
- `all()` — Every row, unabridged.
- `df()` — A pandas DataFrame when pandas is available, else a clear error.
- `column(name)`
- `where(predicate)`
- `sort(key, reverse=False)`
- `total(column)`
- `__str__()`

**class `PageStore`** — Holds oversized tool output and hands it out a page at a time.
- `capture(text)`
- `n_pages(handle)`
- `show(handle, page=1)`
- `handles()`

- `show(handle, page=1)` — Return one page of a stored oversized output.
- `paginate(text)` — Store `text` if it is oversized and return what the model should see.

## `brief`

A short summary of the task environment (demand, stock, offers, BOMs, work centres),

Bound in your kernel: `brief`.
