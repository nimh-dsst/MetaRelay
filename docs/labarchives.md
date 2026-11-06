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
| First path name (`"Lab Notebook"`) | A notebook. `ensure_container` creates a missing one |
| Each later name | A folder |
| A container's records | The `Metadata` page inside it, created on the first write |
| A container's files (`write_file`) | The `Files` page inside it |
| One record | A JSON attachment `<record_id>.json`, captioned `MetaRelay record \| <record_id>`, plus a rich-text entry that shows the record as tables |

Pages are never containers. Other entries on the `Metadata` and `Files`
pages are left alone.

## Edits made in LabArchives

The JSON attachment is the source of truth. If someone changes it, or edits
the table entry, in LabArchives, the next write raises `ConflictError`
instead of overwriting their change (D7). Keep the edit by copying it into
the record, or replace it with `mode="force"`.

## Developing without a LabArchives account

`docker compose up` starts a fake LabArchives server on port 8080 (set
`FAKE_LABARCHIVES_PORT` to change it). It speaks the same API as
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
and fails wherever the fake sends something LabArchives doesn't.
