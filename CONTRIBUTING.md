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
```

`uv run ruff format .` and `uv run ruff check --fix .` fix most issues.

## Tests

- Tests live in `tests/` and run without network access or credentials.
- Never call a real ELN API from the test suite. Use fakes or recorded
  responses. Live smoke tests are opt-in and gated by secrets.
- Add a test for every bug fix and every new behavior.
- `tests/examples/` runs every example in `examples/` end to end, and checks
  that the code in the docs still matches the examples. After changing an
  example, run `uv run python tests/examples/test_docs_snippets.py` to update
  the docs.

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
