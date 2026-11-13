# ADR 0001: How the eLabFTW backend stores records

- **Status:** Accepted 2026-11-13. Two parts still need confirming with DSST:
  the layout (one entry per session or page, see Decision 2) and the
  direct-edit behavior (D7, see Decision 3).
- **Issue:** #6. Plan §3.3, D3, D7, R2.
- **Tested against:** eLabFTW 6.0.9 (`elabftw/elabimg:6.0.9`) with
  `elabapi-python` 6.0.1, using the dev stack in `compose.yaml`.

## Context

MetaRelay has to put a record (validated JSON) into eLabFTW so that:

1. MetaRelay can read back exactly what it wrote;
2. people can find and read the metadata in eLabFTW without Chronoxiv;
3. a direct edit in eLabFTW is caught, not silently overwritten (D7);
4. the entry body shows the metadata as tables, never only raw JSON (#6).

eLabFTW has no folders, only flat **categories** (one per entry) and
**tags** (many per entry). The shared contract
(`Session.ensure_container(..., holds_records=True)`) already says a
records-only container is an eLabFTW entry.

## Options tested (D3 spike)

We wrote the same record (nested values, a list, a float with
`0.30000000000000004`, `None`, HTML characters, non-Latin text) three ways:

| | Exact round-trip | Plain search | Readable in eLabFTW | Direct edit |
|---|---|---|---|---|
| **Upload** (`<id>.json`) | Yes, byte for byte. eLabFTW's own sha256 matches ours | No | No, it's a file to download | Replacing it adds a new upload; the old one stays |
| **Body block** (JSON in `<pre>`) | Only after unescaping. eLabFTW's sanitizer strips `data-*` attributes and `<tbody>` and rewrites entities | Yes | Yes (tables) | Easy to break the JSON by hand. Body has revision history |
| **`metadata`** (`extra_fields` + a custom key) | Yes. Stored in a MySQL JSON column: key order changes, values are exact, so `canonical_json` round-trips | Only with extended search: `extrafield:**:VTA` | Yes, as form fields | Editing one field keeps our other keys |

Also found:

- The generated client parses `metadata` into a model that **drops custom
  keys**. The adapter has to read the raw response.
- One `PATCH` can set `body` and `metadata` together, on experiments and
  resources.
- Tags: the filter is exact but ignores case, a `|` in a tag becomes a space,
  and the API returns tags joined with `|`.
- `DELETE` is a soft delete (state 3); deleted entries drop out of lists.
- The list endpoint omits `metadata`, so reading records means one `GET` per
  entry.

DSST's earlier prototype (github.com/Python-AI-Solutions/elabftw-app, eLabFTW
5.3.6) stored everything in grouped `extra_fields`. It mapped the Tejeda
Lab's LabArchives folders to categories and cohort/session folders to tags.
It used fixed numbered fields ("Inj 1..5") for repeated items, because
`extra_fields` can't nest.

## Decision

### 1. Each record is written three ways, in one `PATCH`

- **`metadata.metarelay`, the source of truth.** It holds the record JSON
  exactly, keyed by record ID, plus a format version and the container's
  path.
- **The body: the record as tables**, from `render_preview`. Plain search
  finds it, and it meets #6's "tables, never only raw JSON" criterion.
- **`metadata.extra_fields`: the same rows as eLabFTW fields**, grouped
  (`elabftw.extra_fields_groups`) by record and table, as DSST's prototype
  did. These are text fields, because the record JSON is the typed copy.

There is no upload. Writing all three in one request means they can't
disagree after a failed write. LabArchives needs two writes, which is how it
can end up with a duplicate preview.

The conflict token is `record_checksum` of the record in `metadata.metarelay`.

### 2. One entry holds the records of one container

A container made with `holds_records=True` is one entry (an experiment or a
resource, chosen by the destination), like a LabArchives page. That matches
how the lab works: the Tejeda Lab's LabArchives has one page per cohort and
session, and one surgery form covers a whole session (up to 10 mice).
Chronoxiv lets the lab choose page scope (cohort, subject, session, site).

eLabFTW has no folders, so every other container is also an entry, marked as
a folder in `metadata.metarelay`. It holds its own records and has child
containers. A records-only entry can't have children
(`records_only_is_a_leaf = True`).

**How the path appears:** an entry's title is the last name in its path. Its
tags are every prefix of its path. For example, `Behavior/Cohort 1/Day 1` is
tagged `Behavior`, `Behavior/Cohort 1` and `Behavior/Cohort 1/Day 1`.
Clicking a tag in eLabFTW then works like opening a folder. Tags are only for
people: they ignore case, and an entry's tags also match everything below it.
Names containing `|` are rejected, because eLabFTW turns it into a space.

**How MetaRelay finds entries:** two read-only extra fields on every entry,
`MetaRelay path` and `MetaRelay parent`. A strict extended search on one of
them (`extrafield:s:"MetaRelay path":"Study/Cohort A"`) returns the
candidates. That search uses SQL `LIKE`, so it ignores case and treats `%`
and `_` as wildcards: it can return too much, so MetaRelay keeps only entries
whose `metadata.metarelay` path matches exactly (in NFC). A backslash makes
the search match nothing, so it is sent as `_`, which keeps the search from
ever returning too little. MetaRelay doesn't set categories: they are team-wide
settings that a lab manages itself, and DSST's prototype used them for the
kind of record (Behavior, Surgeries). Setting one from the path can be added
later.

**Rejected: one record per entry.** That would mean no rewrites of shared
entries and simpler edit detection. But one surgery day would become dozens
of experiments, unlike any layout the lab uses. To keep this choice
reversible, records stay separate (keyed by ID) inside `metadata.metarelay`
with a format version. Splitting entries later is a mechanical migration.

### 3. A direct edit anywhere in an entry is a conflict (D7)

At each write, MetaRelay saves a checksum of the entry's body and
`extra_fields` *as eLabFTW saved them* in `metadata.metarelay`. Before the
next write into that entry, it compares that checksum with the entry's
current one. If they differ, someone edited the entry in eLabFTW. The write
raises `ConflictError`, and the caller either copies the edit into the record
or writes with `mode="force"`.

So each write rebuilds the whole body and `extra_fields` from the records in
`metadata.metarelay`, with no splicing of sanitized HTML. One cost:
`force` replaces the whole entry's tables and fields, including hand edits to
other records' sections. The error message says so.

### 4. An adapter isolates `elabapi-python` (R2)

Everything that touches the generated client (and raw responses, for
`metadata`) lives in one adapter module. The backend calls it with plain
data. eLabFTW 6.0.9 is pinned in CI and in the dev stack. The client is
`elabapi-python>=6,<7`: 5.x clients fail against it (5.0.0 has no `scope`
parameter, 5.3.0 has a different `patch_experiment`). CI also tests 6.0.0.

## Consequences

- **Lost updates are possible.** A write is read-then-`PATCH`, and eLabFTW has
  no version check, so two writers saving into the same entry at the same
  moment can lose one save. Chronoxiv is single-user, so this should be rare.
  The entry is read at the start of each write and patched right after, and
  `WriteMode.CREATE` stays best effort, as the core already documents.
- **Two writers creating one container** can each make an entry. Lookups
  settle on the entry that holds records, else the oldest, as the LabArchives
  backend does for its pages; only two entries that both hold records are a
  `ConflictError`. The extra empty entry stays (MetaRelay never deletes).
- **An entry whose `metadata.metarelay` is missing or unreadable** (someone
  broke it in eLabFTW's metadata editor) is a `ValidationError` naming the
  entry, not "missing": treating it as missing would create a second entry
  and orphan its records.
- **A write that fails after eLabFTW saved it**, before MetaRelay stored the
  saved checksum, makes the next write into that entry a false conflict;
  `force` clears it.
- **Lookups search the API key's team only** (`scope=2`), so another team's
  MetaRelay entries never match.
- **Reads cost one `GET` per entry**, because lists omit `metadata`: a lookup
  is a search plus one `GET` per match, and listing a container's children
  is one `GET` per child.
- **Tags over 255 characters are skipped**: eLabFTW can't store them. Deep
  paths still work; their deepest prefixes just have no tag.
- **`extra_fields` flatten nested values.** Labels carry the record ID. The
  JSON in `metadata.metarelay` keeps the real structure.
- **A hand edit to a field is visible but not used.** MetaRelay reports it as
  a conflict; it never reads values back from `extra_fields` or the body.
- **To confirm with DSST:** the layout (Decision 2) and the direct-edit
  behavior (Decision 3).
