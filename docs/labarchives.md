# LabArchives

The `labarchives://` backend writes records into LabArchives notebooks through
[`labapi`](https://github.com/nimh-dsst/labapi).

```bash
pip install 'metarelay[labarchives]'
```

## Logging in

LabArchives uses a browser login, so there is no `connect()`. Use
`begin_login` and `complete_login` instead:

```python
from metarelay import begin_login, complete_login
from metarelay.backends.labarchives import LabArchivesConfig

config = LabArchivesConfig(akid="...", akpass="...")  # the institution's API key pair
challenge = begin_login("labarchives://api.labarchives.com", "https://my.app/callback", config)
# Send the user to challenge.auth_url. LabArchives sends them back to
# https://my.app/callback?email=...&auth_code=...
session = complete_login(challenge, callback_query_params, config)
```

`LabArchivesConfig` also reads `METARELAY_LABARCHIVES_AKID`,
`METARELAY_LABARCHIVES_AKPASS`, and `METARELAY_LABARCHIVES_API_URL`.

## Where things go

| MetaRelay | LabArchives |
| --- | --- |
| First path name (`"Lab Notebook"`) | A notebook. It must already exist: MetaRelay never creates notebooks, so a typo is `NotFoundError` instead of a stray notebook |
| Names in the middle | Folders |
| The last name | A folder, or a page. `ensure_container(path, holds_records=True)` creates a page; without it, a folder |
| Records and files in a page | On that page |
| Records in a folder or notebook | On the `MetaRelay records` page inside it, created on the first write |
| Files (`write_file`) in a folder or notebook | On the `MetaRelay files` page inside it |
| One record | A rich-text entry that shows the record as tables, plus a JSON attachment `<record_id>.json` captioned `MetaRelay record \| <record_id> \| preview <entry ID> \| checksum <sha256>`: it names that entry and records its checksum as of MetaRelay's last write |

Pages made in LabArchives count too: if `Equipment` is a page, records written
to `("Lab Notebook", "Study", "Equipment")` go on it. A page can't contain
anything, so creating a container inside one raises `ValidationError`.
Listings mark pages with `Container.holds_records`.

If a folder and a page in the same place share a name (a cohort folder named
`Equipment` next to the `Equipment` page, say), records use the page and
container operations use the folder. Two folders, or two pages, with the
same name are ambiguous and raise `ConflictError`; rename one.

The `MetaRelay records` and `MetaRelay files` pages belong to their folder,
so they aren't listed as containers and can't be created with
`holds_records=True`. Other entries on any page are left alone.

Records Chronoxiv wrote before it used MetaRelay (`ChronoXIV metadata | ...`
captions) are a different format and aren't read here. Chronoxiv's own
migration converts them (plan D6, Goal 1c).

## Errors

LabArchives error 4547 (not the notebook's owner) raises
`PermissionDeniedError`; bad credentials and expired sessions raise
`AuthenticationError`. Any other LabArchives error, and network failures,
raise `BackendUnavailableError`. LabArchives' not-found codes aren't
documented yet, so they land there too until the smoke test captures one.

## Edits made in LabArchives

The JSON attachment is the source of truth. If someone changes it, or edits
the table entry, in LabArchives, the next write raises `ConflictError`
instead of overwriting their change (D7). That holds even if they deleted
everything in the table entry (the attachment's caption names the entry), and
even if your code read the record after the edit: a read returns the JSON,
not the table, so its receipt keeps the table's checksum from MetaRelay's
last write, which the caption stores. Keep the edit by copying it into
the record, or replace it with `mode="force"`.

## Developing without a LabArchives account

`docker compose up` starts a fake LabArchives server on port 8080 (set
`FAKE_LABARCHIVES_PORT` to change it, and use the same port in
`METARELAY_LABARCHIVES_API_URL` and the `labarchives://` URI below). It speaks the same API as
LabArchives for everything `labapi` uses, and its login page logs you in as
`demo@example.org` immediately:

```bash
docker compose up -d
export METARELAY_LABARCHIVES_AKID=fake-akid METARELAY_LABARCHIVES_AKPASS=fake-akpass
export METARELAY_LABARCHIVES_API_URL=http://localhost:8080
python -m metarelay.testing.labarchives_smoke --uri labarchives://localhost:8080 \
    --notebook "Test notebook" --auto-login
```

The starting notebooks come from `docker/fake-labarchives/seed.json`.
Tests use the same fake in-process:
`metarelay.testing.labarchives_fake.FakeLabArchives` and `serve_fake`.

## Checking against real LabArchives (DSST)

The fake is built from reading `labapi`, so DSST checks it against the real
service before the backend is accepted:

```bash
export METARELAY_LABARCHIVES_AKID=... METARELAY_LABARCHIVES_AKPASS=...
python -m metarelay.testing.labarchives_smoke --notebook "<a test notebook>" \
    --record labarchives-live.json
```

It writes into a new `MetaRelay smoke tests/run <time>` folder (remove it
afterwards; `labapi` cannot delete) and saves the traffic as a cassette, with
secrets redacted. Review the cassette, then add it to
`tests/cassettes/labarchives/`. The test suite compares it with the fake
and fails wherever the fake sends something LabArchives doesn't, or answers
the deliberate missing-folder request with a different error code.

Add `--check-edits` to also edit a preview by hand when it pauses, which
checks that MetaRelay refuses to overwrite edits made in LabArchives. The run
also reports whether LabArchives keeps HTML comments in entries. MetaRelay
finds a record's preview through the attachment's caption, so it works either
way; the comment is only a fallback.
