# AGENTS.md: MetaRelay

MetaRelay is a backend-agnostic Python library for writing NWB metadata
records to electronic lab notebooks, GitHub, and local disk. Read
[`docs/concepts.md`](docs/concepts.md) for the vocabulary and
[`docs/architecture.md`](docs/architecture.md) for what each core file owns
and how one write flows through them. The roadmap and plan decisions (D1,
D7, ...) are in [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md).

## Layout

| Path | Holds |
| --- | --- |
| `src/metarelay/core/` | The backend-neutral public API: records, schemas, sessions, receipts, conflicts, previews, errors. Imports no backend |
| `src/metarelay/backends/` | One module per backend: `local.py` (`file://`), `labarchives.py` (`labarchives://`) |
| `src/metarelay/testing/` | `MemorySession`, the conformance suite, and the fake LabArchives server with its smoke and cassette tools |
| `src/metarelay/cli.py` | The `metarelay` command |
| `tests/` | One folder per package folder, plus `tests/examples/` for the examples |
| `examples/` | Runnable examples. Indexed in [`docs/examples.md`](docs/examples.md) |
| `docs/` | User and developer docs |
| `docker/`, `compose.yaml` | The dev stack: the fake LabArchives server |

## Commands

```sh
uv sync --all-extras          # every backend and extra
uv run ruff check .           # lint
uv run ruff format --check .  # formatting
uv run pytest                 # tests, examples, and the docs drift check
uv build                      # sdist and wheel
docker compose up -d          # the fake LabArchives server on port 8080
```

CI runs the same checks on Python 3.12, 3.13, and 3.14, once with the base
install and once with all extras. `uv run ruff format .` and
`uv run ruff check --fix .` fix most lint.

## Rules that hold everywhere

- **The core imports no backend.** Backends register under the
  `metarelay.backends` entry-point group in `pyproject.toml`, and
  `connect()` loads only the one a URI names.
- **The base install is `pydantic` and `pydantic-settings` only.** A test
  enforces it. Backend dependencies go in that backend's extra. Change
  dependencies with `uv add` or `uv remove` so `uv.lock` stays in sync.
- **Conflicts are decided only by `decide_write`** (`core/conflict.py`). A
  backend works out the checksums of what is at the destination and asks.
- **Previews come only from `render_preview`** (`core/preview.py`).
  Backends never build preview markup.
- **Raise only `metarelay` errors.** Translate native errors (`labapi`,
  `requests`, `OSError`) into the classes in `core/errors.py`.
- **Never put a secret in a message.** No token, signed URL, or input value
  in an error or log. `labapi` signs requests in the query string, so never
  print or chain a `requests` error. Use `raise ... from None`.
- **Never store credentials.** Callers pass config objects. MetaRelay never
  saves them, and destination URIs can't hold a username or password.
- **Never call a real ELN from the test suite.** Use `MemorySession`, the
  fake LabArchives server, or recorded cassettes. Live smoke tests are
  opt-in.
- **Every bug fix and new behavior gets a test.** A new backend must pass
  `metarelay.testing.BackendConformance`.
- **Docs code comes from the examples.** Code between `# [docs:<name>]` and
  `# [/docs:<name>]` in `examples/` is copied into the docs after a
  `<!-- example: <file>#<name> -->` marker. After changing either side, run
  `uv run python tests/examples/test_docs_snippets.py` to rewrite the docs,
  or the tests fail.
- **Examples run against no account.** Each uses a temporary folder or the
  fake LabArchives server, and `tests/examples/` runs it end to end.

## Writing

American English. Short, plain sentences. Conventional commit prefixes
(`feat`, `fix`, `docs`, `chore`, `refactor`, `test`) with a scope when
natural, such as
`feat(local): ...`. PR titles follow the same form. See
[`CONTRIBUTING.md`](CONTRIBUTING.md).
