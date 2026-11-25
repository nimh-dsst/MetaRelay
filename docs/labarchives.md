# LabArchives

The `labarchives://` backend writes records into LabArchives notebooks through
[`labapi`](https://github.com/nimh-dsst/labapi).

```bash
pip install 'metarelay[labarchives]'
```

## Logging in

LabArchives uses a browser login, so there is no `connect()`. Use
`begin_login` and `complete_login` instead. This comes from the shared login
helper the [examples](examples.md) use, `examples/login.py`:

<!-- example: login.py#browser-login -->
```python
challenge = begin_login(uri, callback_url, config)
# The person logs in at challenge.auth_url. LabArchives then sends their
# browser to callback_url?email=...&auth_code=..., and those query
# parameters finish the login.
redirected = browser(challenge.auth_url)
params = dict(parse_qsl(urlsplit(redirected).query))
session = complete_login(challenge, params, config)
```

`browser` stands for whatever gets the person to `challenge.auth_url` and
returns the address LabArchives sent them back to. A web app redirects to the
link and serves `callback_url` itself. A terminal script prints the link and
asks the person to paste the address (`ask_in_terminal` in the helper).

`config` is a `LabArchivesConfig` holding the institution's API key pair:

```python
from metarelay.backends.labarchives import LabArchivesConfig

config = LabArchivesConfig(akid="...", akpass="...")
```

### Credentials

`LabArchivesConfig()`, or `None` in place of a config, reads
`METARELAY_LABARCHIVES_AKID`, `METARELAY_LABARCHIVES_AKPASS`, and
`METARELAY_LABARCHIVES_API_URL` for anything not passed:

```sh
# .env
METARELAY_LABARCHIVES_AKID=...
METARELAY_LABARCHIVES_AKPASS=...
```

MetaRelay never looks for a `.env` file on its own. Name it with
`_env_file=`, a supported argument despite the underscore (it comes from
`pydantic-settings`):

```python
config = LabArchivesConfig(_env_file=".env")
```

Variables already set in the environment win over the file. Add `.env` to
your `.gitignore` so the key pair is never committed.

**Moving from `labapi`'s `.env` (ChronoXIV).** `labapi.Client()` loads `.env`
by itself and reads `ACCESS_KEYID`, `ACCESS_PWD`, and `API_URL`. MetaRelay
doesn't accept those names, since they are too generic to be sure they mean
LabArchives. Rename them to `METARELAY_LABARCHIVES_AKID`,
`METARELAY_LABARCHIVES_AKPASS`, and `METARELAY_LABARCHIVES_API_URL`, and pass
the file with `_env_file=`. If `ACCESS_KEYID` is set and the new key isn't,
the `ConfigurationError` names the new variables.

## Opening a notebook

From the
[`open_labarchives_notebook` example](../examples/open_labarchives_notebook/README.md),
with `notebook` a notebook name and `inside` a `LogicalPath` within it
(`ROOT` for the notebook itself):

<!-- example: open_labarchives_notebook/open_labarchives_notebook.py#open-notebook -->
```python
# A LabArchives session's top-level containers are the account's notebooks.
notebooks = [container.name for container in session.list_containers(ROOT)]
if notebook not in notebooks:
    raise NotFoundError(f"No notebook named {notebook!r}. Yours: {', '.join(notebooks)}.")

# Folders and pages inside it. holds_records is True for a page.
where = LogicalPath((notebook, *inside.parts))
containers = session.list_containers(where)

# Records are found by caption, on the page itself or on the
# "MetaRelay records" page of a folder.
records = {stored.record.record_id: stored for stored in session.list_records(where)}
```

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
raise `BackendUnavailableError`. That includes error 4999 ("your request
contained bad information"), which is what LabArchives answers for an ID that
names nothing. It is a general code, so MetaRelay does not read it as "not
found". MetaRelay finds things by name and raises `NotFoundError` itself.

The backend needs `labapi` 1.2.0 or later. LabArchives puts an error inside
the API class's root element (`<tree-tools><error>...`), and earlier versions
don't find the code there, so a bad login would not raise
`AuthenticationError`.

## Edits made in LabArchives

The JSON attachment is the source of truth. If someone changes it, or edits
the table entry, in LabArchives, the next write raises `ConflictError`
instead of overwriting their change (D7). That holds even if they deleted
everything in the table entry (the attachment's caption names the entry), and
even if your code read the record after the edit: a read returns the JSON,
not the table, so its receipt keeps the table's checksum from MetaRelay's
last write, which the caption stores. Keep the edit by copying it into
the record, or replace it with `mode="force"`.

The error's `preview_diff` lists the values that changed in the table entry
([Concepts](concepts.md#when-someone-edited-the-preview)). LabArchives rewrites the HTML it
stores, so MetaRelay reads only the text of headings and table cells. That
is best effort: if LabArchives changes the markup more than that, the list
may be empty or marked incomplete.
To keep the edits, `accept_edits` copies them into the record and writes it,
or run `metarelay write --accept-edits` ([Concepts](concepts.md#when-someone-edited-the-preview)).

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
To log in and open a notebook from your own code instead, run the
[`open_labarchives_notebook` example](../examples/open_labarchives_notebook/README.md)
against the same fake.
Tests use the same fake in-process:
`metarelay.testing.labarchives_fake.FakeLabArchives` and `serve_fake`.

## Checking against real LabArchives (DSST)

The fake is built from reading `labapi`, so DSST checks it against the real
service before the backend is accepted:

```bash
export METARELAY_LABARCHIVES_AKID=... METARELAY_LABARCHIVES_AKPASS=...
python -m metarelay.testing.labarchives_smoke --notebook "<a test notebook>" \
    --record recording.json
```

It writes into a new `MetaRelay smoke tests/run <time>` folder (remove it
afterwards; `labapi` cannot delete) and saves the traffic as a cassette. A
cassette holds no real data: recording redacts secrets and replaces IDs,
timestamps, your name, ORCID, and IP address, share links, and the name of
every notebook, folder, and page the script did not create with fake values.
If LabArchives sends an element the recorder has no rule for, it writes no
cassette and names the element, so nothing passes through unchecked.
Review the cassette anyway, then add it to
`tests/cassettes/labarchives/`. The test suite compares it with the fake
and fails wherever the fake sends something LabArchives doesn't, or answers
the deliberate missing-folder request with a different error code.

Add `--check-edits` to also edit a preview by hand when it pauses: change the
label `second` to `edited`, and nothing else. It checks that MetaRelay
refuses to overwrite the edit, that the conflict names exactly that change,
and that accepting it copies it into the record. LabArchives rewrites stored
HTML, so this is the check that reading edits works on the real thing. The run
also reports whether LabArchives keeps HTML comments in entries. MetaRelay
finds a record's preview through the attachment's caption, so it works either
way; the comment is only a fallback.
