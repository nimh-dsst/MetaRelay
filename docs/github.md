# GitHub

The `github://` backend writes records as files in a GitHub repository,
through the [Contents API](https://docs.github.com/en/rest/repos/contents).
Labs get a free, versioned, reviewable home for their metadata.

```bash
pip install 'metarelay[github]'
```

## Connecting

```python
from metarelay import connect
from metarelay.backends.github import GitHubConfig

session = connect("github://my-lab/metadata@main/records", GitHubConfig(token="github_pat_..."))
```

The destination is `github://<owner>/<repo>@<branch>[/<prefix>]`:

- The branch must already exist. Write a branch with `/` in its name with
  `%2F`: `github://my-lab/metadata@feature%2Fdemo`.
- Everything MetaRelay writes goes under the prefix (`records` above), or
  the repository root without one. The prefix folder is created by the first
  write.
- Owner and repository are case-insensitive on GitHub, so MetaRelay
  lowercases them: receipts are found however the URI was typed.

The token comes from you, never from MetaRelay: pass it in `GitHubConfig`, or
set `METARELAY_GITHUB_TOKEN`. MetaRelay keeps it in memory for the session
and never writes it to a receipt, log, or file. A fine-grained token needs
**Contents: read and write** on the repository (read-only works for reading).

`GitHubConfig` also takes, or reads from `METARELAY_GITHUB_*`:

| Setting | Default | |
| --- | --- | --- |
| `api_url` | `https://api.github.com` | `https://<host>/api/v3` for GitHub Enterprise Server |
| `max_file_bytes` | 1 MB | The largest file `write_file` accepts, up to 50 MB |
| `timeout` | 30 | Seconds per request |

## Where things go

The same layout as the local backend:

```text
records/                     the prefix
└── Fiber study/             a container: a folder
    ├── implant-M1.json      the record: the source of truth
    ├── implant-M1.md        readable preview; GitHub shows it as tables
    ├── trace.csv            a file from write_file
    └── .metarelay.json      MetaRelay's index (see below)
```

`.metarelay.json` lists which files came from `write_file` (so `notes.json`
isn't mistaken for a record) and each preview's checksum as MetaRelay last
wrote it. Git can't store an empty folder, so `ensure_container` writes this
file into a new folder. If it is deleted, every `.json` file in the folder
counts as a record again.

In a folder that has an index, a `.json` file MetaRelay didn't write and that
isn't a record (a `package.json` at the repository root, say) is left out of
`list_records`. Reading it by name still raises `ValidationError`. Using a
prefix keeps MetaRelay's files apart from the rest of the repository.

Names are matched in Unicode NFC form. A folder or file committed with an
NFD name (some Linux tools do this) is listed but can't be opened yet.

Each changed file is one commit, so writing a record makes up to three
(record, preview, index). Writing a record that hasn't changed makes none.

The record is written first. If the preview or index commit then fails, the
error says the record was saved, and carries the new receipt as
`error.receipt`. Write the record again with that receipt as `prior_receipt`
to bring the preview and index up to date:

```python
try:
    receipt = session.write_record(path, record, prior_receipt=receipt)
except MetaRelayError as error:
    receipt = getattr(error, "receipt", receipt)  # the record may be saved
    raise
```

Names that would break a checkout on macOS or Windows (`\ : * ? " < > |`, a
trailing dot, `CON`) and names that differ only in case from one already
there are refused, as in the local backend. Names starting with `.` are
reserved.

## Conflicts

The conflict token is the record file's blob `sha`. Every write sends the
`sha` MetaRelay read, so if anyone changes the file in between, GitHub
refuses the write and you get `ConflictError` instead of a lost change.
`mode="create"` is enforced the same way: GitHub refuses to create a file
that exists.

Editing the `.md` preview on github.com is also a `ConflictError` on the next
write (D7), even if your code read the record after the edit. Keep the edit
by copying it into the record, or replace it with `mode="force"`.

## Files are size-capped

GitHub is for metadata. `write_file` raises `ValidationError` for files over
`max_file_bytes`, before anything is sent. Keep large data in your data store
and put its location in the record.

## Errors

| GitHub says | MetaRelay raises |
| --- | --- |
| 401 | `AuthenticationError` |
| 403 (rate limit), 429, 5xx, network failure | `BackendUnavailableError` |
| 403 (other), 409 for a protected branch | `PermissionDeniedError` |
| 409 for a stale `sha`, or 422 for a missing one | `ConflictError` |
| Missing repository or branch at `connect()` | `ConfigurationError` |

## Testing

Tests run against `metarelay.testing.github_fake.FakeGitHub`, an in-memory
GitHub API served over real HTTP, so the backend's own `httpx` code runs
unchanged. Run it on its own for manual testing:

```bash
python -m metarelay.testing.github_fake --port 8081 --repo my-lab/metadata --token fake-token
export METARELAY_GITHUB_TOKEN=fake-token METARELAY_GITHUB_API_URL=http://127.0.0.1:8081
```

To run the conformance suite against real GitHub, point it at a sandbox
repository or branch (it commits there and deletes nothing):

```bash
export METARELAY_GITHUB_TOKEN=github_pat_...
export METARELAY_GITHUB_LIVE_DESTINATION=github://my-lab/sandbox@metarelay-ci/live
pytest tests/backends/test_github_live.py
```
