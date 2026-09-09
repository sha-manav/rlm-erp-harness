"""A plan ledger with the task's objective, summarised for periodic reinjection."""

from __future__ import annotations

from .fmt import Table

EXPORTS = ["plan", "Plan"]

STATUSES = ("todo", "doing", "done", "blocked")
SUMMARY_LINES = 10


class Plan:
    """An ordered list of steps with a status each. Ids are 1-based and stable."""

    def __init__(self):
        self.items: list[dict] = []
        self.objective: str = ""

    def set(self, texts: list[str], objective: str = "") -> "Plan":
        """Replace the whole plan. Use once, at the start."""
        self.items = [{"id": i, "text": str(text), "status": "todo", "note": ""}
                      for i, text in enumerate(texts, start=1)]
        if objective:
            self.objective = str(objective).strip()
        return self

    def add(self, text: str) -> int:
        """Append one step and return its id."""
        item_id = len(self.items) + 1
        self.items.append({"id": item_id, "text": str(text), "status": "todo", "note": ""})
        return item_id

    def update(self, item_id: int, status: str, note: str = "") -> "Plan":
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
        for item in self.items:
            if item["id"] == item_id:
                item["status"] = status
                if note:
                    item["note"] = note
                return self
        raise KeyError(f"no plan item {item_id}; ids are 1..{len(self.items)}")

    def show(self) -> Table:
        return Table(self.items, ["id", "status", "text", "note"], "plan", max_rows=60)

    def summary(self) -> str:
        """At most `SUMMARY_LINES` lines, for re-injection into the conversation."""
        if not self.items:
            return "plan is empty — call plan.set([...]) with the task's rules"
        done = [i for i in self.items if i["status"] == "done"]
        open_items = [i for i in self.items if i["status"] != "done"]
        lines = [f"{len(done)}/{len(self.items)} done"]
        if self.objective:
            lines.append(f"objective: {self.objective[:160]}")
        # SUMMARY_LINES caps the whole block, header and overflow note included: this text
        # is re-injected every ledger_k steps, so it has to stay a fixed small cost.
        room = SUMMARY_LINES - len(lines)
        shown = open_items if len(open_items) <= room else open_items[: room - 1]
        for item in shown:
            note = f" — {item['note']}" if item["note"] else ""
            lines.append(f"[{item['status']}] {item['id']}. {item['text']}{note}")
        hidden = len(open_items) - len(shown)
        if hidden > 0:
            lines.append(f"(+{hidden} more open)")
        return "\n".join(lines)

    def open_items(self) -> list[dict]:
        return [i for i in self.items if i["status"] != "done"]

    def is_complete(self) -> bool:
        return bool(self.items) and all(i["status"] == "done" for i in self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __str__(self) -> str:
        return str(self.show())


plan = Plan()
