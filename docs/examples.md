# Examples

Runnable scripts for common tasks, in [`examples/`](../examples). Each one
runs without an account, and CI runs each one end to end.

| Example | Task | Needs |
| --- | --- | --- |
| [`save_metadata`](../examples/save_metadata/README.md) | Write a record to any destination, read it back, update it, and handle a conflict | Base install |
| [`open_labarchives_notebook`](../examples/open_labarchives_notebook/README.md) | Log in to LabArchives, list notebooks, open one, and load a record | `metarelay[labarchives]`, and the fake server from `docker compose up` |

Run them from the repository root as modules, after `uv sync` (add
`--extra labarchives` for LabArchives):

```sh
mkdir -p metadata
uv run python -m examples.save_metadata "file://$PWD/metadata"
```

## Shared pieces

| File | Holds |
| --- | --- |
| [`examples/login.py`](../examples/login.py) | `open_session(uri)`: `connect()` for most backends, the browser login for LabArchives |
| [`examples/lab_schemas.py`](../examples/lab_schemas.py) | The `lab.implant` schema both examples use. MetaRelay's own schemas replace it in #10 and #11 |

## How they stay correct

- Each example splits reusable functions, which take a session and return
  data, from `main()`, which handles arguments, login, output, and exit codes.
  This is the same layout as `labapi`'s examples.
- `tests/examples/` runs every example against a temporary folder and an
  in-process fake LabArchives server, both by calling `main()` and as
  `python -m examples.<name>`.
- The code in the [quickstart](quickstart.md), [concepts](concepts.md), and
  [LabArchives](labarchives.md) pages is copied from marked regions of the
  examples. `tests/examples/test_docs_snippets.py` fails if a page and its
  example drift apart. After changing an example, update the pages with
  `uv run python tests/examples/test_docs_snippets.py`.

Writing a backend isn't an example here. The "write a backend" guide (#13)
covers it. Until then, read `src/metarelay/testing/memory.py` (the smallest
backend), then `backends/local.py` and `backends/labarchives.py`, and see
[architecture](architecture.md).
