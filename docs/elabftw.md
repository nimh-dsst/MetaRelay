# eLabFTW

The `elabftw://` backend writes records into eLabFTW experiments or resources
through [`elabapi-python`](https://pypi.org/project/elabapi-python/). It
targets eLabFTW 6 (CI runs `elabftw/elabimg:6.0.9`). Why it stores records
the way it does: [ADR 0001](adr/0001-elabftw-record-storage.md).

```bash
pip install 'metarelay[elabftw]'
```

## Connecting

eLabFTW uses an API key (in eLabFTW: your user settings, *API keys*):

```python
from metarelay import connect
from metarelay.backends.elabftw import ElabFTWConfig

session = connect("elabftw://elab.example.org/experiments", ElabFTWConfig(token="..."))
```

The URI's path picks the kind of entry: `/experiments` or `/resources`.
Requests go to `https://<host>/api/v2`; set `api_url` for anything else.

### Credentials

`ElabFTWConfig()` reads `METARELAY_ELABFTW_TOKEN`, `METARELAY_ELABFTW_API_URL`,
and `METARELAY_ELABFTW_VERIFY_TLS` for anything not passed:

```sh
# .env
METARELAY_ELABFTW_TOKEN=...
```

MetaRelay never looks for a `.env` file on its own. Name it with
`_env_file=`, a supported argument despite the underscore (it comes from
`pydantic-settings`):

```python
config = ElabFTWConfig(_env_file=".env")
```

Variables already set in the environment win over the file. Add `.env` to
your `.gitignore` so the key is never committed.

## Opening an entry

The [`open_elabftw_experiment` example](../examples/open_elabftw_experiment/README.md)
lists the entries inside a path and loads a record from it:

<!-- example: open_elabftw_experiment/open_elabftw_experiment.py#open-entry -->
```python
# eLabFTW has no folders: every container is an entry (an experiment or
# a resource). holds_records is True for a records-only entry.
containers = session.list_containers(where)

# An entry's records are in the entry itself: in its metadata, and shown
# as tables in its body and as eLabFTW fields.
records = {}
if where != ROOT:
    records = {stored.record.record_id: stored for stored in session.list_records(where)}
```

## Where things go

eLabFTW has no folders, so every container is an entry.

| MetaRelay | eLabFTW |
| --- | --- |
| A container | An entry titled with the last name in its path, tagged with every prefix of the path (`Study`, `Study/Cohort A`, …) so clicking a tag works like opening a folder |
| `ensure_container(path, holds_records=True)` | A records-only entry, like a LabArchives page: it can't contain containers (`ValidationError`). Listings mark it with `Container.holds_records` |
| Records in a container | In that container's entry, whether it is records-only or not |
| One record | In its entry's metadata as JSON (`metadata.metarelay`), in the body as tables, and as eLabFTW fields grouped under `<record_id> · <table>` |
| Files (`write_file`) | Uploads on the entry. Writing the same name again replaces the upload |

Two read-only fields on every entry, `MetaRelay path` and `MetaRelay parent`,
are how MetaRelay finds entries. Don't change them: an entry whose path field
no longer matches is not found, and `ensure_container` makes a new one.
eLabFTW tags ignore case and turn `|` into a space, so names containing `|`
are rejected; names that differ only in case are still separate containers.
Two writers creating the same container at once can each make an entry.
MetaRelay then uses the one that holds records, else the oldest; only two
entries that both hold records raise `ConflictError` (move the records into
one). An entry whose `metadata.metarelay` was broken by hand raises
`ValidationError` naming the entry, instead of being treated as missing.
Paths longer than 255 characters work, but eLabFTW can't store tags that
long, so their deepest prefixes get no tag. MetaRelay only looks at entries
of the API key's team.

MetaRelay doesn't set categories. A lab that wants them (DSST's prototype
used Behavior, Surgeries, …) can set them in eLabFTW; MetaRelay leaves them
alone.

## Edits made in eLabFTW

The JSON in `metadata.metarelay` is the source of truth. MetaRelay never
takes a value from the tables or fields unless you accept the edit.

At each write, MetaRelay saves a checksum of the entry's body and fields as
eLabFTW saved them. If someone edits either one in eLabFTW, the next write
into that entry raises `ConflictError` instead of overwriting the edit (D7).
That's true for every record in the entry, and even if your code read the
record after the edit. Keep the edit by copying it into the record, or
replace it with `mode="force"`. Forcing rebuilds the whole entry from the
stored records, so it also replaces hand edits to the other records' tables.

The error's `preview_diff` says what changed ([Concepts](concepts.md#when-someone-edited-the-preview)):
each value that differs from what MetaRelay wrote, in the body's tables or
in the eLabFTW fields, with its record ID, so an edit to another record in
the same entry is listed too. The body is read by its text, since eLabFTW's
editor reformats the HTML. The same change made in both places is listed
once. It is complete only if nothing else changed: no text added outside
the tables, no table or heading renamed, no field added or retyped, and the
body and the fields don't disagree about a value.
To keep the edits, `accept_edits` copies them into the record and writes it,
or run `metarelay write --accept-edits` ([Concepts](concepts.md#when-someone-edited-the-preview)).

## Limits

- A write reads the entry and then replaces its body and metadata. eLabFTW
  has no version check, so two writers saving into the same entry at the
  same moment can lose one save. `mode="create"` is best effort.
- eLabFTW's lists leave out metadata, so reading costs one request per entry.
- If a write fails after eLabFTW saved it but before MetaRelay recorded the
  saved checksum, the next write into that entry is a false conflict;
  `mode="force"` clears it.
- Nothing here deletes an entry.

## Errors

HTTP 401 raises `AuthenticationError`, 403 `PermissionDeniedError`, 404
`NotFoundError`, and 400 `ValidationError`. Anything else, and network
failures, raise `BackendUnavailableError`. Messages never include the API key.

## Developing against a local eLabFTW

`compose.yaml` runs eLabFTW 6.0.9 on port 3148 (set `ELABFTW_PORT` to change
it). Seed a user and API key after the first start, and again after
`docker compose down -v`:

```bash
docker compose up -d --wait elabftw
docker compose exec elabftw bin/init db:populate /seed/populate.yml --yes --fast
```

Log in at http://localhost:3148 as `metarelay@example.org` /
`metarelay-dev-password`, or use the API key `metarelay-dev-key`:

```python
config = ElabFTWConfig(token="metarelay-dev-key", api_url="http://localhost:3148/api/v2")
session = connect("elabftw://localhost:3148/experiments", config)
```

The backend's tests run the conformance suite against it:

```bash
export METARELAY_ELABFTW_TEST_API_URL=http://localhost:3148/api/v2
pytest tests/backends/test_elabftw.py
```

CI does the same with the same `compose.yaml`.
