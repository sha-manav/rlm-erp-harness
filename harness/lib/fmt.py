"""Tables capped at 40 rows and a page store for oversized tool output."""

from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

MAX_ROWS = 40
MAX_CELL = 60
PAGE_THRESHOLD = 4000
PAGE_SIZE = 3000

EXPORTS = ["Table", "PageStore", "pages", "show", "paginate"]


def _cell(value: Any) -> str:
    """Render one value, flattening the Odoo shapes that would otherwise be noise."""
    if value is None or value is False:
        # Odoo returns False for empty relations and empty char fields alike.
        return ""
    if value is True:
        return "yes"
    if isinstance(value, (list, tuple)):
        # Many2one reads come back as [id, "display name"].
        if len(value) == 2 and isinstance(value[0], int) and isinstance(value[1], str):
            return value[1]
        return ", ".join(_cell(item) for item in value)
    if isinstance(value, float):
        text = f"{value:,.2f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


class Table:
    """A list of dicts that prints as a fixed-width table and stays small."""

    def __init__(
        self,
        rows: Iterable[dict],
        cols: Sequence[str] | None = None,
        title: str = "",
        max_rows: int = MAX_ROWS,
    ):
        self.rows: list[dict] = [dict(row) for row in rows]
        if cols is not None:
            self.cols = list(cols)
        else:
            seen: dict[str, None] = {}
            for row in self.rows:
                for key in row:
                    seen.setdefault(key, None)
            self.cols = list(seen)
        self.title = title
        self.max_rows = max_rows

    # -- data access ----------------------------------------------------------
    def all(self) -> list[dict]:
        """Every row, unabridged."""
        return self.rows

    def df(self):
        """A pandas DataFrame when pandas is available, else a clear error."""
        try:
            import pandas
        except ImportError as exc:  # pragma: no cover - depends on the image
            raise RuntimeError(
                "pandas is not installed in this container; use .all() or "
                "pip3 install --break-system-packages pandas"
            ) from exc
        return pandas.DataFrame(self.rows, columns=self.cols)

    def column(self, name: str) -> list[Any]:
        return [row.get(name) for row in self.rows]

    def where(self, predicate: Callable[[dict], bool]) -> "Table":
        return Table([r for r in self.rows if predicate(r)], self.cols, self.title, self.max_rows)

    def sort(self, key: str, reverse: bool = False) -> "Table":
        ordered = sorted(self.rows, key=lambda row: (row.get(key) is None, row.get(key)), reverse=reverse)
        return Table(ordered, self.cols, self.title, self.max_rows)

    def total(self, column: str) -> float:
        return sum(float(row.get(column) or 0) for row in self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return Table(self.rows[index], self.cols, self.title, self.max_rows)
        return self.rows[index]

    def __bool__(self) -> bool:
        return bool(self.rows)

    # -- rendering ------------------------------------------------------------
    def __str__(self) -> str:
        if not self.rows:
            return f"{self.title} (0 rows)" if self.title else "(0 rows)"

        shown = self.rows[: self.max_rows]
        cells = [[_truncate(_cell(row.get(col)), MAX_CELL) for col in self.cols] for row in shown]
        widths = [
            max(len(str(col)), *(len(row[i]) for row in cells)) if cells else len(str(col))
            for i, col in enumerate(self.cols)
        ]

        lines = []
        if self.title:
            lines.append(self.title)
        lines.append("  ".join(str(col).ljust(widths[i]) for i, col in enumerate(self.cols)).rstrip())
        lines.append("  ".join("-" * widths[i] for i in range(len(self.cols))))
        for row in cells:
            lines.append("  ".join(row[i].ljust(widths[i]) for i in range(len(self.cols))).rstrip())

        hidden = len(self.rows) - len(shown)
        if hidden > 0:
            lines.append(f"… ({hidden} more rows; use .all() for every row)")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return self.__str__()


class PageStore:
    """Holds oversized tool output and hands it out a page at a time."""

    def __init__(self, threshold: int = PAGE_THRESHOLD, page_size: int = PAGE_SIZE,
                 hint: str | None = None):
        self.threshold = threshold
        self.page_size = page_size
        # What the truncation notice tells the reader to do next. The root loop has a
        # `show` tool; a sub-agent (python only) is told to print less instead.
        self.hint = hint
        self._store: dict[str, str] = {}
        self._counter = 0

    def capture(self, text: str) -> str:
        if text is None:
            return ""
        if len(text) <= self.threshold:
            return text
        self._counter += 1
        handle = f"h{self._counter}"
        self._store[handle] = text
        total = self.n_pages(handle)
        head = text[: self.page_size]
        hint = self.hint or f'show("{handle}", 2) for the next page'
        return (
            f"{head}\n"
            f"[truncated: showing page 1 of {total} ({len(text):,} chars). {hint}]"
        )

    def n_pages(self, handle: str) -> int:
        text = self._store.get(handle, "")
        return max(1, -(-len(text) // self.page_size))

    def show(self, handle: str, page: int = 1) -> str:
        if handle not in self._store:
            known = ", ".join(sorted(self._store)) or "none"
            return f"no such handle {handle!r} (known handles: {known})"
        total = self.n_pages(handle)
        if page < 1 or page > total:
            return f"{handle} has {total} page(s); asked for page {page}"
        text = self._store[handle]
        start = (page - 1) * self.page_size
        body = text[start : start + self.page_size]
        suffix = (
            f'\n[page {page} of {total}; show("{handle}", {page + 1}) for the next]'
            if page < total
            else f"\n[page {page} of {total}; end of output]"
        )
        return body + suffix

    def handles(self) -> list[str]:
        return sorted(self._store)


# A process-wide store so `show(...)` works from inside the kernel as well as from the
# harness loop's own `show` tool.
pages = PageStore()


def show(handle: str, page: int = 1) -> str:
    """Return one page of a stored oversized output."""
    return pages.show(handle, page)


def paginate(text: str) -> str:
    """Store `text` if it is oversized and return what the model should see."""
    return pages.capture(text)
