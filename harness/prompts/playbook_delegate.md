## When to delegate

`delegate(task)` hands a self-contained question to a fresh sub-agent: its own context
window, its own kernel namespace, the same library and database, **read-only** (every
write raises `PermissionError`). It returns a report of at most ~400 tokens, which
appears in the same tool result and is kept in `delegate.reports[n]`. Use it for work
that is independent of what you are holding in your head, so that its reading and
reasoning do not accumulate in *your* context:

* **Sourcing analysis, one sub-agent per product (or per vendor group):** "For product
  `P-...`, need 40 by 2026-09-20: every vendor offer with MOQ, tier price, lead time and
  arrival; the min-cost feasible split (`erp.cheapest_buy`); any note-stated ceiling."
* **Feasibility, one sub-agent per order:** "For S00012 (due 2026-09-18, lines …): what
  is on hand, what must be bought or made, the earliest completion (`erp.earliest_build`),
  and whether the due date can be met."
* **Independent audit before `finish`:** "Run `check.all(erp)` and, for every confirmed
  order created today, verify origin, dates, quantities and invoicing against the rules
  below: …" — a reader with no stake in the plan catches what its author skips.

Write the task as a brief to a colleague who has not read the instruction: product
codes, quantities, due dates, the objective, what to return. A sub-agent cannot see
your variables or your plan ledger. Do not delegate one-liners (a single `erp.stock()`
is cheaper inline) and do not delegate the writes — sub-agents cannot perform them;
you execute the plan on main yourself. Several delegations run one after another;
each costs its own steps and tokens against the same trial caps.
