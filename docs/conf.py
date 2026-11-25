"""Sphinx configuration for the MetaRelay docs site.

The pages are the Markdown files in this folder, parsed with MyST. Build them with:

    uv run --group docs sphinx-build -W --keep-going -n -b html docs docs/_build/html
"""

from __future__ import annotations

import posixpath
import re
import tomllib
from datetime import datetime
from pathlib import Path

DOCS = Path(__file__).resolve().parent
REPO = DOCS.parent
GITHUB = "https://github.com/nimh-dsst/MetaRelay"

# -- Project information -----------------------------------------------------

project = "MetaRelay"
author = "MetaRelay contributors"
copyright = f"{datetime.now().year}, {author}"

with (REPO / "pyproject.toml").open("rb") as project_file:
    release = tomllib.load(project_file)["project"]["version"]
version = ".".join(release.split(".")[:2])

# -- General configuration ---------------------------------------------------

extensions = [
    "myst_parser",
    "sphinx_copybutton",
    "sphinx_design",
]

source_suffix = {".md": "markdown"}
# The original build plan stays in the repository for the team, off the site.
exclude_patterns = ["_build", "IMPLEMENTATION_PLAN.md"]
language = "en"

myst_enable_extensions = ["colon_fence"]
# Headings up to this level get anchors, so links like concepts.md#write-modes resolve.
myst_heading_anchors = 3

# GitHub builds heading anchors in the browser, so linkcheck can't see them.
linkcheck_anchors_ignore_for_url = [r"https://github\.com/.*"]

# -- Options for HTML output -------------------------------------------------

html_theme = "pydata_sphinx_theme"
html_title = "MetaRelay"
html_static_path = ["_static"]
html_css_files = ["brand.css"]

html_theme_options = {
    "logo": {"text": "MetaRelay"},
    "github_url": GITHUB,
    "icon_links": [
        {
            "name": "Issue tracker",
            "url": f"{GITHUB}/issues",
            "icon": "fa-solid fa-bug",
        },
    ],
    "header_links_before_dropdown": 7,
    "show_toc_level": 2,
    "navigation_with_keys": False,
}

# -- Links to files outside the site -----------------------------------------
# The docs link to examples and source files by relative path, so the links work
# when browsing the repository on GitHub. Those files are not pages on the site,
# so point the links at GitHub instead. Code spans and fenced code blocks are
# left as written, since a link there is an example, not a link.

LINK = re.compile(r"\]\((?P<target>[^)\s#]+)(?P<anchor>#[^)\s]*)?\)")
CODE = re.compile(
    r"""
    ^[ ]{0,3}(?P<fence>(?P<char>[`~])(?P=char){2,})[^\n]*\n  # opening fence
    .*?
    (?:^[ ]{0,3}(?P=fence)(?P=char)*[ \t]*$|\Z)            # closing fence, or end of text
    |
    (?P<ticks>`+).+?(?<!`)(?P=ticks)(?!`)                   # inline code span
    """,
    re.MULTILINE | re.DOTALL | re.VERBOSE,
)


def github_links(text: str, source: Path, page: Path | None = None) -> str:
    """Rewrite relative links that leave docs/ into links to the file on GitHub.

    `source` is the file the text came from. `page` is the page it lands on, when
    another page includes it. Links into docs/ are made relative to that page.
    """
    page = page or source

    def replace(match: re.Match[str]) -> str:
        target = match["target"]
        if "://" in target or target.startswith(("mailto:", "/")):
            return match[0]
        resolved = (source.parent / target).resolve()
        if resolved.is_relative_to(DOCS):
            if page == source:
                return match[0]
            local = posixpath.relpath(resolved.as_posix(), page.resolve().parent.as_posix())
            return f"]({local}{match['anchor'] or ''})"
        relative = posixpath.normpath(resolved.relative_to(REPO).as_posix())
        kind = "tree" if resolved.is_dir() else "blob"
        return f"]({GITHUB}/{kind}/main/{relative}{match['anchor'] or ''})"

    parts = []
    position = 0
    for code in CODE.finditer(text):
        parts.append(LINK.sub(replace, text[position : code.start()]))
        parts.append(code[0])
        position = code.end()
    parts.append(LINK.sub(replace, text[position:]))
    return "".join(parts)


def on_source_read(app, docname: str, content: list[str]) -> None:
    content[0] = github_links(content[0], Path(app.env.doc2path(docname)))


def on_include_read(app, relative_path: Path, parent_docname: str, content: list[str]) -> None:
    page = Path(app.env.doc2path(parent_docname))
    content[0] = github_links(content[0], Path(app.srcdir) / relative_path, page)


def setup(app) -> dict[str, bool]:
    app.connect("source-read", on_source_read)
    app.connect("include-read", on_include_read)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
