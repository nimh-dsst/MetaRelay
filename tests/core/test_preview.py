import pydantic
import pytest

from metarelay import Heading, Preview, Record, Table, register_renderer, render_preview
from metarelay.core import preview as preview_module
from metarelay.core.checksum import preview_checksum
from metarelay.core.preview import (
    PreviewDiff,
    PreviewEdit,
    diff_blocks,
    format_value,
    generic_blocks,
    parse_html,
    parse_markdown,
    preview_diff,
    to_html,
    to_markdown,
)


@pytest.fixture(autouse=True)
def isolated_renderers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preview_module, "_renderers", {})


@pytest.fixture
def nested_record(isolated_registry) -> Record:
    class Free(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="allow")

    isolated_registry.register("test.free", 1, Free)
    return Record(
        schema_id="test.free",
        schema_version=1,
        record_id="implant-M1",
        context={"cohort": "A", "subject_id": "M1", "notes": None},
        payload={
            "fiber": {"diameter_um": 400, "model": "MFC_400"},
            "sites": [{"region": "NAc", "hemisphere": "left"}],
            "indicators": ["GCaMP8m", "tdTomato"],
            "bilateral": False,
            "extras": {},
        },
    )


def test_generic_renderer_turns_nested_payload_into_rows(nested_record: Record) -> None:
    blocks = generic_blocks(nested_record)
    assert blocks[0] == Heading(text="test.free: implant-M1")
    assert blocks[1] == Table(
        title="Context", rows=[("cohort", "A"), ("subject_id", "M1"), ("notes", "—")]
    )
    assert blocks[2].rows == [
        ("fiber.diameter_um", "400"),
        ("fiber.model", "MFC_400"),
        ("sites[0].region", "NAc"),
        ("sites[0].hemisphere", "left"),
        ("indicators", "GCaMP8m, tdTomato"),
        ("bilateral", "No"),
        ("extras", "—"),
    ]


def test_html_is_tables_and_escaped() -> None:
    html = to_html([Heading(text="A & B"), Table(title="T", rows=[("depth", "<5 mm")])])
    assert html.startswith("<h3>A &amp; B</h3><h4>T</h4><table")
    assert "<th>depth</th><td>&lt;5 mm</td>" in html
    assert "{" not in html  # no raw JSON


def test_markdown_is_a_table_and_escaped() -> None:
    markdown = to_markdown([Heading(text="Implant"), Table(rows=[("a|b", "line1\nline2")])])
    assert markdown == ("## Implant\n\n| Field | Value |\n| --- | --- |\n| a\\|b | line1 line2 |\n")


def test_markdown_headings_and_titles_escape_inline_html() -> None:
    markdown = to_markdown([Heading(text="<img src=x>"), Table(title="<b>T</b>", rows=[])])
    assert "## &lt;img src=x&gt;" in markdown
    assert "### &lt;b&gt;T&lt;/b&gt;" in markdown
    assert "<" not in markdown


def test_markdown_cells_escape_inline_html() -> None:
    markdown = to_markdown([Table(rows=[("depth", "<5 mm"), ("note", "<b>x</b> & y")])])
    assert "| depth | &lt;5 mm |" in markdown
    assert "| note | &lt;b&gt;x&lt;/b&gt; &amp; y |" in markdown


def test_render_preview_uses_the_generic_renderer_by_default(make_record) -> None:
    preview = render_preview(make_record())
    assert isinstance(preview, Preview)
    assert "<td>400.0</td>" in preview.html
    assert "| diameter_um | 400.0 |" in preview.markdown


def test_a_schema_can_register_its_own_renderer(make_record) -> None:
    def fiber_blocks(record: Record) -> list[Heading | Table]:
        return [Table(title="Fiber", rows=[("Diameter", f"{record.payload['diameter_um']:g} µm")])]

    register_renderer("test.fiber", fiber_blocks)
    preview = render_preview(make_record())
    assert "<th>Diameter</th><td>400 µm</td>" in preview.html
    assert "diameter_um" not in preview.markdown


def test_preview_is_plain_data(make_record) -> None:
    preview = render_preview(make_record())
    assert Preview.model_validate_json(preview.model_dump_json()) == preview


def test_rendering_is_deterministic(make_record) -> None:
    record = make_record()
    assert preview_checksum(render_preview(record).html) == preview_checksum(
        render_preview(record).html
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "—"), ("", "—"), (True, "Yes"), (0, "0"), ([], "—"), ([1, None], "1, —")],
)
def test_format_value(value, expected: str) -> None:
    assert format_value(value) == expected


# Reading an edited preview back


def _edited(record: Record, old: str, new: str, fmt: str) -> str:
    rendered = render_preview(record)
    return (rendered.markdown if fmt == "markdown" else rendered.html).replace(old, new)


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_an_unedited_preview_has_no_edits(nested_record: Record, fmt: str) -> None:
    diff = preview_diff(nested_record, _edited(nested_record, "x", "x", fmt), format=fmt)
    assert diff == PreviewDiff(edits=[], complete=True)


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_a_changed_value_is_listed(nested_record: Record, fmt: str) -> None:
    diff = preview_diff(
        nested_record, _edited(nested_record, "MFC_400", "MFC_200", fmt), format=fmt
    )
    assert diff == PreviewDiff(
        edits=[
            PreviewEdit(
                record_id="implant-M1",
                table="Metadata",
                field="fiber.model",
                written="MFC_400",
                current="MFC_200",
            )
        ],
        complete=True,
    )


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_escaping_round_trips(isolated_registry, fmt: str) -> None:
    class Free(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="allow")

    isolated_registry.register("test.free", 1, Free)
    tricky = 'a | b \\ c <i>&amp;</i> "q"  spaced'
    record = Record(
        schema_id="test.free",
        schema_version=1,
        record_id="r",
        payload={"pipe|key": tricky, "back\\slash": "\\|", "x": "y"},
    )
    rendered = render_preview(record)
    text = rendered.markdown if fmt == "markdown" else rendered.html
    assert preview_diff(record, text, format=fmt) == PreviewDiff(edits=[], complete=True)
    edited = text.replace("| y |", "| z |") if fmt == "markdown" else text.replace(">y<", ">z<")
    [edit] = preview_diff(record, edited, format=fmt).edits
    assert (edit.field, edit.written, edit.current) == ("x", "y", "z")
    blocks, clean = (parse_markdown if fmt == "markdown" else parse_html)(text)
    assert clean
    assert ("pipe|key", " ".join(tricky.split())) in blocks[-1].rows
    assert ("back\\slash", "\\|") in blocks[-1].rows


def test_html_survives_a_notebook_cleaning_it_up(nested_record: Record) -> None:
    # eLabFTW and LabArchives strip <thead>/<tbody>, add wrappers and
    # attributes, and rewrite entities; values must still line up.
    html = render_preview(nested_record).html
    cleaned = (
        "<!-- metarelay-record:implant-M1 -->\n<div class='x'>"
        + html.replace("<thead>", "")
        .replace("</thead>", "")
        .replace("<tbody>", "")
        .replace("</tbody>", "")
        .replace("<td>", '<td style="color:red">\n  ')
        .replace("NAc", "<strong>NAc</strong>")
        + "</div>\n<p>&nbsp;</p>"
    )
    assert preview_diff(nested_record, cleaned, format="html") == PreviewDiff(
        edits=[], complete=True
    )


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_added_text_makes_the_diff_incomplete(nested_record: Record, fmt: str) -> None:
    text = _edited(nested_record, "MFC_400", "MFC_200", fmt)
    text += "\nThe fiber broke.\n" if fmt == "markdown" else "<p>The fiber broke.</p>"
    diff = preview_diff(nested_record, text, format=fmt)
    assert not diff.complete
    assert [edit.current for edit in diff.edits] == ["MFC_200"]
    assert "can't list" in diff.describe()


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_a_renamed_table_is_incomplete(nested_record: Record, fmt: str) -> None:
    diff = preview_diff(nested_record, _edited(nested_record, "Context", "Animal", fmt), format=fmt)
    assert not diff.complete
    assert {edit.table for edit in diff.edits} == {"Context", "Animal"}


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_a_changed_heading_is_incomplete(nested_record: Record, fmt: str) -> None:
    diff = preview_diff(
        nested_record, _edited(nested_record, "test.free: ", "Surgery ", fmt), format=fmt
    )
    assert diff == PreviewDiff(edits=[], complete=False)


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_free_text_lists_every_row_as_removed(nested_record: Record, fmt: str) -> None:
    diff = preview_diff(nested_record, "Edited by hand\n", format=fmt)
    assert not diff.complete
    assert diff.edits and all(edit.current is None for edit in diff.edits)


def test_added_and_removed_rows() -> None:
    written = [Table(title="T", rows=[("a", "1"), ("b", "2")])]
    current = [Table(title="T", rows=[("a", "1"), ("c", "3")])]
    assert diff_blocks("r", written, current) == PreviewDiff(
        edits=[
            PreviewEdit(record_id="r", table="T", field="b", written="2", current=None),
            PreviewEdit(record_id="r", table="T", field="c", written=None, current="3"),
        ],
        complete=True,
    )


def test_repeated_row_names_match_in_order() -> None:
    written = [Table(rows=[("site", "NAc"), ("site", "VTA")])]
    current = [Table(rows=[("site", "NAc"), ("site", "DMS")])]
    [edit] = diff_blocks("r", written, current).edits
    assert (edit.written, edit.current) == ("VTA", "DMS")
    current = [Table(rows=[("site", "DMS"), ("site", "VTA")])]
    [edit] = diff_blocks("r", written, current).edits
    assert (edit.written, edit.current) == ("NAc", "DMS")


def test_nothing_to_compare_is_none(nested_record: Record) -> None:
    assert preview_diff(None, "x", format="markdown") is None
    assert preview_diff(nested_record, None, format="html") is None
    assert preview_diff(b"not json", "x", format="markdown") is None
    data = nested_record.model_dump_json().encode()
    assert preview_diff(data, render_preview(nested_record).markdown, format="markdown") == (
        PreviewDiff(edits=[], complete=True)
    )


def test_describe() -> None:
    edit = PreviewEdit(record_id="r", table="T", field="a", written="1", current="2")
    assert PreviewDiff(edits=[edit], complete=True).describe() == "Changed: T · a: '1' → '2'."
    assert PreviewDiff(edits=[edit] * 7, complete=True).describe().endswith("; and 2 more.")
    assert "formatting" in PreviewDiff(edits=[], complete=True).describe()
    assert "can't tell" in PreviewDiff(edits=[], complete=False).describe()
    added = edit.model_copy(update={"written": None, "table": None})
    removed = edit.model_copy(update={"current": None})
    assert added.describe() == "a: added '2'"
    assert removed.describe() == "T · a: removed (was '1')"


def test_an_inserted_row_does_not_shift_the_others() -> None:
    written = [Table(title="T", rows=[("a", "1"), ("b", "2")])]
    current = [Table(title="T", rows=[("new", "0"), ("a", "1"), ("b", "2")])]
    assert diff_blocks("r", written, current).edits == [
        PreviewEdit(record_id="r", table="T", field="new", written=None, current="0")
    ]


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_a_row_named_like_the_header_is_still_a_row(isolated_registry, fmt: str) -> None:
    class Free(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="allow")

    isolated_registry.register("test.free", 1, Free)
    record = Record(
        schema_id="test.free", schema_version=1, record_id="r", payload={"Field": "Value"}
    )
    rendered = render_preview(record)
    text = rendered.markdown if fmt == "markdown" else rendered.html
    if fmt == "markdown":
        # Only the row, not the header above it.
        head, _, rows = text.partition("| --- | --- |")
        edited = head + "| --- | --- |" + rows.replace("Value", "Other")
    else:
        head, _, rows = text.partition("</thead>")
        edited = head + "</thead>" + rows.replace("Value", "Other")
    [edit] = preview_diff(record, edited, format=fmt).edits
    assert (edit.field, edit.written, edit.current) == ("Field", "Value", "Other")


def test_a_stray_markdown_table_line_is_not_clean(nested_record: Record) -> None:
    text = render_preview(nested_record).markdown + "\n| a note |\n"
    assert not preview_diff(nested_record, text, format="markdown").complete


def test_html_line_breaks_and_unclosed_cells(nested_record: Record) -> None:
    html = render_preview(nested_record).html
    # A value someone split over two lines reads as one value with a space.
    split = html.replace("<td>GCaMP8m, tdTomato</td>", "<td>GCaMP8m,<br>tdTomato</td>")
    assert preview_diff(nested_record, split, format="html") == PreviewDiff(edits=[], complete=True)
    # Pasted HTML without </td>: the first cell is not lost.
    unclosed = html.replace("<th>cohort</th><td>A</td>", "<th>cohort<td>B")
    [edit] = preview_diff(nested_record, unclosed, format="html").edits
    assert (edit.field, edit.written, edit.current) == ("cohort", "A", "B")


def test_an_added_image_is_not_complete(nested_record: Record) -> None:
    html = render_preview(nested_record).html + '<img src="photo.png">'
    assert preview_diff(nested_record, html, format="html") == PreviewDiff(edits=[], complete=False)


def test_long_values_are_cut_short_in_messages() -> None:
    edit = PreviewEdit(record_id="r", table=None, field="notes", written="a", current="x" * 500)
    assert len(edit.describe()) < 100
    assert edit.describe().endswith("…'")
