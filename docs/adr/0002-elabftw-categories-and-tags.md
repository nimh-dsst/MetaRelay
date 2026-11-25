# ADR 0002: eLabFTW categories and tags

- **Status:** Accepted. Confirmed by DSST on 2026-11-20. Supersedes two parts
  of [ADR 0001](0001-elabftw-record-storage.md): categories left to the lab,
  and tagging each entry with every prefix of its path.
- **Issue:** [#37](https://github.com/nimh-dsst/MetaRelay/issues/37).
- **Tested against:** eLabFTW 6.0.9 (`elabftw/elabimg:6.0.9`), using the dev
  stack in `compose.yaml`.

## Context

ADR 0001 left categories to the lab and tagged each entry with every prefix
of its path, so clicking a tag worked like opening a folder. DSST then asked
that eLabFTW use categories where ChronoXIV shows the LabArchives notebook
picker. DSST said:

- A category is the class of a set of experiments. Labs create them. A lab
  that groups experiments into LabArchives notebooks would use a category
  where it uses a notebook now.
- Tags are for the kind of record (Surgeries, Behavior, …). DSST's earlier
  prototype did the opposite: it put the kind of record in categories.

## Decision

1. **The first name in a path is a category**, like a LabArchives notebook.
   `Category/Experiment 1/Behavior/M1` is an entry in that category. Every
   entry MetaRelay creates under it gets that category, folder entries and
   records-only entries alike.
2. **The lab creates categories.** MetaRelay never does. `ensure_container`
   with a missing category raises `NotFoundError` naming it, as LabArchives
   does for a missing notebook. Categories are read from the API key's team.
3. **`list_containers(ROOT)` lists the team's category titles.**
   `list_containers("Category")` lists the entries at the top of it. A
   category is not an entry: it holds entries, not records.
4. **Lookups stay on the `MetaRelay path` field.** The path starts with the
   category, so the same name in two categories is two entries.
5. **Moving an entry to another category is not a hand edit.** The edit
   check covers only the body and `extra_fields` (ADR 0001). MetaRelay finds
   the entry by its path and doesn't move it back.
6. **Tags carry the kind of record.** `ElabFTWConfig.record_tags` maps a
   `schema_id` to a tag, and a record write adds that tag to its entry. A
   schema with no tag in the map adds none. ChronoXIV builds the map from
   its per-lab settings.
7. **Path-prefix tags are gone.** Categories and entry titles give the
   navigation they gave. MetaRelay never removes a tag, so tags a person
   adds stay.

## Consequences

- Names may now contain `|`. ADR 0001 rejected it only because tags turn it
  into a space.
- Two categories with the same title are a `ConflictError`, as two
  LabArchives notebooks with the same name are. Titles a path can't hold,
  like `11/12/2026`, are left out of `list_containers(ROOT)`.
- Entries made before this change have no category and a path that starts
  one level higher. MetaRelay doesn't migrate them.
- The dev stack's seed creates a `Fiber study` category for the examples.
  The tests write into a `MetaRelay tests` category, also from the seed.
