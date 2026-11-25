# Contributing to MetaRelay

## Setup

You need [uv](https://docs.astral.sh/uv/). uv installs the right Python for
you.

```sh
git clone https://github.com/nimh-dsst/MetaRelay.git
cd MetaRelay
uv sync
```

`uv sync` creates `.venv/` with the package installed in editable mode and the
`dev` dependency group (pytest, ruff). To work on a backend, add its extra:

```sh
uv sync --extra labarchives   # or elabftw, github, nwb
uv sync --all-extras          # everything
```

The project targets Python 3.12, 3.13, and 3.14. `.python-version` pins 3.12,
the oldest supported version, for local work. To test on another version:

```sh
uv run --python 3.13 pytest
```

## Running checks

CI runs these on every PR. Run them before you push.

```sh
uv run ruff check .           # lint
uv run ruff format --check .  # formatting
uv run pytest                 # tests
uv build                      # sdist and wheel
uv run --group docs sphinx-build -W --keep-going -n -b html docs docs/_build/html  # docs
```

`uv run ruff format .` and `uv run ruff check --fix .` fix most issues.

## Tests

- Tests live in `tests/` and need no credentials. Every backend passes the
  shared conformance suite (`metarelay.testing.BackendConformance`), run
  against the most realistic thing that needs no account:

  | Backend | Default `pytest` runs against | Live check, run by hand |
  | --- | --- | --- |
  | Local | A temporary folder | None needed |
  | LabArchives | The fake LabArchives server, in-process | DSST runs the smoke script ([LabArchives](docs/labarchives.md#checking-against-real-labarchives-dsst)) |
  | eLabFTW | A real eLabFTW in Docker. Without it, most eLabFTW tests skip | None needed: CI runs the real server |
  | GitHub | The fake GitHub server, in-process | `tests/backends/test_github_live.py` ([GitHub](docs/github.md#testing)) |

  The backends differ on purpose. eLabFTW ships a Docker image, so CI tests
  the real server. LabArchives can't be self-hosted and GitHub needs a token,
  so their default tests use fakes, and the live checks before each release
  keep those fakes honest.

- To run the eLabFTW tests locally, start the server from `compose.yaml`:

  ```sh
  docker compose up -d --wait elabftw
  docker compose exec elabftw bin/init db:populate /seed/populate.yml --yes --fast
  METARELAY_ELABFTW_TEST_API_URL=http://localhost:3148/api/v2 uv run --all-extras pytest
  ```

- Never call a real LabArchives or GitHub from the default test run. The
  live checks are opt-in and need credentials. Run them before each release,
  and after changing that backend or its fake: the fakes only behave the way
  we wrote them.
- Add a test for every bug fix and every new behavior, in the one layer that
  owns the rule. Don't test a rule again in another layer:

  | Layer | Owns | Example |
  | --- | --- | --- |
  | `tests/core/` | Logic and its edge cases | A row of the `decide_write` table, a preview-diff quirk |
  | `BackendConformance` | That every backend wires the contract up | A missing container is `NotFoundError` on every backend |
  | `tests/backends/`, `tests/testing/` | One backend's own storage, races and errors | GitHub retries a racing commit; local refuses `CON` |
  | `tests/examples/` | What each example itself prints and handles | The LabArchives example hides the signed login link |

  In a parametrized table, keep one case per branch of the code, not one per
  value. Don't test what a library guarantees: pydantic round trips, enum
  values, hashes. A test must fail when the rule it checks breaks.
- `tests/examples/` runs every example in `examples/` end to end, and checks
  that the code in the docs still matches the examples. After changing an
  example, run `uv run python tests/examples/test_docs_snippets.py` to update
  the docs.

## Docs

The docs site is built with [Sphinx](https://www.sphinx-doc.org/) from the
Markdown files in `docs/`, parsed with [MyST](https://myst-parser.readthedocs.io/),
and published to <https://nimh-dsst.github.io/MetaRelay/> on every push to `main`.
Build it locally with the `docs` dependency group:

```sh
uv run --group docs sphinx-build -W --keep-going -n -b html docs docs/_build/html
```

Open `docs/_build/html/index.html` to read it. CI runs the same build on every
PR, and `-W` turns every warning into an error.

- Write docs in Markdown. A new page goes in a `toctree` in `docs/index.md`, or
  the build fails.
- Link to other pages by relative path, like `[Concepts](concepts.md)`, so the
  link works on GitHub too. Links to files outside `docs/`, such as
  `../examples/login.py`, become links to the file on GitHub.
- Code in the docs comes from `examples/`. See [Tests](#tests).

## Dependencies

- The base install depends only on `pydantic` and `pydantic-settings`. A test
  enforces this. Anything backend-specific goes in that backend's extra.
- Change dependencies with `uv add` or `uv remove` so `uv.lock` stays in sync.
  CI installs with `--locked` and fails if the lockfile is stale.

## Pull requests

- Keep PRs small and focused. DSST staff review every PR, so aim for a diff
  that one person can review in one sitting.
- Link the issue the PR addresses.
- Use a [conventional commit](https://www.conventionalcommits.org/) prefix in
  the PR title: `feat`, `fix`, `docs`, `chore`, `refactor`, or `test`, with a
  scope when there is a natural one, for example `feat(local): ...`.
- Describe what changed, why, and how to test it.
- CI must pass before merge.
