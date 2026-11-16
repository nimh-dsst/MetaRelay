# Examples

Runnable scripts for common tasks, in [`examples/`](../examples). Each one
runs without an account, and CI runs each one end to end.

| Example | Task | Needs |
| --- | --- | --- |
| [`save_metadata`](../examples/save_metadata/README.md) | Write a record to any destination, read it back, update it, and handle a conflict | Base install |
| [`open_labarchives_notebook`](../examples/open_labarchives_notebook/README.md) | Log in to LabArchives, list notebooks, open one, and load a record | `metarelay[labarchives]`, and the fake server from `docker compose up` |
| [`open_elabftw_experiment`](../examples/open_elabftw_experiment/README.md) | Connect to eLabFTW with an API key, list entries, open one, and load a record | `metarelay[elabftw]`, and eLabFTW from `docker compose up elabftw` |

Run them from the repository root as modules, after `uv sync` (add
`--extra labarchives` for LabArchives, `--extra elabftw` for eLabFTW).
`save_metadata` writes to every destination; its README shows the URI and
settings for [LabArchives](../examples/save_metadata/README.md#run-it-against-labarchives)
and [eLabFTW](../examples/save_metadata/README.md#run-it-against-elabftw):

```sh
mkdir -p metadata
uv run python -m examples.save_metadata "file://$PWD/metadata"
```

## Shared pieces

| File | Holds |
| --- | --- |
| [`examples/login.py`](../examples/login.py) | `open_session(uri)`: `connect()` for most backends (eLabFTW reads its API key from `METARELAY_ELABFTW_TOKEN`), the browser login for LabArchives |
| [`examples/lab_schemas.py`](../examples/lab_schemas.py) | The `lab.implant` schema every example uses. MetaRelay's own schemas replace it in #10 and #11 |

## How they stay correct

- Each example splits reusable functions, which take a session and return
  data, from `main()`, which handles arguments, login, output, and exit codes.
  This is the same layout as `labapi`'s examples.
- `tests/examples/` runs every example against a temporary folder and an
  in-process fake LabArchives server, both by calling `main()` and as
  `python -m examples.<name>`. The eLabFTW tests run against the dev stack's
  eLabFTW when `METARELAY_ELABFTW_TEST_API_URL` is set, as in CI's `elabftw`
  job.
- The code in the [quickstart](quickstart.md), [concepts](concepts.md),
  [LabArchives](labarchives.md), and [eLabFTW](elabftw.md) pages is copied from marked regions of the
  examples. `tests/examples/test_docs_snippets.py` fails if a page and its
  example drift apart. After changing an example, update the pages with
  `uv run python tests/examples/test_docs_snippets.py`.

Writing a backend isn't an example here. The "write a backend" guide (#13)
covers it. Until then, read `src/metarelay/testing/memory.py` (the smallest
backend), then `backends/local.py` and `backends/labarchives.py`, and see
[architecture](architecture.md).
