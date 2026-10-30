# MetaRelay — Proposed Plan

**Status:** Proposed · **Owner:** Tech Lead · **Date:** 2026-10-30

MetaRelay is a backend-agnostic Python interface for writing NWB metadata to
electronic lab notebooks and other destinations. This plan covers the NIH/DSST
engagement goals from the kickoff: Goal 1 (MetaRelay — this repo), and the
Chronoxiv-side work in Goals 1c, 2 and 3 that depends on it.

---

## 1. Context

### 1.1 Engagement goals

| Goal | Scope | Where the work lands |
| --- | --- | --- |
| **1a — Multi-backend interface** | One Python interface; user selects a destination and passes NWB metadata. Backends: LabArchives (via `labapi`), eLabFTW (via `elabapi-python`), GitHub, local disk. | MetaRelay |
| **1b — Metadata format** | JSON, Pydantic-validated, covering `ndx-fiber-photometry` and `ndx-microscopy`. Must round-trip cleanly through every backend. | MetaRelay |
| **1c — Demonstrate in Chronoxiv** | Replace Chronoxiv's direct `labapi` dependency with MetaRelay; `labapi` stays as the LabArchives backend. | Chronoxiv |
| **2 — Microscopy modality** | vizarr image viewing, microscopy metadata forms, NWB export. | Chronoxiv (schemas from MetaRelay) |
| **3 — UX/UI** | UX review, design artifacts, consistent model across fiber-photometry and microscopy pages. | Chronoxiv |

Success criteria: PR review by DSST staff, and a runnable demo PIs want to
use. Six-month target, no hard deadline. Chronoxiv is not tied to Streamlit;
a TypeScript/React rebuild is under consideration.

### 1.2 What exists today

Chronoxiv 0.1.0 already contains most of the concepts MetaRelay needs, but
hard-wired to LabArchives. Fifteen files reference `labapi`; the coupling is
concentrated in three modules:

| Chronoxiv module | Already provides | Becomes in MetaRelay |
| --- | --- | --- |
| `metadata_records.py` | Portable record envelope (schema version, record type/ID, context, payload), sync receipts, checksums | Backend-neutral `Record`, `Receipt`, `SyncIndex` |
| `labarchives_metadata.py` | Upsert of JSON attachment + readable preview, checksum-based conflict detection, record read/list, derivative file upload | LabArchives backend's write/read/list/file operations |
| `experiment_structure.py` | Notebook/experiment browsing, idempotent folder/page creation, configuration page | Container browsing and `ensure_container` |
| `labarchives_errors.py` | Error → user-message normalisation | Shared error hierarchy |
| `nwb_metadata.py` | 27 Pydantic models: NDX-aligned building blocks (devices, indicator, viral vector, injection, fiber insertion, subject) plus app workflow models (project, equipment profiles, cohorts, sessions, workspace document, resolved/readiness views) with migrations | Building blocks → `metarelay.schemas`; workflow models stay in Chronoxiv |

Takeaways that shape the design:

1. The record / receipt / conflict model is proven in production — lift it,
   don't reinvent it. Receipts need a backend-neutral locator instead of
   LabArchives entry IDs.
2. Every record write produces two things: the JSON (source of truth) and a
   human-readable preview. Each backend needs an answer for both.
3. Hierarchy matters: notebook → experiment → cohort → subject. The interface
   needs a logical path that each backend maps to its native containers.
4. Both target NDX extensions depend on `ndx-ophys-devices`, so device models
   are a natural shared schema module.
5. Chronoxiv's metadata models mix two layers: NWB/NDX-shaped building
   blocks, and workflow models shaped by the app (manifest paths, equipment
   presets, cohort → subject → session defaults, its workspace document).
   Only the first layer is reusable outside Chronoxiv, so only it moves.

---

## 2. Design principles

- **Library first, UI-agnostic.** Pure Python, synchronous, no UI framework
  in the core. Public API uses plain data (records, receipts, locators), so
  it can sit behind an HTTP service later without redesign.
- **Records are the unit of exchange.** Backends store and retrieve validated
  JSON records; hierarchy and presentation are backend concerns behind one
  interface.
- **Round-trip is the contract.** A backend is done when it passes the shared
  conformance suite.
- **Credentials never live in the library.** Callers supply them via explicit
  config objects.
- **Reuse `labapi` and `elabapi-python` as-is;** contribute upstream when
  blocked.
- **Small, reviewable PRs** sized for DSST review.

---

## 3. Architecture

### 3.1 Package layout

```
src/metarelay/
├── core/        record, receipt, path, session protocol, registry, preview, errors, config
├── backends/    local, labarchives, elabftw, github
├── schemas/     common, devices, fiber_photometry/, microscopy/
├── testing/     conformance suite, in-memory fakes
└── cli.py
docs/            Sphinx site (deployed to GitHub Pages)
```

Optional extras per backend (`metarelay[labarchives]`, `[elabftw]`,
`[github]`, `[nwb]`, `[all]`); the local backend is always available.

### 3.2 Core concepts

| Concept | Role |
| --- | --- |
| **Record** | Validated JSON document with a `schema_id`, schema version, record ID, context (e.g. cohort, subject) and payload. Payload is validated against the schema registry on construction. |
| **Receipt** | Proof of a write: backend name, record key, container locator, backend-specific IDs, checksum, timestamp. Receipts are plain data and drive conflict detection on the next write. |
| **Container / LogicalPath** | A tuple path (e.g. notebook → experiment → cohort → subject) that each backend maps to its native hierarchy. `ensure_container` is idempotent. |
| **Session** | The backend interface: list/ensure containers, write/read/list records, write arbitrary files (derivatives), close. |
| **Write modes** | `create` (fail if exists), `upsert` (default, conflict-checked against prior receipt), `force`. |
| **AuthFlow** | Abstracts "none", "token" and "redirect" login. LabArchives' redirect flow (auth URL → callback code → user session) is the only redirect case; UIs render the challenge, the library completes it. |
| **Destination URI** | `file:///…`, `labarchives://host`, `elabftw://host/api/v2`, `github://org/repo@branch/prefix`. Usable in config files, env, CLI and UI dropdowns alike. |
| **Errors** | One hierarchy (`Configuration`, `Authentication`, `BackendUnavailable`, `NotFound`, `Conflict`, `Validation`, `PermissionDenied`) that every backend translates native exceptions into. |
| **Preview** | HTML and Markdown renderers per schema with a generic fallback, so ELN pages stay human-readable. |

Conflict semantics are lifted directly from Chronoxiv: no prior receipt and a
different remote record under the same ID → conflict; prior receipt present
and remote changed since → conflict; otherwise write only when changed.

### 3.3 Backend mapping

| | Container hierarchy | Record storage | Preview | Conflict token | Test strategy |
| --- | --- | --- | --- | --- | --- |
| **Local** | Directory tree | `{id}.json` + index/history | `{id}.md` sidecar | Content hash | Conformance oracle, always in CI |
| **LabArchives** | Notebook → directory → page | JSON attachment entry with marker | Text entry | Attachment checksum | Fake tree (lifted from Chronoxiv tests) + recorded cassettes; live smoke when credentials exist |
| **eLabFTW** | Category/tags → experiment or resource item | Upload, body block, or `extra_fields` (decide in spike) | Body | Upload checksum | Docker instance from `elabftw-app` in CI |
| **GitHub** | Directory under prefix | `{id}.json` via Contents API | `{id}.md` | Blob `sha` | HTTP mocks in CI; optional live test |

GitHub is metadata-only: derivative uploads are size-capped with a clear
error.

### 3.4 Schemas

- Each schema module exports its Pydantic models, schema ID/version, the NDX
  extension and version it targets, a migration chain, optional `to_nwb`
  builders, and JSON Schema export.
- **Scope boundary.** MetaRelay owns models that map onto NWB / NDX types and
  have `to_nwb` builders. Application workflow models stay in the
  application, which composes MetaRelay's models and registers its own
  record schemas with MetaRelay's registry. Backends move any registered
  record, so round-trip does not depend on where a schema lives.
- **Fiber photometry** is derived from Chronoxiv's NDX-aligned models:
  optical fiber, excitation source, photodetector, optical filter, dichroic,
  indicator, viral vector, injection, fiber insertion, and NWB subject
  fields.
- **Stays in Chronoxiv:** project, equipment profiles, cohorts, Chronoxiv's
  subject/implant/session composition (manifest paths, event timebase,
  channel bindings, unit defaults), the workspace document
  (`NWBMetadataDocument`, schema v4), resolved/readiness views, and the
  legacy migrations. Chronoxiv rebuilds these on MetaRelay's models, keeping
  its current portable record version readable so existing notebooks keep
  working.
- **Microscopy** is new, aligned to `ndx-microscopy`, scoped to the fields
  Goal 2's forms will collect; field list agreed with DSST first. The same
  boundary applies: NDX-shaped models here, form and workflow models in
  Chronoxiv.
- **Shared devices** module aligned to `ndx-ophys-devices` serves both.
- Because MetaRelay's models mirror the BSD-3 NDX specs rather than copying
  Chronoxiv's app code, little GPL-3 code moves across (see D1).
- JSON Schema export also enables non-Python consumers (e.g. AI skills that
  author metadata, raised at kickoff).

### 3.5 Chronoxiv migration strategy

Strangler pattern, app usable after every PR:

1. Swap record/receipt models for MetaRelay's behind a shim; migrate sync
   indexes on load.
2. Reimplement the LabArchives metadata and structure modules over the
   MetaRelay backend, keeping public function signatures so existing tests
   act as the regression suite.
3. Route "Local JSON" mode through the local backend, removing the duplicate
   save path.
4. Replace `labapi` session state and login page with MetaRelay session and
   auth flow; delete the error-translation module.
5. Make the destination picker config-driven and multi-destination.
6. Rebuild Chronoxiv's app models on MetaRelay's schemas; delete the
   duplicated building-block models from `nwb_metadata.py`.
7. Remove `labapi` as a direct dependency; demo against eLabFTW and GitHub
   with no code change.

If the UI moves to React, steps 4–5 happen in the new frontend against a thin
FastAPI layer over MetaRelay; library work is unaffected.

---

## 4. Quality & delivery

- **Tooling:** `uv`, `ruff`, `pytest`; mirrors Chronoxiv conventions.
- **Testing tiers:** unit (fakes, no network) → conformance (local, docker
  eLabFTW, mocked GitHub, fake LabArchives) → live smoke (manual/nightly,
  gated by secrets).
- **CI:** GitHub Actions on 3.12/3.13, eLabFTW service container, docs
  build + deploy, PyPI release on tag.
- **Docs:** Sphinx site under `docs/` with `pydata-sphinx-theme`, matching
  Chronoxiv's docs stack, and MyST so pages are authored in Markdown. Built
  with `-W` (warnings fail the build) and deployed to GitHub Pages by CI on
  every push to `main` (PR builds as a check). Content: quickstart,
  destinations and auth, schema reference, "write a backend" guide, Chronoxiv
  integration. The schema reference is generated from the Pydantic models
  themselves via `autodoc_pydantic`, so it never drifts; the Python API
  reference comes from `autodoc`/`autosummary`. No Node toolchain needed.
- **Versioning:** `0.x` during the engagement; `1.0` when all four backends
  pass conformance and Chronoxiv has switched over.
- **Reporting:** issues labelled by goal; milestones map directly to the
  monthly report's Technical Progress section.

### 4.1 Distribution

One PyPI distribution, `metarelay`, with optional extras — not a family of
packages. The base install carries only `pydantic`/`pydantic-settings` and
works offline with the local backend.

| Install | Adds | Contents |
| --- | --- | --- |
| `metarelay` | — | Core interface, local backend, all schemas (validation and JSON only), CLI |
| `metarelay[labarchives]` | `labapi` | LabArchives backend |
| `metarelay[elabftw]` | `elabapi-python` | eLabFTW backend |
| `metarelay[github]` | `httpx` | GitHub backend |
| `metarelay[nwb]` | `pynwb`, `ndx-*` extensions | `to_nwb` builders (kept out of the base so validating JSON never pulls in HDF5) |
| `metarelay[testing]` | `pytest` | Conformance suite for downstream backends and consumers |
| `metarelay[all]` | everything above | |

Backends register through an entry-point group, so `connect()` on an
uninstalled backend fails with an "install `metarelay[<name>]`" message, and
third parties can ship a backend as their own package without changes to
core.

Chronoxiv depends on `metarelay[labarchives,nwb]` and drops `labapi` as a
direct dependency. It keeps `pynwb` and `ndx-fiber-photometry`, because
`nwb_export.py` assembles NWB files with the recorded data itself; MetaRelay's
`to_nwb` builders cover metadata objects only. A first `0.x`
release is published as soon as the core interface and local backend land so
the Chronoxiv migration can pin a PyPI version rather than a git ref.

Schemas stay inside `metarelay` for now; the module boundary makes a later
split into a standalone `nwb-metadata-schemas` package cheap if other
pipelines want validation without any ELN code. Splitting backends
into separate distributions is not planned — extras give the same
install-time optionality with one version number and one release train.

---

## 5. Open decisions & risks

| # | Item | Recommendation / mitigation | Owner |
| --- | --- | --- | --- |
| D1 | **Licence** — repo is currently CC0; Chronoxiv is GPL-3 | Propose BSD-3-Clause (permissive, GPL-compatible); confirm with DSST | Tech Lead |
| D2 | **Chronoxiv UI stack** — Streamlit vs React | Doesn't block MetaRelay; public API is plain data so an HTTP layer is additive. Decide before Goal 3 design work starts. | Tech Lead, Designer, DSST |
| D3 | **eLabFTW record storage** — uploads vs `extra_fields` vs body | Timeboxed spike; choose on round-trip fidelity and in-app searchability; record as ADR | Dev |
| D4 | **Microscopy field scope** | Agree field list with DSST before schema work | Designer, Dev, DSST |
| D5 | **Hosting / always-on deployment** — ad hoc today; NIH-hosted, interest in always-available internal tool | Design for both; defer deployment work until D2 settles | Tech Lead, DSST |
| D6 | **Schema boundary for 1b** — the scope asks to "refine the metadata format" without saying where schemas live; the JSON Chronoxiv writes today is mostly its app-shaped workspace document | Propose: MetaRelay owns the record envelope and NDX-aligned models (per the scope's reuse preference); Chronoxiv's workflow models and workspace document stay in Chronoxiv (§3.4). Confirm with DSST before 1b schema work starts | Tech Lead, DSST |
| R1 | **LabArchives guest account unconfirmed** | Build fake-first using Chronoxiv's existing mocks; DSST runs live smoke tests; record cassettes once access lands | DSST (access), Dev |
| R2 | **eLabFTW / `elabapi-python` version drift** | Pin tested eLabFTW version in docker; adapter layer isolates generated client | Dev |
| R3 | **Chronoxiv regressions during migration** | Keep public signatures through early steps; Chronoxiv's test suite is the regression gate; DSST reviews each PR | Dev, Tech Lead |
| R4 | **Data scale (5–10 GB, up to tens of GB)** | Out of MetaRelay's path (metadata only); relevant to UI stack and hosting decisions — request HPC node specs and sample data | Tech Lead, DSST |
| R5 | **Repository access for reviewers** | Collect reviewer usernames; grant write on MetaRelay and Chronoxiv forks | DSST |
| R6 | **Adoption** — success depends on PIs liking the tool | Early UX discovery with a day-to-day user; demo-ready builds at each milestone | Designer, DSST |

---

## 6. Milestones

One milestone per goal / sub-goal from the kickoff. Repo bootstrapping lives
in 1a (it is the first thing 1a needs); release and demo live in 1c (the
demonstration is the deliverable). Goal 1 is the first priority; Goals 2 and
3 run in parallel on the Chronoxiv side and depend on 1b (schemas) and 1c
(migration) respectively.

| Milestone | Outcome |
| --- | --- |
| **Goal 1a — Multi-backend interface** | Stable record/receipt/session API; local, LabArchives, eLabFTW and GitHub backends all pass the conformance suite; CLI and docs site live |
| **Goal 1b — Metadata format** | NDX-aligned fiber-photometry schemas derived from Chronoxiv's models; microscopy schemas agreed and delivered; JSON Schema export and `to_nwb` builders; round-trip proven through every backend |
| **Goal 1c — Demonstrate in Chronoxiv** | Chronoxiv routes all ELN operations through MetaRelay, `labapi` removed as a direct dependency; MetaRelay 1.0 released; same workspace demonstrably publishes to all four backends |
| **Goal 2 — Microscopy modality** | vizarr image viewing, microscopy metadata forms backed by MetaRelay schemas, NWB export |
| **Goal 3 — UX/UI** | UX review and prioritised list agreed with DSST; design artifacts for primary flows; improvements delivered incrementally; one coherent application across both modalities |

---

## 7. Issues

Grouped issues under each milestone; each is an epic that spawns a handful of
PRs. Labels: `goal:1a|1b|1c|2|3`, `backend:<name>`, `schema`, `infra`,
`docs`, `chronoxiv` (filed in the Chronoxiv repo).

### Goal 1a — Multi-backend interface
1. **Project foundation** `infra` `docs` — package scaffold and tooling (uv, ruff, pytest), CI matrix, licence decision and contributor docs, Sphinx docs site with GitHub Pages deploy workflow, PyPI trusted publishing with a first `0.x` release once the core interface and local backend land.
2. **Core interface** — record, receipt, sync index, logical path/container, session protocol and write modes, auth flow, error hierarchy, schema registry, destination URIs and `connect()`, preview renderers.
3. **Local backend, conformance suite and CLI** — reference `file://` backend; shared conformance tests and in-memory fake every backend must pass; `metarelay` CLI; quickstart and concepts docs.
4. **LabArchives backend** `backend:labarchives` — redirect auth flow, container browse/ensure, record upsert with preview and conflict logic, derivative files; fake labapi tree and cassette harness; upstream `labapi` contributions as needed.
5. **eLabFTW backend** `backend:elabftw` — storage-mapping spike and ADR; docker test instance in CI; full backend to conformance green.
6. **GitHub backend** `backend:github` — Contents-API backend with `sha` conflict token and size-capped files; mock suite and optional live test.

### Goal 1b — Metadata format
1. **Schema framework** `schema` — base model config, versioning and migration runner, example instances, JSON Schema export with golden-file check, generated schema-reference docs.
2. **Shared device models** `schema` — `ndx-ophys-devices`-aligned module used by both modalities.
3. **Fiber-photometry schemas** `schema` — derive the NDX-aligned models from Chronoxiv (optical fiber, excitation source, photodetector, filter, dichroic, indicator, viral vector, injection, fiber insertion, subject); `to_nwb` builders. Workflow models (project, equipment profiles, cohorts, sessions, workspace document, resolved/readiness views) stay in Chronoxiv.
4. **Microscopy schemas** `schema` — field-scope agreement with DSST; microscope, light source, optical channel, imaging space and session models; `to_nwb` builders.
5. **Round-trip verification** — every schema's examples through every backend; documented versioning policy.

### Goal 1c — Demonstrate in Chronoxiv `chronoxiv`
1. **Adopt MetaRelay models and backend** — swap record/receipt models with index migration; reimplement LabArchives metadata and structure modules over the MetaRelay backend behind existing signatures.
2. **Unify storage modes** — route Local JSON through the local backend; config-driven, multi-destination picker.
3. **Replace session and auth** — MetaRelay session and auth flow on the login page; delete `labapi`-specific error handling; rebuild Chronoxiv's app models on MetaRelay schemas and register its record schemas; remove `labapi` as a direct dependency.
4. **Service layer (conditional on D2)** — FastAPI wrapper over MetaRelay if the UI moves to React.
5. **1.0 release and demo** `docs` `infra` — API freeze review with DSST, "write a backend" guide, PyPI trusted publishing, multi-backend demo script and recording.

### Goal 2 — Microscopy modality `chronoxiv`
1. **Image viewing** — vizarr integration with sample data.
2. **Metadata forms** — microscopy form pages backed by MetaRelay schemas, consistent with the fiber-photometry pages.
3. **NWB export** — microscopy session export and end-to-end test.

### Goal 3 — UX/UI `chronoxiv`
1. **Discovery and UX review** — access and setup, pre-questionnaire and working session, brief report, prioritised improvement list agreed with DSST.
2. **Design system** — wireframes for primary flows (ELN auth, browsing, record curation, directory monitoring, status, NWB packaging, DANDI upload), design tokens, component-library configuration.
3. **Validation** — usability checks on key flows with a few users.
4. **Incremental delivery** — improvements landed via PRs with design QA; shared visual and interaction model across fiber-photometry and microscopy pages.

---

## Appendix — Chronoxiv → MetaRelay mapping

| Chronoxiv today | MetaRelay |
| --- | --- |
| `labapi.Client` auth URL + login | `connect()` + auth flow begin/complete |
| `list_notebook_options`, `list_root_experiment_options` | `list_containers` |
| `materialize_labarchives_organization`, `ensure_labarchives_experiment_structure`, `resolve_metadata_target` | `ensure_container` |
| `publish_metadata_record` | `write_record` |
| `read_metadata_record(s)` | `read_record`, `list_records` |
| `publish_analysis_derivative` | `write_file` |
| `save/load_labarchives_configuration` | `write_record`/`read_record` with a config schema |
| `LabArchivesMetadataConflict`, `labarchives_failure_message` | `ConflictError`, shared error hierarchy |
| `PortableMetadataRecord`, `SyncReceipt`, `LabArchivesSyncIndex` | `Record`, `Receipt`, `SyncIndex` |
