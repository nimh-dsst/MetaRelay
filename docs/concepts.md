# Concepts

MetaRelay moves **records** to **destinations**. The ideas below are the
same for every backend.

## The pieces

| Concept | What it is |
| --- | --- |
| **Record** | A JSON document: `schema_id`, `schema_version`, `record_id`, `context` and `payload`. The payload is checked against its schema when the record is created. |
| **Schema** | A Pydantic model registered under an ID and version, for example `fiber_photometry.implant` v1. It must set `extra="forbid"` or `"allow"`, so unknown keys are never dropped silently. |
| **Destination** | A URI that picks the backend: `file:///…`, `labarchives://…`, `elabftw://…`, `github://…`. |
| **Session** | An open connection to one destination. `connect(uri)` returns one. |
| **Container** | Where records live, named by a **LogicalPath** such as `("Fiber study", "Cohort A")`. Each backend maps it to what it has: folders, notebook pages, tags. Only `ensure_container` creates one; listing, reading or writing a missing container raises `NotFoundError`, so a typo isn't mistaken for an empty container. `ensure_container(path, holds_records=True)` asks for a container that holds only records (a notebook page where the backend has one). |
| **Receipt** | What MetaRelay knows about a record after writing or reading it, including its checksum. Keep receipts (in a `SyncIndex`) to detect conflicts next time. |
| **Preview** | A readable table of the record, stored next to it, so people never have to read raw JSON. |

## Writing a record

```
             ┌────────────── your receipt from last time
             ▼
  record ──► write_record ──► compare with what's at the destination now
                                   │
          ┌────────────────────────┼─────────────────────────┐
          ▼                        ▼                         ▼
   nothing there,          same as your record,       changed by someone
   or only your own        nothing to do              else since your
   earlier version                                    receipt
          │                        │                         │
        WRITE                    SKIP                 ConflictError
```

## Write modes

| Mode | If the record already exists |
| --- | --- |
| `upsert` (default) | Update it, unless it changed at the destination since your receipt. Without a receipt, only an identical record is accepted. |
| `create` | Always `ConflictError`. |
| `force` | Overwrite it. Use only after the user chose to. |

Edits made directly to the preview (in the notebook entry) also count as a
change at the destination, so they are not overwritten silently.

## Errors

Every backend raises the same errors, all subclasses of `MetaRelayError`:

| Error | Means |
| --- | --- |
| `ConfigurationError` | Bad URI or config, or the backend's extra isn't installed |
| `AuthenticationError` | Login failed |
| `PermissionDeniedError` | Logged in, but not allowed |
| `BackendUnavailableError` | Couldn't reach the destination |
| `NotFoundError` | No such container or record |
| `ConflictError` | The write would overwrite someone else's change |
| `ValidationError` | A record, payload or name is invalid |

## Backends and the conformance suite

A backend is done when it passes the conformance suite. It ships in
`metarelay[testing]`, so a backend in another package can run the same
checks:

```python
import pytest
from metarelay import connect
from metarelay.testing import BackendConformance


class TestMyBackend(BackendConformance):
    @pytest.fixture
    def session(self):
        session = connect("mybackend://test-server")
        yield session
        session.close()
```

`metarelay.testing.MemorySession` is an in-memory backend for testing code
that uses MetaRelay without touching disk.

## The local backend

`file:///path/to/folder` stores each container as a folder. The folder must
already exist.

- `{id}.json` is the record and the source of truth; `{id}.md` is its preview.
- `.metarelay/index.json` lists which files are records. Delete it, and it is
  rebuilt from the `.json` files.
- `.metarelay/history.jsonl` gets one line per write.
- Names that are unsafe on any common filesystem are rejected everywhere
  (`\ : * ? " < > |`, a trailing dot or space, `CON`, `NUL`, …). Names
  starting with `.` are reserved.
- Names are compared ignoring case and Unicode spelling, on every OS. A name
  that matches an existing one only that way (`study` next to `Study`, or
  record `Implant-M1` next to `implant-M1`) is refused, so a folder behaves
  the same on macOS, Windows and Linux.
- Every `.json` in a container is a record, except files written with
  `write_file`. A `.json` that isn't a valid record makes listing fail with
  an error naming the file, rather than the record silently disappearing.
- The same folder always gets the same destination URI (`lab data` and
  `lab%20data`, or a symlinked path, are one destination), so receipts match.
- `create` is enforced even with two writers. Other modes assume one writer
  per folder at a time.
