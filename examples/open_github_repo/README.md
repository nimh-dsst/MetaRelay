# Open a GitHub repository

Connect to GitHub with a token, list what is there, open a folder, and load a
record. It's the GitHub counterpart of
[`open_labarchives_notebook`](../open_labarchives_notebook/README.md).

`open_github_repo.py` has two layers:

- `open_folder()` takes an open session and returns what it found. Import it
  to reuse the steps.
- `main()` is the command line: arguments, connecting, output, and exit codes.

Containers are folders in the repository, under the URI's prefix, and each
record is a `.json` file with a `.md` preview beside it. The records it loads
follow the `lab.implant` schema in [`examples/lab_schemas.py`](../lab_schemas.py).
A record whose schema isn't registered can't be loaded, so an application
imports its schemas before it reads.

## Run it against the fake GitHub API

No account needed. From the repository root, start the fake in one terminal:

```sh
uv sync --extra github
uv run python -m metarelay.testing.github_fake --port 8081 --repo my-lab/metadata --token fake-token
```

Its repository starts empty, so in another terminal write a record with the
[`save_metadata` example](../save_metadata/README.md) first:

```sh
export METARELAY_GITHUB_TOKEN=fake-token METARELAY_GITHUB_API_URL=http://127.0.0.1:8081
uv run python -m examples.save_metadata github://my-lab/metadata@main/records
uv run python -m examples.open_github_repo --uri github://my-lab/metadata@main/records \
    --path "Fiber study/Cohort A"
```

```
Opened Fiber study/Cohort A
Records: implant-M1

implant-M1 (lab.implant v1), records/Fiber study/Cohort A/implant-M1.json:

## lab.implant: implant-M1
…
| fiber_diameter_um | 200.0 |
| target_region | VTA |
```

The table is the same one GitHub shows for `implant-M1.md`.

| Option | Does |
| --- | --- |
| `--uri URI` | The destination, `github://<owner>/<repo>@<branch>[/<prefix>]` |
| `--path "a/b"` | The folder to open. Default: the top level (the prefix) |
| `--record ID` | The record to load. Default: the first one |

## Run it against real GitHub

Create a fine-grained token with **Contents: read and write** on the
repository (read-only is enough for this example), and leave out
`METARELAY_GITHUB_API_URL`:

```sh
export METARELAY_GITHUB_TOKEN=github_pat_...
uv run python -m examples.open_github_repo --uri github://my-lab/metadata@main/records
```

The branch must already exist. [`docs/github.md`](../../docs/github.md) covers
the folder layout, conflicts, and the size cap.
