# Save metadata

Write a metadata record to any destination, read it back, and update it. The
same code works for every backend. Only the destination URI changes.

`save_metadata.py` has two layers:

- `save_metadata()` and `write()` take an open session and return receipts.
  Import them to reuse the steps.
- `main()` is the command line: arguments, login, output, and exit codes.

The record follows the `lab.implant` schema in
[`examples/lab_schemas.py`](../lab_schemas.py). The login goes through the
shared helper, [`examples/login.py`](../login.py).

## Run it against a folder

No account needed. From the repository root:

```sh
uv sync
mkdir -p metadata
uv run python -m examples.save_metadata "file://$PWD/metadata"
```

```
Wrote implant-M1 to file:///…/metadata in Fiber study/Cohort A
Read it back: {'fiber_diameter_um': 400.0, 'target_region': 'VTA'}
Updated it:   {'fiber_diameter_um': 200.0, 'target_region': 'VTA'}
Receipts saved in receipts.json
```

The record is `metadata/Fiber study/Cohort A/implant-M1.json`, with a
readable table of it in `implant-M1.md` beside it. Run it again and it
succeeds again: the receipts in `receipts.json` show that the record there is
the one this script wrote last time. Delete `receipts.json` and the next run
stops with a conflict, because MetaRelay can no longer tell.

| Option | Does |
| --- | --- |
| `--path "a/b"` | Where the record goes. Default `Fiber study/Cohort A` |
| `--receipts FILE` | Where receipts are kept between runs. Default `receipts.json` |
| `--force` | Replace the record even if it changed at the destination |
| `--auto-login` | Log in to the fake LabArchives server without a browser |

## When the record changed at the destination

Change the record by hand, as a colleague might:

```sh
sed -i.bak 's/200.0/300.0/' "metadata/Fiber study/Cohort A/implant-M1.json"
uv run python -m examples.save_metadata "file://$PWD/metadata"
```

The write raises `ConflictError`, so the script stops with exit status 3
and changes nothing:

```
implant-M1 changed at file:///…/metadata since this script last wrote it, so MetaRelay did not overwrite it.
  At the destination now: {'fiber_diameter_um': 300.0, 'target_region': 'VTA'}
To replace it with this script's record, run again with --force.
This script always writes the same record, so it can't keep that change. To keep one in your own code, see docs/concepts.md#handling-a-conflict.
```

MetaRelay never picks a winner. The person decides, and `--force` is how
they say so. Editing the table in `implant-M1.md` counts as a change too.

This script can only replace the change, because it writes the same record
every run. Application code can keep it instead. See
[Handling a conflict](../../docs/concepts.md#handling-a-conflict).

## Run it against LabArchives

Install the extra, and start the fake LabArchives server (no account):

```sh
uv sync --extra labarchives
docker compose up -d
export METARELAY_LABARCHIVES_AKID=fake-akid METARELAY_LABARCHIVES_AKPASS=fake-akpass
export METARELAY_LABARCHIVES_API_URL=http://localhost:8080
uv run python -m examples.save_metadata labarchives://localhost:8080 \
    --path "Test notebook/Fiber study/Cohort A" --auto-login
```

In LabArchives the first name in `--path` is a notebook, and it must already
exist. The fake starts with `Lab Notebook` and `Test notebook`. Then open the
record with the
[`open_labarchives_notebook` example](../open_labarchives_notebook/README.md).

For real LabArchives, set the institution's key pair, drop
`METARELAY_LABARCHIVES_API_URL` and `--auto-login`, and use
`labarchives://api.labarchives.com`. The script prints a login link. Log in,
then paste the address your browser was sent back to. That page may not
load, which is expected: only its address matters.
