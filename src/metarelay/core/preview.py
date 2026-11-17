"""Readable previews: the tables a person sees in the notebook entry.

DSST's senior users said trainees will not read raw JSON (#15), so every
record gets a table view, and raw JSON is never the only thing a reader sees.

A preview is built in two steps:

1. A renderer turns a record into **blocks**, which describe *what* to show
   (headings and tables). Each schema can register its own renderer. Records
   without one get the generic renderer, which turns the payload into a
   field/value table.
2. Formatters turn blocks into HTML (for LabArchives and eLabFTW) and Markdown
   (for local and GitHub ``.md`` files).

Backends never build preview markup themselves. They call ``render_preview``
and store the result. Visual rendering (diagrams, device layouts) is a later
stretch goal: it would be a new block type here, and no renderer or backend
would need to change.

Renderers live in their own registry here, keyed by schema ID, separate from
the schema registry. #8 may merge the two when real schemas arrive.

**Reading an edited preview back.** When someone edits a preview in the
notebook, MetaRelay refuses the next write (D7). ``preview_diff`` says what
changed, so the person can decide whether to keep it: it renders the record
MetaRelay last wrote, reads both that and the current preview back into
blocks with the same parser (``parse_markdown`` or ``parse_html``), and
compares them row by row. The JSON stays the source of truth: nothing here
changes a record by itself. When a person accepts the edits, ``apply_edits``
copies them into the record, so the JSON and the preview agree again.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime
from html import escape, unescape
from html.parser import HTMLParser
from typing import Annotated, Any, Literal

from pydantic import Field

from metarelay.core.errors import ValidationError
from metarelay.core.model import MetaRelayModel
from metarelay.core.record import Record
from metarelay.core.registry import SchemaRegistry

__all__ = [
    "Block",
    "Heading",
    "Preview",
    "PreviewDiff",
    "PreviewEdit",
    "Renderer",
    "Table",
    "apply_edits",
    "best_effort",
    "diff_blocks",
    "generic_blocks",
    "parse_html",
    "parse_markdown",
    "preview_diff",
    "register_renderer",
    "render_preview",
    "to_html",
    "to_markdown",
]

EMPTY = "—"


class Heading(MetaRelayModel):
    kind: Literal["heading"] = "heading"
    text: str


class Table(MetaRelayModel):
    """A two-column field/value table."""

    kind: Literal["table"] = "table"
    title: str | None = None
    rows: list[tuple[str, str]]


Block = Annotated[Heading | Table, Field(discriminator="kind")]
Renderer = Callable[[Record], list[Heading | Table]]


class Preview(MetaRelayModel):
    """A record's readable view, in both formats."""

    blocks: list[Block]
    html: str
    markdown: str


_renderers: dict[str, Renderer] = {}


def register_renderer(schema_id: str, renderer: Renderer) -> None:
    """Use ``renderer`` for every version of ``schema_id``."""
    _renderers[schema_id] = renderer


def render_preview(record: Record) -> Preview:
    """Render ``record`` with its schema's renderer, or the generic one."""
    renderer = _renderers.get(record.schema_id, generic_blocks)
    blocks = renderer(record)
    return Preview(blocks=blocks, html=to_html(blocks), markdown=to_markdown(blocks))


# The generic renderer


def generic_blocks(record: Record) -> list[Heading | Table]:
    """A heading, a context table, and every payload field as a table row."""
    blocks: list[Heading | Table] = [Heading(text=f"{record.schema_id}: {record.record_id}")]
    if record.context:
        blocks.append(
            Table(
                title="Context",
                rows=[(key, format_value(value)) for key, value in record.context.items()],
            )
        )
    blocks.append(Table(title="Metadata", rows=list(_flatten(record.payload)) or [("", EMPTY)]))
    return blocks


def format_value(value: Any) -> str:
    """How one value reads in a table cell."""
    if value is None or value == "":
        return EMPTY
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, Sequence) and not isinstance(value, str):
        return ", ".join(format_value(item) for item in value) or EMPTY
    return str(value)


def _flatten(value: Any, prefix: str = "") -> Iterator[tuple[str, str]]:
    """Turn nested data into ``("fiber.diameter_um", "400")`` rows."""
    if isinstance(value, Mapping):
        if not value and prefix:
            yield prefix, EMPTY
        for key, item in value.items():
            yield from _flatten(item, f"{prefix}.{key}" if prefix else str(key))
    elif (
        isinstance(value, Sequence)
        and not isinstance(value, str)
        and any(isinstance(item, Mapping | list) for item in value)
    ):
        for index, item in enumerate(value):
            yield from _flatten(item, f"{prefix}[{index}]")
    else:
        yield prefix, format_value(value)


# Formatters


def to_html(blocks: Sequence[Heading | Table]) -> str:
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, Heading):
            parts.append(f"<h3>{escape(block.text)}</h3>")
        else:
            if block.title:
                parts.append(f"<h4>{escape(block.title)}</h4>")
            rows = "".join(
                f"<tr><th>{escape(field)}</th><td>{escape(value)}</td></tr>"
                for field, value in block.rows
            )
            parts.append(
                '<table border="1"><thead><tr><th>Field</th><th>Value</th></tr></thead>'
                f"<tbody>{rows}</tbody></table>"
            )
    return "".join(parts)


def to_markdown(blocks: Sequence[Heading | Table]) -> str:
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, Heading):
            parts.append(f"## {_squash(_md_escape(block.text))}")
        else:
            lines = []
            if block.title:
                lines += [f"### {_squash(_md_escape(block.title))}", ""]
            lines += ["| Field | Value |", "| --- | --- |"]
            lines += [f"| {_md_cell(field)} | {_md_cell(value)} |" for field, value in block.rows]
            parts.append("\n".join(lines))
    return "\n\n".join(parts) + "\n"


def _squash(text: str) -> str:
    """Collapse whitespace, as Markdown cells and parsed values both need."""
    return " ".join(text.split())


def _md_escape(text: str) -> str:
    # GitHub renders inline HTML in Markdown, so escape it like the HTML output.
    return escape(text, quote=False)


def _md_cell(text: str) -> str:
    return _squash(_md_escape(text.replace("\\", "\\\\").replace("|", "\\|")))


# Reading an edited preview back


class PreviewEdit(MetaRelayModel):
    """One table row that differs from what MetaRelay wrote."""

    record_id: str
    table: str | None
    """The table's title, or ``None`` for an untitled table."""
    field: str
    written: str | None
    """The value as MetaRelay wrote it, or ``None`` if the row was added by hand."""
    current: str | None
    """The value in the notebook now, or ``None`` if the row was removed."""

    def describe(self, *, name_record: bool = False) -> str:
        """The change, for a message. ``name_record`` says which record it's in."""
        where = f"{self.table} · {self.field}" if self.table else self.field
        if name_record:
            where += f" of {self.record_id}"
        if self.written is None:
            return f"{where}: added {_short(self.current)}"
        if self.current is None:
            return f"{where}: removed (was {_short(self.written)})"
        return f"{where}: {_short(self.written)} → {_short(self.current)}"


def _short(value: str | None, limit: int = 60) -> str:
    """A value for a message: quoted, and cut short if it is long."""
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 2] + "…'"


class PreviewDiff(MetaRelayModel):
    """What changed in a preview someone edited in the notebook (D7)."""

    edits: list[PreviewEdit]
    complete: bool
    """``True`` when ``edits`` is every change: the rest of the preview (its
    headings and table titles) still reads as MetaRelay wrote it. ``False``
    when something else changed that MetaRelay can't list, such as text added
    between the tables or a table renamed."""

    def describe(self, limit: int = 5, *, writing: str | None = None) -> str:
        """A sentence for an error message.

        ``writing`` is the ID of the record being written. Edits to other
        records (in an eLabFTW entry that holds several) name their record,
        so the message doesn't read as a change to the one being written.
        """
        if not self.edits:
            if self.complete:
                return "Only formatting changed, no values."
            return "MetaRelay can't tell what changed."
        listed = "; ".join(
            edit.describe(name_record=writing is not None and edit.record_id != writing)
            for edit in self.edits[:limit]
        )
        more = len(self.edits) - limit
        text = f"Changed: {listed}" + (f"; and {more} more" if more > 0 else "") + "."
        if not self.complete:
            text += " Other changes MetaRelay can't list were made too."
        return text


def best_effort(explain: Callable[[], PreviewDiff | None]) -> PreviewDiff | None:
    """Run ``explain``, or return ``None`` if it fails.

    The diff only explains a conflict. A failure working it out (a renderer
    that raises on an old record, odd data at the destination) must not
    replace the ``ConflictError`` the caller knows how to handle.
    """
    try:
        return explain()
    except Exception:
        return None


def preview_diff(
    written: Record | bytes | None,
    current: str | None,
    *,
    format: Literal["markdown", "html"],
) -> PreviewDiff | None:
    """Compare the preview of ``written`` with ``current``, a preview read back.

    ``written`` is the record as MetaRelay last wrote it: a ``Record``, or the
    stored JSON bytes. Both sides go through the same parser, so escaping and
    whitespace never show up as an edit. Returns ``None`` when either side is
    missing or the stored JSON is unreadable: there is nothing to compare.
    """
    if isinstance(written, bytes):
        try:
            written = Record.model_validate_json(written)
        except ValueError:
            return None
    if written is None or current is None:
        return None
    blocks = render_preview(written).blocks
    if format == "markdown":
        expected, _ = parse_markdown(to_markdown(blocks))
        found, clean = parse_markdown(current)
    else:
        expected, _ = parse_html(to_html(blocks))
        found, clean = parse_html(current)
    return diff_blocks(written.record_id, expected, found, clean=clean)


def diff_blocks(
    record_id: str,
    written: Sequence[Heading | Table],
    current: Sequence[Heading | Table],
    *,
    clean: bool = True,
) -> PreviewDiff:
    """Compare two previews row by row.

    Rows are matched by table title and field name (the second row with the
    same name matches the second one). ``clean`` is ``False`` when the parser
    found content outside headings and tables in ``current``.
    """
    old, new = _rows(written), _rows(current)
    edits = [
        PreviewEdit(
            record_id=record_id, table=key[0], field=key[1], written=value, current=new.get(key)
        )
        for key, value in old.items()
        if new.get(key) != value
    ]
    edits += [
        PreviewEdit(record_id=record_id, table=key[0], field=key[1], written=None, current=value)
        for key, value in new.items()
        if key not in old
    ]
    complete = clean and _outline(written) == _outline(current)
    return PreviewDiff(edits=edits, complete=complete)


def _rows(blocks: Sequence[Heading | Table]) -> dict[tuple[str | None, str, int], str]:
    rows: dict[tuple[str | None, str, int], str] = {}
    seen: Counter[tuple[str | None, str]] = Counter()
    for block in blocks:
        if isinstance(block, Table):
            for field, value in block.rows:
                rows[(block.title, field, seen[(block.title, field)])] = value
                seen[(block.title, field)] += 1
    return rows


def _outline(blocks: Sequence[Heading | Table]) -> list[tuple[str, str | None]]:
    """Headings and table titles, in order: what an edit to values leaves alone."""
    return [
        ("heading", block.text) if isinstance(block, Heading) else ("table", block.title)
        for block in blocks
    ]


def parse_markdown(text: str) -> tuple[list[Heading | Table], bool]:
    """Read a Markdown preview back into blocks.

    Returns the blocks and whether every line was a heading, a table line, or
    blank (``False`` means someone added other content).
    """
    blocks: list[Heading | Table] = []
    clean = True
    title: str | None = None
    table: Table | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            table = None
            continue
        if stripped.startswith("|"):
            cells = _md_cells(stripped)
            if table is None:
                if cells == ["Field", "Value"]:
                    table = Table(title=title, rows=[])
                    blocks.append(table)
                    title = None
                    continue
                clean = False
                continue
            if all(set(cell) <= {"-", ":"} and cell for cell in cells) and not table.rows:
                continue
            if len(cells) != 2:
                clean = False
                continue
            table.rows.append((cells[0], cells[1]))
            continue
        table = None
        if stripped.startswith("### "):
            if title is not None:
                clean = False
            title = _squash(unescape(stripped[4:]))
        elif stripped.startswith("## "):
            blocks.append(Heading(text=_squash(unescape(stripped[3:]))))
        else:
            clean = False
    if title is not None:
        clean = False
    return blocks, clean


def _md_cells(line: str) -> list[str]:
    """Split a table line on unescaped pipes and undo ``_md_cell``'s escaping."""
    cells: list[str] = []
    cell: list[str] = []
    escaped = False
    for char in line.strip()[1:]:
        if escaped:
            cell.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(cell))
            cell = []
        else:
            cell.append(char)
    if cell and "".join(cell).strip():
        cells.append("".join(cell))
    return [_squash(unescape(cell)) for cell in cells]


def parse_html(text: str) -> tuple[list[Heading | Table], bool]:
    """Read an HTML preview back into blocks, best effort.

    Notebooks clean up HTML they store (LabArchives and eLabFTW both do), so
    this looks only at text: ``h3`` headings, ``h4`` table titles, and table
    rows of two cells. Returns the blocks and whether all other text was
    whitespace (``False`` means someone added other content).
    """
    parser = _PreviewHTML()
    parser.feed(text)
    parser.close()
    return parser.blocks, parser.clean


_BREAK_TAGS = frozenset({"br", "p", "div", "li"})
"""Tags that separate words inside a cell."""
_CONTENT_TAGS = frozenset({"img", "video", "audio", "iframe", "object", "embed", "svg", "canvas"})


class _PreviewHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Heading | Table] = []
        self.clean = True
        self._title: str | None = None
        self._table: Table | None = None
        self._text: list[str] | None = None  # inside h3, h4, th or td
        self._row: list[str] | None = None
        self._header = False  # the current table's Field/Value row was seen

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _CONTENT_TAGS:
            self.clean = False  # an image or embed: content that has no text
        if tag in {"th", "td"} and self._text is not None and self._row is not None:
            self._row.append(_squash("".join(self._text)))  # an unclosed cell
        if tag in {"h3", "h4", "th", "td"}:
            self._text = []
        elif self._text is not None and tag in _BREAK_TAGS:
            self._text.append(" ")
        elif tag == "table":
            self._table = Table(title=self._title, rows=[])
            self._title = None
        elif tag == "tr":
            self._row = []

    def handle_endtag(self, tag: str) -> None:
        text = _squash("".join(self._text)) if self._text is not None else ""
        if tag == "h3" and self._text is not None:
            self.blocks.append(Heading(text=text))
            self._text = None
        elif tag == "h4" and self._text is not None:
            if self._title is not None:
                self.clean = False
            self._title = text
            self._text = None
        elif tag in {"th", "td"} and self._text is not None:
            if self._row is not None:
                self._row.append(text)
            self._text = None
        elif tag == "tr" and self._row is not None:
            if self._text is not None:  # the row's last cell was never closed
                self._row.append(text)
                self._text = None
            if self._table is None:
                self.clean = False
            elif not self._table.rows and self._row == ["Field", "Value"] and not self._header:
                self._header = True
            elif len(self._row) == 2:
                self._table.rows.append((self._row[0], self._row[1]))
            else:
                self.clean = False
            self._row = None
        elif tag == "table" and self._table is not None:
            self.blocks.append(self._table)
            self._table = None
            self._header = False

    def handle_data(self, data: str) -> None:
        if self._text is not None:
            self._text.append(data)
        elif data.strip():
            self.clean = False

    def close(self) -> None:
        super().close()
        if self._title is not None or self._table is not None:
            self.clean = False


# Accepting edits into the record

_PATH_PART = re.compile(r"\[(\d+)\]|([^.\[\]]+)")


def apply_edits(
    record: Record,
    edits: Sequence[PreviewEdit],
    *,
    registry: SchemaRegistry | None = None,
) -> Record:
    """Copy edits a person accepted from the preview into ``record``.

    Each edit's new value goes back where the generic renderer showed it: a
    ``Context`` row into ``context``, a ``Metadata`` row (``fiber.core_um``,
    ``sites[0].region``) into ``payload``, converted to the type of the value
    it replaces ("200" for a number, "Yes"/"No" for a yes/no value, "a, b"
    for a list). A value that was empty stays text, and the schema converts
    it (a number field takes "200"). The result is validated like any record,
    against ``registry`` if given, and must show the accepted values in its
    preview.

    All or nothing: if any edit can't be applied, nothing is. To accept the
    edits in a ``ConflictError`` and write them, use ``accept_edits``, which
    also handles the other records of an eLabFTW entry.

    Raises:
        ValidationError: If an edit is for another record, adds or removes a
            row, no longer matches the record (it changed since), names a row
            shown twice, can't be converted, or the schema rejects the
            result. Also for records with their own renderer, whose rows
            MetaRelay can't map back.
    """
    if record.schema_id in _renderers:
        raise ValidationError(
            f"Edits to {record.record_id!r} can't be applied automatically: its schema "
            f"{record.schema_id!r} has its own preview, whose rows MetaRelay can't map "
            "back to the record. Copy them into the record by hand."
        )
    shown = _rows(generic_blocks(record))
    context = dict(record.context)
    payload = copy.deepcopy(record.payload)
    problems: list[str] = []
    for edit in edits:
        where = edit.describe()
        if edit.record_id != record.record_id:
            problems.append(f"{where}: it is for record {edit.record_id!r}")
            continue
        if edit.written is None or edit.current is None:
            problems.append(f"{where}: adding or removing a row isn't supported")
            continue
        if (edit.table, edit.field, 1) in shown:
            problems.append(f"{where}: two rows have that name, so MetaRelay can't tell which")
            continue
        now = shown.get((edit.table, edit.field, 0))
        if now is None or _squash(now) != _typed(edit.written):
            problems.append(f"{where}: the record no longer shows {_short(edit.written)} there")
            continue
        current = _typed(edit.current)
        try:
            if edit.table == "Context":
                context[edit.field] = None if current == EMPTY else current
            elif edit.table == "Metadata":
                _set_shown_value(payload, edit.field, current)
            else:
                raise ValueError("it isn't in a table MetaRelay can map back")
        except (ValueError, LookupError, TypeError) as error:
            problems.append(f"{where}: {error}")
    if not problems:
        data = {
            **record.model_dump(),
            "context": context,
            "payload": payload,
            "updated_at": datetime.now().astimezone(),
        }
        try:
            if registry is None:
                updated = Record.model_validate(data)
            else:
                updated = Record.model_validate(data, context={"registry": registry})
        except ValidationError as error:
            message = f"The edited {record.record_id!r} is not valid: {error}"
            raise ValidationError(message) from None
        after = _rows(generic_blocks(updated))
        problems = [
            f"{edit.describe()}: the record would show "
            f"{_short(after.get((edit.table, edit.field, 0)))} instead"
            for edit in edits
            if not _same_shown(after.get((edit.table, edit.field, 0)), edit.current)
        ]
    if problems:
        raise ValidationError(
            f"Can't apply the edits to {record.record_id!r}, so none were: " + "; ".join(problems)
        )
    return updated


_ANSWERS = {"yes": True, "true": True, "no": False, "false": False}
"""What a person may type for a yes/no value. The preview shows Yes or No."""


def _typed(text: str) -> str:
    """A value as typed, the way the preview shows it: blank is EMPTY.

    Values read from Markdown or HTML already look like this. eLabFTW's
    fields give the raw text, where a cleared field is "".
    """
    return _squash(text) or EMPTY


def _answer(text: str) -> bool | None:
    return _ANSWERS.get(text.strip().lower())


def _same_shown(shown: str | None, typed: str | None) -> bool:
    """Whether the preview shows what the person typed (``250`` reads as ``250.0``)."""
    if shown is None or typed is None:
        return shown is typed
    shown, typed = _squash(shown), _typed(typed)
    if shown == typed:
        return True
    if _answer(shown) is not None and _answer(typed) is not None:
        return _answer(shown) == _answer(typed)
    try:
        return float(shown) == float(typed)
    except ValueError:
        return False


def _set_shown_value(payload: dict[str, Any], field: str, text: str) -> None:
    """Set the payload value shown as row ``field`` from its shown ``text``."""
    parts = [int(index) if index else key for index, key in _PATH_PART.findall(field)]
    if not parts:
        raise ValueError("it doesn't name a payload value")
    parent: Any = payload
    for part in parts[:-1]:
        parent = parent[part]
    last = parts[-1]
    parent[last] = _from_shown(parent[last], text)


def _from_shown(old: Any, text: str) -> Any:
    """Turn ``text``, as a person typed it in a table cell, into a value like ``old``."""
    if isinstance(old, Mapping):
        raise ValueError("an empty group can't take a value")
    if text == EMPTY:
        if isinstance(old, list):
            return []
        return "" if isinstance(old, str) else None
    if isinstance(old, bool):
        answer = _answer(text)
        if answer is None:
            raise ValueError(f"{_short(text)} isn't Yes or No")
        return answer
    if isinstance(old, int | float):
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            raise ValueError(f"{_short(text)} isn't a number") from None
    if isinstance(old, list):
        if any(isinstance(item, str) and ", " in item for item in old):
            raise ValueError("its list has items containing ', ', so it can't be split")
        sample = old[0] if old else ""
        return [_from_shown(sample, item) for item in text.split(", ")]
    return text
