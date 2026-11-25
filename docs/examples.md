# Examples

Runnable scripts for common tasks, in [`examples/`](../examples). Each one
runs without an account, and CI runs each one end to end.

| Example | Task | Needs |
| --- | --- | --- |
| [`first_write`](../examples/first_write.py) | The README's first write: write one record to `./metadata` and read it back. Run it as a script, not a module | Base install |
| [`save_metadata`](../examples/save_metadata/README.md) | Write a record to any destination, read it back, update it, and handle a conflict | Base install |
| [`open_labarchives_notebook`](../examples/open_labarchives_notebook/README.md) | Log in to LabArchives, list notebooks, open one, and load a record | `metarelay[labarchives]`, and the fake server from `docker compose up` |
| [`open_elabftw_experiment`](../examples/open_elabftw_experiment/README.md) | Connect to eLabFTW with an API key, list entries, open one, and load a record | `metarelay[elabftw]`, and eLabFTW from `docker compose up elabftw` |
| [`open_github_repo`](../examples/open_github_repo/README.md) | Connect to GitHub with a token, list folders, open one, and load a record | `metarelay[github]`, and the fake from `python -m metarelay.testing.github_fake` |
| [`credentials_env_file`](../examples/credentials_env_file.py) | Read a token from a `.env` file with `_env_file=` | `metarelay[github]`. It serves its own fake GitHub |

Run them from the repository root as modules, after `uv sync` (add
`--extra labarchives` for LabArchives, `--extra elabftw` for eLabFTW,
`--extra github` for GitHub). `save_metadata` writes to every destination;
its README shows the URI and settings for
[LabArchives](../examples/save_metadata/README.md#run-it-against-labarchives),
[eLabFTW](../examples/save_metadata/README.md#run-it-against-elabftw), and
[GitHub](../examples/save_metadata/README.md#run-it-against-github):

```sh
mkdir -p metadata
uv run python -m examples.save_metadata "file://$PWD/metadata"
```

## Shared pieces

| File | Holds |
| --- | --- |
| [`examples/login.py`](../examples/login.py) | `open_session(uri)`: `connect()` for most backends (eLabFTW reads its API key from `METARELAY_ELABFTW_TOKEN`, GitHub its token from `METARELAY_GITHUB_TOKEN`), the browser login for LabArchives |
| [`examples/lab_schemas.py`](../examples/lab_schemas.py) | The `lab.implant` schema every example uses. Used until MetaRelay ships its own schemas |

## How they stay correct

- Each example splits reusable functions, which take a session and return
  data, from `main()`, which handles arguments, login, output, and exit codes.
  This is the same layout as `labapi`'s examples.
- `tests/examples/` runs every example against a temporary folder and
  in-process fake LabArchives and GitHub servers, both by calling `main()`
  and as `python -m examples.<name>`. The eLabFTW tests run against the dev
  stack's eLabFTW when `METARELAY_ELABFTW_TEST_API_URL` is set, as in CI's
  `elabftw` job.
- The code in the README and these docs is copied from the examples, and a
  test fails if the two drift apart. See [Contributing](contributing.md#tests).

Writing a backend isn't an example here. A guide to writing one is planned.
Until then, read `src/metarelay/testing/memory.py` (the smallest
backend), then `backends/local.py` and `backends/labarchives.py`, and see
[architecture](architecture.md).
