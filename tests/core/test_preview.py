import pydantic
import pytest

from metarelay import Heading, Preview, Record, Table, register_renderer, render_preview
from metarelay.core import preview as preview_module
from metarelay.core.checksum import preview_checksum
from metarelay.core.errors import ValidationError
from metarelay.core.preview import (
    PreviewDiff,
    PreviewEdit,
    apply_edits,
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
    other = edit.model_copy(update={"record_id": "s"})
    both = PreviewDiff(edits=[edit, other], complete=True)
    assert both.describe(writing="r") == "Changed: T · a: '1' → '2'; T · a of s: '1' → '2'."
    assert both.describe() == "Changed: T · a: '1' → '2'; T · a: '1' → '2'."


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


# Accepting edits into the record


@pytest.fixture
def free(isolated_registry):
    class Free(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="allow")

    class Checked(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="forbid")

        diameter_um: float = pydantic.Field(gt=0)

    isolated_registry.register("test.free", 1, Free)
    isolated_registry.register("test.checked", 1, Checked)


def _accepted(record: Record, *changes: tuple[str, str, str]) -> Record:
    """Edit rows in the record's Markdown preview, read the diff, and apply it."""
    text = render_preview(record).markdown
    for table, field, value in changes:
        rows = dict(
            next(
                b for b in render_preview(record).blocks if getattr(b, "title", None) == table
            ).rows
        )
        text = text.replace(f"| {field} | {rows[field]} |", f"| {field} | {value} |")
    diff = preview_diff(record, text, format="markdown")
    assert diff.complete and len(diff.edits) == len(changes)
    return apply_edits(record, diff.edits)


def test_apply_edits_puts_values_back_with_their_types(nested_record: Record) -> None:
    updated = _accepted(
        nested_record,
        ("Context", "cohort", "B"),
        ("Context", "notes", "moved cage"),
        ("Metadata", "fiber.diameter_um", "200"),
        ("Metadata", "sites[0].region", "VTA"),
        ("Metadata", "indicators", "GCaMP8s, tdTomato"),
        ("Metadata", "bilateral", "Yes"),
    )
    assert updated.context == {"cohort": "B", "subject_id": "M1", "notes": "moved cage"}
    assert updated.payload["fiber"] == {"diameter_um": 200, "model": "MFC_400"}
    assert updated.payload["sites"] == [{"region": "VTA", "hemisphere": "left"}]
    assert updated.payload["indicators"] == ["GCaMP8s", "tdTomato"]
    assert updated.payload["bilateral"] is True
    assert updated.updated_at > nested_record.updated_at
    # The JSON and the preview agree again.
    assert preview_diff(updated, render_preview(updated).markdown, format="markdown").edits == []


def test_apply_edits_reads_empty_and_decimal_values(free) -> None:
    record = Record(
        schema_id="test.free",
        schema_version=1,
        record_id="r",
        context={"cohort": "A"},
        payload={"count": 3, "na": 0.39, "note": "x"},
    )
    updated = _accepted(
        record,
        ("Context", "cohort", "—"),
        ("Metadata", "count", "3.5"),
        ("Metadata", "na", "0.5"),
        ("Metadata", "note", "—"),
    )
    assert updated.context == {"cohort": None}
    assert updated.payload == {"count": 3.5, "na": 0.5, "note": ""}


def test_the_schema_checks_the_result(free) -> None:
    record = Record(
        schema_id="test.checked", schema_version=1, record_id="r", payload={"diameter_um": 4}
    )
    with pytest.raises(ValidationError, match="not valid"):
        _accepted(record, ("Metadata", "diameter_um", "-1"))


def _edit(field: str, written: str | None, current: str | None, **overrides) -> PreviewEdit:
    values = {"record_id": "implant-M1", "table": "Metadata", "field": field}
    return PreviewEdit(**{**values, **overrides}, written=written, current=current)


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (_edit("fiber.model", "MFC_400", "x", record_id="other"), "is for record 'other'"),
        (_edit("new", None, "x"), "adding or removing"),
        (_edit("fiber.model", "MFC_400", None), "adding or removing"),
        (_edit("fiber.model", "MFC_200", "x"), "no longer shows"),
        (_edit("fiber.diameter_um", "400", "wide"), "isn't a number"),
        (_edit("bilateral", "No", "maybe"), "isn't Yes or No"),
        (_edit("extras", "—", "x"), "empty group"),
        (_edit("cohort", "A", "B", table="Animal"), "no longer shows"),
    ],
)
def test_edits_that_cant_be_applied_change_nothing(nested_record, edit, reason) -> None:
    fine = _edit("fiber.model", "MFC_400", "MFC_200")
    with pytest.raises(ValidationError, match=reason) as raised:
        apply_edits(nested_record, [fine, edit])
    assert "none were" in str(raised.value)
    assert nested_record.payload["fiber"]["model"] == "MFC_400"


def test_a_list_with_commas_in_its_items_cant_be_split(free) -> None:
    record = Record(
        schema_id="test.free", schema_version=1, record_id="r", payload={"tags": ["a, b"]}
    )
    with pytest.raises(ValidationError, match="can't be split"):
        apply_edits(record, [_edit("tags", "a, b", "a, c", record_id="r")])


def test_a_key_the_path_cant_name_is_refused(free) -> None:
    # "a.b" as one key renders as row "a.b", which also reads as a nested path.
    record = Record(schema_id="test.free", schema_version=1, record_id="r", payload={"a.b": 1})
    with pytest.raises(ValidationError, match="none were"):
        apply_edits(record, [_edit("a.b", "1", "2", record_id="r")])


def test_a_custom_preview_cant_be_mapped_back(nested_record: Record) -> None:
    register_renderer("test.free", lambda record: [Table(title="Mine", rows=[("x", "1")])])
    with pytest.raises(ValidationError, match="its own preview"):
        apply_edits(nested_record, [_edit("x", "1", "2", table="Mine")])


def test_typed_yes_and_numbers_read_as_shown(nested_record: Record) -> None:
    updated = _accepted(nested_record, ("Metadata", "bilateral", "yes"))
    assert updated.payload["bilateral"] is True
    updated = _accepted(nested_record, ("Metadata", "bilateral", "TRUE"))
    assert updated.payload["bilateral"] is True


def test_a_schema_that_rewrites_the_value_is_refused(isolated_registry) -> None:
    # The person typed "vta"; the schema stores "VTA". The record would not
    # say what they accepted, so nothing is applied.
    class Upper(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="forbid")

        target: str

        @pydantic.field_validator("target")
        @classmethod
        def _upper(cls, value: str) -> str:
            return value.upper()

    isolated_registry.register("test.upper", 1, Upper)
    record = Record(
        schema_id="test.upper", schema_version=1, record_id="r", payload={"target": "NAC"}
    )
    with pytest.raises(ValidationError, match="would show 'VTA' instead"):
        _accepted(record, ("Metadata", "target", "vta"))


def test_a_filled_in_empty_value_is_converted_by_the_schema(isolated_registry) -> None:
    class Optional_(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="forbid")

        core_um: float | None = None
        bilateral: bool | None = None

    isolated_registry.register("test.optional", 1, Optional_)
    record = Record(
        schema_id="test.optional",
        schema_version=1,
        record_id="r",
        payload={"core_um": None, "bilateral": None},
    )
    updated = _accepted(record, ("Metadata", "core_um", "200"), ("Metadata", "bilateral", "yes"))
    assert updated.payload == {"core_um": 200.0, "bilateral": True}


def test_raw_field_values_are_read_like_the_preview(free) -> None:
    # eLabFTW's fields give raw text: spaces aren't collapsed, and a cleared
    # field is "" rather than "—".
    record = Record(
        schema_id="test.free",
        schema_version=1,
        record_id="r",
        context={"cohort": "A"},
        payload={"note": "two  spaces", "count": 3, "tags": ["a", "b"]},
    )
    edits = [
        _edit("note", "two  spaces", "one", record_id="r"),
        _edit("count", "3", "", record_id="r"),
        _edit("tags", "a, b", "", record_id="r"),
        _edit("cohort", "A", "", record_id="r", table="Context"),
    ]
    updated = apply_edits(record, edits)
    assert updated.payload == {"note": "one", "count": None, "tags": []}
    assert updated.context == {"cohort": None}


def test_a_row_shown_twice_is_refused(free) -> None:
    record = Record(
        schema_id="test.free", schema_version=1, record_id="r", payload={"a.b": 1, "a": {"b": 2}}
    )
    with pytest.raises(ValidationError, match="two rows have that name"):
        apply_edits(record, [_edit("a.b", "1", "3", record_id="r")])


def test_edits_validate_against_the_given_registry() -> None:
    from metarelay.core.registry import SchemaRegistry

    class Own(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="forbid")

        size: float

    mine = SchemaRegistry()
    mine.register("test.own", 1, Own)
    data = {"schema_id": "test.own", "schema_version": 1, "record_id": "r", "payload": {"size": 1}}
    record = Record.model_validate(data, context={"registry": mine})
    edit = _edit("size", "1.0", "2", record_id="r")
    with pytest.raises(ValidationError):
        apply_edits(record, [edit])  # the default registry doesn't know test.own
    assert apply_edits(record, [edit], registry=mine).payload == {"size": 2.0}
