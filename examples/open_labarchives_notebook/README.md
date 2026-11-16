# Open a LabArchives notebook

Log in to LabArchives, list your notebooks, open one, and load a record. This
is the first thing the Chronoxiv migration rebuilds (Goal 1c).

`open_labarchives_notebook.py` has two layers:

- `open_notebook()` takes a logged-in session and returns what it found.
  Import it to reuse the steps.
- `main()` is the command line: arguments, login, output, and exit codes.

The login goes through the shared helper,
[`examples/login.py`](../login.py). The records it loads follow the
`lab.implant` schema in [`examples/lab_schemas.py`](../lab_schemas.py). A
record whose schema isn't registered can't be loaded, so an application
imports its schemas before it reads.

## Run it against the fake LabArchives server

No account needed. From the repository root:

```sh
uv sync --extra labarchives
docker compose up -d
export METARELAY_LABARCHIVES_AKID=fake-akid METARELAY_LABARCHIVES_AKPASS=fake-akpass
export METARELAY_LABARCHIVES_API_URL=http://localhost:8080
```

The fake's notebooks start with no records, so write one with the
[`save_metadata` example](../save_metadata/README.md) first:

```sh
uv run python -m examples.save_metadata labarchives://localhost:8080 \
    --path "Test notebook/Fiber study/Cohort A" --auto-login
uv run python -m examples.open_labarchives_notebook --uri labarchives://localhost:8080 \
    --notebook "Test notebook" --path "Fiber study/Cohort A" --auto-login
```

```
Notebooks: Test notebook, Lab Notebook
Opened Test notebook/Fiber study/Cohort A
Records: implant-M1

implant-M1 (lab.implant v1):

## lab.implant: implant-M1
…
| fiber_diameter_um | 200.0 |
| target_region | VTA |
```

The table is the same one the notebook entry shows in LabArchives.

| Option | Does |
| --- | --- |
| `--uri URI` | The LabArchives destination. Default `labarchives://api.labarchives.com` |
| `--notebook NAME` | The notebook to open. Without it, the script only lists notebooks |
| `--path "a/b"` | A folder or page inside the notebook. Default: the notebook itself |
| `--record ID` | The record to load. Default: the first one |
| `--auto-login` | Log in to the fake without a browser. The fake only |

A folder or page with no records lists what is inside it instead, so
`--notebook "Lab Notebook" --path "Example experiment"` shows the seeded
`Notes` page.

## Run it against real LabArchives

Set the institution's API key pair, and leave out `--auto-login`:

```sh
export METARELAY_LABARCHIVES_AKID=... METARELAY_LABARCHIVES_AKPASS=...
uv run python -m examples.open_labarchives_notebook --notebook "My notebook"
```

The script prints a login link. Log in, then paste the address your browser
was sent back to (`http://localhost:8089/callback?...`). That page may not
load, which is expected: only its address matters. A web app serves that
address itself instead. [`docs/labarchives.md`](../../docs/labarchives.md)
covers the login and where records go in a notebook.
