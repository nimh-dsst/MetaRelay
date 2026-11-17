# Open an eLabFTW entry

Connect to eLabFTW with an API key, list what is there, open an entry, and
load a record. It's the eLabFTW counterpart of
[`open_labarchives_notebook`](../open_labarchives_notebook/README.md).

`open_elabftw_experiment.py` has two layers:

- `open_entry()` takes an open session and returns what it found. Import it
  to reuse the steps.
- `main()` is the command line: arguments, connecting, output, and exit codes.

eLabFTW has no folders, so every container MetaRelay makes is an entry (an
experiment, or a resource with `/resources`), and a path like
`Fiber study/Cohort A` is an entry inside an entry. The records it loads
follow the `lab.implant` schema in [`examples/lab_schemas.py`](../lab_schemas.py).
A record whose schema isn't registered can't be loaded, so an application
imports its schemas before it reads.

## Run it against the dev stack's eLabFTW

No account needed. From the repository root:

```sh
uv sync --extra elabftw
docker compose up -d --wait elabftw
docker compose exec elabftw bin/init db:populate /seed/populate.yml --yes --fast
export METARELAY_ELABFTW_TOKEN=metarelay-dev-key
export METARELAY_ELABFTW_API_URL=http://localhost:3148/api/v2
```

The seeded eLabFTW starts with no records, so write one with the
[`save_metadata` example](../save_metadata/README.md) first:

```sh
uv run python -m examples.save_metadata elabftw://localhost:3148/experiments
uv run python -m examples.open_elabftw_experiment --uri elabftw://localhost:3148/experiments \
    --path "Fiber study/Cohort A"
```

```
Opened Fiber study/Cohort A
Records: implant-M1

implant-M1 (lab.implant v1), entry 5030:

## lab.implant: implant-M1
…
| fiber_diameter_um | 200.0 |
| target_region | VTA |
```

The table is the same one the entry's body shows in eLabFTW. To see it there,
log in at http://localhost:3148 as `metarelay@example.org` /
`metarelay-dev-password` and open the entry with that number.

| Option | Does |
| --- | --- |
| `--uri URI` | The eLabFTW destination, ending in `/experiments` or `/resources` |
| `--path "a/b"` | The entry to open. Default: list the top-level entries |
| `--record ID` | The record to load. Default: the first one |

An entry with no records lists the entries inside it instead, so
`--path "Fiber study"` shows `Cohort A/`.

## Run it against a real eLabFTW

Create an API key in eLabFTW (your user settings, *API keys*), and leave out
`METARELAY_ELABFTW_API_URL`: requests then go to `https://<host>/api/v2`.

```sh
export METARELAY_ELABFTW_TOKEN=...
uv run python -m examples.open_elabftw_experiment --uri elabftw://elab.example.org/experiments
```

MetaRelay only looks at entries of the API key's team.
[`docs/elabftw.md`](../../docs/elabftw.md) covers where records go and what
happens when someone edits them in eLabFTW.
