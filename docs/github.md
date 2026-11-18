# GitHub

The `github://` backend writes records as files in a GitHub repository,
through the [Git Data API](https://docs.github.com/en/rest/git). Each write
is one commit. Labs get a free, versioned, reviewable home for their metadata.

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

## Opening a folder

The [`open_github_repo` example](../examples/open_github_repo/README.md) lists
the folders inside a path and loads a record from it:

<!-- example: open_github_repo/open_github_repo.py#open-folder -->
```python
# Containers are folders in the repository, under the URI's prefix.
containers = session.list_containers(where)

# Each record is a .json file, with a .md preview beside it that GitHub
# shows as tables.
records = {stored.record.record_id: stored for stored in session.list_records(where)}
```

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

A `.json` file MetaRelay didn't write and that isn't a record (a
`package.json` at the repository root, say) is left out of `list_records`,
even in a folder MetaRelay hasn't written to. Reading it by name still raises
`ValidationError`, and `write_file` can update it. Using a prefix keeps
MetaRelay's files apart from the rest of the repository.

Names are matched in Unicode NFC form. A folder or file committed with an
NFD name (some Linux tools do this) is listed, but opening it or writing to
it raises `ValidationError` until it is renamed, rather than creating a
second folder that looks the same.

A folder can hold any number of files. `list_records` downloads every record
in the folder, one request each, so a listing of hundreds of records takes a
while and counts against GitHub's rate limit (5,000 requests an hour). Use
`read_record` when you know the ID.

Each write is one commit: a record, its preview, and the index change land
together, and a file from `write_file` lands with its line in the index.
There is no half-written state: a write lands whole or not at all. Rarely,
the connection drops after GitHub took the commit, so the write raises
`BackendUnavailableError` but did land; writing again is safe and makes no
second commit. Writing a record that hasn't changed makes no commit.

Names that would break a checkout on macOS or Windows (`\ : * ? " < > |`, a
trailing dot, `CON`) and names that differ only in case from one already
there are refused, as in the local backend. So is a prefix in the URI with
such a name, as a `ConfigurationError`. Names starting with `.` are
reserved.

## Conflicts

The conflict token is the record file's blob `sha`. A write reads the
branch's latest commit, checks the record and preview there, and makes its
commit on top of that one. GitHub moves the branch to it only as a fast
forward, so a write never lands on a change MetaRelay didn't see.

When another commit lands on the branch at the same moment (two people
writing at once, even to different files), MetaRelay reads the branch again
and retries, a few times, waiting a little longer each time. A retry keeps
the other commit. If that commit changed the record or its preview, the
write raises `ConflictError` instead. `mode="create"` raises `ConflictError`
if the record exists, even if it was created during the write.

Editing the `.md` preview on github.com is also a `ConflictError` on the next
write (D7), even if your code read the record after the edit. The error's
`preview_diff` lists the values that changed. Keep the edits with
`accept_edits`, which copies them into the record and writes it, or
`metarelay write --accept-edits`
([Concepts](concepts.md#when-someone-edited-the-preview)); or replace them
with `mode="force"`.

## Files are size-capped

GitHub is for metadata. `write_file` raises `ValidationError` for files over
`max_file_bytes`, before anything is sent. Keep large data in your data store
and put its location in the record.

## Errors

| GitHub says | MetaRelay raises |
| --- | --- |
| 401 | `AuthenticationError` |
| 403 (rate limit), 429, 5xx, network failure | `BackendUnavailableError` |
| 422 because other commits kept moving the branch (after retries) | `BackendUnavailableError` |
| 403 (other), 422 for a protected branch | `PermissionDeniedError` |
| The record or preview changed during the write | `ConflictError` |
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
