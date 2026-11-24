"""The docs site rewrites links that leave docs/ into links to the file on GitHub."""

import importlib.util
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[1] / "docs"
BLOB = "https://github.com/nimh-dsst/MetaRelay/blob/main"
TREE = "https://github.com/nimh-dsst/MetaRelay/tree/main"


@pytest.fixture(scope="module")
def github_links():
    spec = importlib.util.spec_from_file_location("docs_conf", DOCS / "conf.py")
    assert spec is not None and spec.loader is not None
    conf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conf)
    return conf.github_links


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[a](../examples/login.py)", f"[a]({BLOB}/examples/login.py)"),
        ("[a](../examples/x/README.md#run-it)", f"[a]({BLOB}/examples/x/README.md#run-it)"),
        ("[a](../examples)", f"[a]({TREE}/examples)"),
        ("[a](concepts.md#write-modes)", "[a](concepts.md#write-modes)"),
        ("[a](adr/0001-elabftw-record-storage.md)", "[a](adr/0001-elabftw-record-storage.md)"),
        ("[a](https://example.org/x)", "[a](https://example.org/x)"),
        ("[a](#local)", "[a](#local)"),
    ],
)
def test_links_from_a_top_level_page(github_links, text: str, expected: str) -> None:
    assert github_links(text, DOCS / "examples.md") == expected


def test_links_from_an_included_file_resolve_from_that_file(github_links) -> None:
    # CONTRIBUTING.md sits at the repository root and is included into docs/.
    root_file = DOCS.parent / "CONTRIBUTING.md"
    page = DOCS / "contributing.md"
    assert github_links("[a](LICENSE)", root_file, page) == f"[a]({BLOB}/LICENSE)"
    assert github_links("[a](docs/concepts.md#x)", root_file, page) == "[a](concepts.md#x)"


@pytest.mark.parametrize(
    "text",
    [
        "Link like `[a](../examples/login.py)`.",
        "Link like ``[a](../examples/login.py)``.",
        "```markdown\n[a](../examples/login.py)\n```\n",
        "~~~\n[a](../examples/login.py)\n~~~\n",
        "````\n```\n[a](../examples/login.py)\n```\n````\n",
    ],
)
def test_links_in_code_are_left_as_written(github_links, text: str) -> None:
    assert github_links(text, DOCS / "examples.md") == text


def test_links_next_to_code_are_still_rewritten(github_links) -> None:
    text = "`[a](x.md)` and [b](../examples/login.py)\n```\n[c](../examples)\n```\n[d](../examples)"
    expected = (
        f"`[a](x.md)` and [b]({BLOB}/examples/login.py)\n```\n[c](../examples)\n```\n"
        f"[d]({TREE}/examples)"
    )
    assert github_links(text, DOCS / "examples.md") == expected


def test_contributing_keeps_its_relative_link_example(github_links) -> None:
    root_file = DOCS.parent / "CONTRIBUTING.md"
    text = root_file.read_text(encoding="utf-8")
    assert "`[Concepts](concepts.md)`" in text
    assert "`[Concepts](concepts.md)`" in github_links(text, root_file, DOCS / "contributing.md")
