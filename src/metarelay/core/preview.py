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
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from html import escape
from typing import Annotated, Any, Literal

import pydantic
from pydantic import ConfigDict, Field

from metarelay.core.record import Record

__all__ = [
    "Block",
    "Heading",
    "Preview",
    "Renderer",
    "Table",
    "generic_blocks",
    "register_renderer",
    "render_preview",
    "to_html",
    "to_markdown",
]

EMPTY = "—"


class Heading(pydantic.BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["heading"] = "heading"
    text: str


class Table(pydantic.BaseModel):
    """A two-column field/value table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["table"] = "table"
    title: str | None = None
    rows: list[tuple[str, str]]


Block = Annotated[Heading | Table, Field(discriminator="kind")]
Renderer = Callable[[Record], list[Heading | Table]]


class Preview(pydantic.BaseModel):
    """A record's readable view, in both formats."""

    model_config = ConfigDict(frozen=True)

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
            parts.append(f"## {_md_text(block.text)}")
        else:
            lines = []
            if block.title:
                lines += [f"### {_md_text(block.title)}", ""]
            lines += ["| Field | Value |", "| --- | --- |"]
            lines += [f"| {_md_cell(field)} | {_md_cell(value)} |" for field, value in block.rows]
            parts.append("\n".join(lines))
    return "\n\n".join(parts) + "\n"


def _md_text(text: str) -> str:
    return " ".join(text.split())


def _md_cell(text: str) -> str:
    # GitHub renders inline HTML in Markdown, so escape it like the HTML output.
    return _md_text(escape(text.replace("\\", "\\\\").replace("|", "\\|"), quote=False))
