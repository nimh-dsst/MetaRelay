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
`ElabFTWConfig` also reads `METARELAY_ELABFTW_TOKEN`,
`METARELAY_ELABFTW_API_URL`, and `METARELAY_ELABFTW_VERIFY_TLS`.

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
reads values back from the tables or fields.

At each write, MetaRelay saves a checksum of the entry's body and fields as
eLabFTW saved them. If someone edits either one in eLabFTW, the next write
into that entry raises `ConflictError` instead of overwriting the edit (D7).
That's true for every record in the entry, and even if your code read the
record after the edit. Keep the edit by copying it into the record, or
replace it with `mode="force"`. Forcing rebuilds the whole entry from the
stored records, so it also replaces hand edits to the other records' tables.

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
