# Architecture

How the code is laid out, what each core file owns, and what happens on one
write. Read [concepts](concepts.md) first for the vocabulary.

## Layout

```
src/metarelay/
├── __init__.py          re-exports metarelay.core: the whole public API
├── cli.py               the metarelay command (ls, mkdir, write, read)
├── core/                backend-neutral: the public API, and it imports no backend
├── backends/
│   ├── local.py         file://, the reference backend
│   └── labarchives.py   labarchives://, built on labapi
└── testing/
    ├── memory.py              an in-memory backend for tests, the smallest backend to read
    ├── conformance.py         the suite every backend must pass (BackendConformance)
    ├── labarchives_fake.py    a fake LabArchives API server, standard library only
    ├── labarchives_smoke.py   a live check DSST runs against real LabArchives
    └── labarchives_cassettes.py  records traffic, and compares the fake with it
```

Backends find each other through entry points, not imports. Each registers
under the `metarelay.backends` group in `pyproject.toml`, keyed by URI
scheme, and `connect()` imports only the one a URI names. So a missing extra
(no `labapi`) only matters to someone who uses that backend, and a
third-party package can add a backend without changing MetaRelay.

## What each file in `core/` owns

| File | Owns |
| --- | --- |
| `backend.py` | The `Backend` protocol, and `connect()`, `begin_login()`, `complete_login()`, `auth_flow()`. Finds and loads the backend for a URI scheme from entry points. Turns every loading failure into `ConfigurationError` |
| `session.py` | The `Session` protocol every backend implements, and `StoredRecord` (a record plus its receipt). Backends don't inherit from it. `isinstance(obj, Session)` checks the methods exist |
| `destination.py` | Parsing and normalizing destination URIs. Refuses a URI with a username or password in it, so credentials never land in a receipt or an error |
| `auth.py` | `AuthFlow` (none, token, redirect) and `LoginChallenge`, the plain data a redirect login passes between its two halves |
| `config.py` | `DestinationConfig` and `TokenConfig`: how credentials reach a backend. A config reads environment variables only under its own prefix, and never echoes a value in an error |
| `record.py` | `Record`, the envelope that moves between destinations. Validates its payload against the schema registry when created |
| `registry.py` | `SchemaRegistry` and `register_schema`. Validates a payload and returns its normal form, the exact JSON a backend saves. Refuses schemas that would drop unknown keys or couldn't read back what they save |
| `model.py` | `MetaRelayModel`, the base of every public model. Turns pydantic's errors into `metarelay.ValidationError`, without input values in the message |
| `path.py` | `LogicalPath` (a tuple of names) and `Container` (a path plus the backend's own ID). Rejects only names unsafe on every backend |
| `checksum.py` | `record_checksum` (SHA-256 of canonical JSON, without `updated_at`) and `preview_checksum` |
| `conflict.py` | `WriteMode`, `WriteAction`, and `decide_write`: the one function that decides write, skip, or `ConflictError`, for every backend |
| `receipt.py` | `Receipt`, `FileReceipt`, and `SyncIndex`, the receipts an application keeps between writes |
| `preview.py` | Readable previews. A renderer turns a record into headings and tables; formatters turn those into HTML and Markdown. Backends store the result and never build markup themselves |
| `errors.py` | The error classes. Every backend translates its native errors into these, with no URL, token, or signature in the message |

Rules that keep the core neutral:

- Conflict rules live only in `decide_write`. A backend works out the
  checksums of what's at the destination, then asks.
- Previews come only from `render_preview`.
- A backend raises only `metarelay` errors.

## One write, from `connect()` to the receipt

This traces the
[`save_metadata` example](../examples/save_metadata/README.md) against a
folder:

```python
session = connect("file:///Users/me/metadata")
receipt = session.write_record(where, record, prior_receipt=prior)
```

1. **`connect(uri)`** (`core/backend.py`) calls `Destination.parse(uri)`
   (`core/destination.py`), which refuses credentials in the URI and
   normalizes it: lowercase scheme and host, no trailing `/`.
2. `load_backend("file")` reads the `metarelay.backends` entry points, finds
   `file = "metarelay.backends.local:LocalBackend"`, imports it, and checks
   it has the `Backend` methods and a valid `auth_flow`.
3. `LocalBackend.auth_flow` is `NONE`, so `connect()` calls
   `LocalBackend.open(destination, config)`. A `REDIRECT` backend such as
   LabArchives would raise `ConfigurationError` here, pointing to
   `begin_login()`.
4. `open()` (`backends/local.py`) checks that the folder exists, resolves
   symlinks, and returns a `LocalSession` whose destination is the folder's
   one canonical URI. Receipts are keyed by that URI, so every spelling of
   the same folder finds the same receipts.
5. Before the write, **`Record(...)`** (`core/record.py`) validated the
   envelope and passed the payload to
   `default_registry.validate_payload` (`core/registry.py`), which stored it
   in normal form.
6. **`session.write_record(where, record, prior_receipt=prior)`** walks to
   the container's folder. A missing folder is `NotFoundError`, since only
   `ensure_container` creates one.
7. It reads what is there now: the record's `.json` and its `.md` preview.
   It computes `record_checksum` of the stored record and `preview_checksum`
   of the stored preview (`core/checksum.py`).
8. **`decide_write`** (`core/conflict.py`) compares those with the new
   record's checksum and with `prior`:
   - nothing there, or `force` mode: write;
   - `create` mode with a record there: `ConflictError`;
   - content other than the new record's or the one `prior` describes:
     `ConflictError`;
   - a preview edited since `prior`: `ConflictError`;
   - otherwise, skip if the content is already the same, or write.

   On `ConflictError` nothing is written.
9. On write, `render_preview(record).markdown` (`core/preview.py`) builds the
   table. The `.json` and `.md` are written atomically, then the folder's
   `.metarelay/` index, preview checksum, and history line are updated.
10. `write_record` reads the record back and returns its **`Receipt`**
    (`core/receipt.py`): destination, container, record ID, schema,
    `checksum`, the backend's `conflict_token` (here the SHA-256 of the
    `.json` file), and `preview_checksum`.
11. The caller keeps it with `SyncIndex.put(receipt)`, keyed by destination,
    path, and record ID, and passes it as `prior_receipt` next time.

LabArchives follows the same steps from step 6, with notebook pages for
folders and entries for files. Its login replaces steps 3 and 4:
`begin_login()` returns a link, and `complete_login()` turns the redirect's
query parameters into a `LabArchivesSession`.
