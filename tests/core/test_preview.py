import pydantic
import pytest

from metarelay import Heading, Preview, Record, Table, register_renderer, render_preview
from metarelay.core import preview as preview_module
from metarelay.core.checksum import preview_checksum
from metarelay.core.preview import format_value, generic_blocks, to_html, to_markdown


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
