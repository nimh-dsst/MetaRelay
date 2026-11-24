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


def test_links_that_leave_docs_go_to_github(github_links) -> None:
    page = DOCS / "examples.md"
    # A file goes to its blob, keeping the anchor; a directory goes to its tree.
    link = "[a](../examples/x/README.md#run-it)"
    assert github_links(link, page) == f"[a]({BLOB}/examples/x/README.md#run-it)"
    assert github_links("[a](../examples)", page) == f"[a]({TREE}/examples)"


def test_links_inside_docs_and_external_links_are_untouched(github_links) -> None:
    for text in [
        "[a](concepts.md#write-modes)",
        "[a](adr/0001-elabftw-record-storage.md)",
        "[a](https://example.org/x)",
        "[a](#local)",
    ]:
        assert github_links(text, DOCS / "examples.md") == text, text


def test_links_from_an_included_file_resolve_from_that_file(github_links) -> None:
    # CONTRIBUTING.md sits at the repository root and is included into docs/.
    root_file = DOCS.parent / "CONTRIBUTING.md"
    page = DOCS / "contributing.md"
    assert github_links("[a](LICENSE)", root_file, page) == f"[a]({BLOB}/LICENSE)"
    assert github_links("[a](docs/concepts.md#x)", root_file, page) == "[a](concepts.md#x)"


def test_links_in_code_are_left_as_written(github_links) -> None:
    for text in [
        "Link like `[a](../examples/login.py)`.",
        "Link like ``[a](../examples/login.py)``.",
        "```markdown\n[a](../examples/login.py)\n```\n",
        "~~~\n[a](../examples/login.py)\n~~~\n",
        "````\n```\n[a](../examples/login.py)\n```\n````\n",
    ]:
        assert github_links(text, DOCS / "examples.md") == text, text


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
