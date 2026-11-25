"""The docs show code copied from the examples. These tests fail when the two drift.

In an example, mark the lines to copy::

    # [docs:write-and-read]
    ...
    # [/docs:write-and-read]

In a doc, put a marker on the line before the code block that shows them::

    <!-- example: save_metadata/save_metadata.py#write-and-read -->
    ```python
    ...
    ```

The path is relative to ``examples/``. The block must equal the marked lines,
dedented. After changing an example, rewrite every marked block with::

    uv run python tests/examples/test_docs_snippets.py
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples"
DOCS = sorted([REPO / "README.md", *(REPO / "docs").glob("*.md")])
DOCS_THAT_MUST_USE_EXAMPLES = [
    "quickstart.md",
    "concepts.md",
    "labarchives.md",
    "elabftw.md",
    "github.md",
]

REGION = re.compile(
    r"^[ \t]*# \[docs:(?P<name>[\w-]+)\]\n(?P<body>.*?)^[ \t]*# \[/docs:(?P=name)\]\n",
    re.MULTILINE | re.DOTALL,
)
SNIPPET = re.compile(
    r"^(?P<head><!-- example: (?P<file>[\w/.-]+)#(?P<name>[\w-]+) -->\n```\w*\n)"
    r"(?P<body>.*?)^```$",
    re.MULTILINE | re.DOTALL,
)


def regions() -> dict[tuple[str, str], str]:
    """Every marked region in the examples, keyed by (file, name)."""
    found: dict[tuple[str, str], str] = {}
    for path in sorted(EXAMPLES.rglob("*.py")):
        relative = path.relative_to(EXAMPLES).as_posix()
        for match in REGION.finditer(path.read_text()):
            key = (relative, match["name"])
            assert key not in found, f"{relative} marks [docs:{match['name']}] twice."
            # Without blank lines at either end: ruff adds them before a
            # closing marker after a function, and strips them in the docs.
            found[key] = textwrap.dedent(match["body"]).strip("\n") + "\n"
    return found


def snippets() -> list[tuple[Path, re.Match[str]]]:
    return [(doc, match) for doc in DOCS for match in SNIPPET.finditer(doc.read_text())]


def sync_docs() -> list[Path]:
    """Rewrite every marked block in the docs from the examples. Returns the docs changed."""
    known = regions()
    changed = []
    for doc in DOCS:
        text = doc.read_text()

        def replace(match: re.Match[str]) -> str:
            return f"{match['head']}{known[(match['file'], match['name'])]}```"

        updated = SNIPPET.sub(replace, text)
        if updated != text:
            doc.write_text(updated)
            changed.append(doc)
    return changed


def test_every_doc_snippet_matches_the_example() -> None:
    known = regions()
    problems = []
    for doc, match in snippets():
        key = (match["file"], match["name"])
        shown = f"{doc.name} shows examples/{match['file']}#{match['name']}"
        if key not in known:
            problems.append(f"{shown}, but no example marks that region.")
        elif match["body"] != known[key]:
            problems.append(f"{shown}, and no longer matches it.")
    assert problems == [], (
        "\n".join(problems) + "\nRun: uv run python tests/examples/test_docs_snippets.py"
    )


def test_every_marked_region_is_shown_in_the_docs() -> None:
    shown = {(match["file"], match["name"]) for _, match in snippets()}
    unused = sorted(f"{file}#{name}" for file, name in regions() if (file, name) not in shown)
    assert unused == [], f"Marked in an example but shown in no doc: {unused}"


def test_the_guides_show_code_from_the_examples() -> None:
    showing = {doc.name for doc, _ in snippets()}
    missing = [f"docs/{name}" for name in DOCS_THAT_MUST_USE_EXAMPLES if name not in showing]
    assert missing == [], f"These docs show no example code: {missing}"


if __name__ == "__main__":
    for path in sync_docs():
        print(f"Updated {path.relative_to(REPO)}")
