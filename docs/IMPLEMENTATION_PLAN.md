# MetaRelay Implementation Plan

**Status:** Revised after DSST review · **Owner:** Tech Lead · **Proposed:** October 30, 2026 · **Revised:** November 3, 2026

MetaRelay is a backend-agnostic Python interface for writing NWB metadata to
electronic lab notebooks and other destinations. This plan covers the NIH/DSST
engagement goals from the kickoff: Goal 1 (MetaRelay — this repo), and the
Chronoxiv-side work in Goals 1c, 2 and 3 that depends on it.

### Changes after DSST review

DSST responded to the proposed plan in
[#15](https://github.com/nimh-dsst/MetaRelay/issues/15). This revision
folds in that response:

- **Readable ELN entries are a priority.** Senior users said trainees will not
  read raw JSON. Every ELN backend must render metadata as tables, with visual
  rendering as a stretch goal (§3.2, §3.3, D3).
- **Direct edits in the ELN need a defined behavior.** New decision D7.
- **Microscopy MVP is planar immunostaining,** pending DANDI guidance on the
  target NWB extension (§3.4, D4).
- **New acceptance gates:** existing Chronoxiv workspaces load, migrate, and
  export unchanged (D6); DSST tests LabArchives with real credentials (R1);
  user-workflow checks at each milestone (R3).
- **Deployment discovery starts now,** alongside the UI stack decision,
  instead of after it (D2, D5).
- **Early large-dataset test** in Chronoxiv (R4).
- **eLabFTW pinned to `elabimg` 6.0.9** (R2).
- **License:** BSD-3-Clause, pending final sign-off (D1).

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
| **Preview** | The human-readable view of a record in the ELN entry. Each schema renders as tables (HTML and Markdown), with a generic table renderer as the fallback. Raw JSON is never the only thing a reader sees. Visual rendering (diagrams, device layouts) is a stretch goal. Required for every ELN backend. |

Conflict semantics are lifted directly from Chronoxiv: no prior receipt and a
different remote record under the same ID → conflict; prior receipt present
and remote changed since → conflict; otherwise write only when changed.

The JSON is the source of truth and the preview is regenerated on every
write. Users can still edit an entry directly in the ELN, so each backend
must detect edits made outside MetaRelay instead of silently overwriting
them. The proposed behavior is in D7.

### 3.3 Backend mapping

| | Container hierarchy | Record storage | Preview | Conflict token | Test strategy |
| --- | --- | --- | --- | --- | --- |
| **Local** | Directory tree | `{id}.json` + index/history | `{id}.md` sidecar | Content hash | Conformance oracle, always in CI |
| **LabArchives** | Notebook → directory → page | JSON attachment entry with marker | Rich-text entry with rendered tables | Attachment checksum | Stateful fake LabArchives API server (real `labapi` runs against it) + recorded cassettes; DSST tests with real credentials before acceptance |
| **eLabFTW** | Category/tags → experiment or resource item | Upload, body block, or `extra_fields` (decide in spike) | Body with rendered tables | Upload checksum | Docker instance (`elabftw/elabimg:6.0.9`) in CI |
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
- **Microscopy** is new. The MVP is simple planar immunostaining, chosen by
  DSST from the wide range of microscopy modalities. DSST coordinates field
  discovery with users. `ndx-microscopy` is the working target, but it is
  built mainly for optical physiology, so the target NWB extension is
  confirmed with the DANDI developers
  ([dandi/helpdesk#213](https://github.com/dandi/helpdesk/discussions/213))
  before schema work starts. The same boundary applies: NDX-shaped models
  here, form and workflow models in Chronoxiv.
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

Acceptance for the migration goes beyond Chronoxiv's test suite:

- **Existing work stays usable (D6).** Representative existing workspaces and
  notebooks, supplied by DSST, are loaded, migrated, and checked to confirm
  their meaning and NWB export output are unchanged. These become fixtures in
  Chronoxiv's test suite.
- **User-workflow checks at each milestone (R3).** Open existing work, edit
  and save metadata, authenticate, publish to an ELN, and export NWB.

---

## 4. Quality & delivery

- **Tooling:** `uv`, `ruff`, `pytest`; mirrors Chronoxiv conventions.
- **Testing tiers:** unit (fakes, no network) → conformance (local, docker
  eLabFTW, mocked GitHub, fake LabArchives server) → live smoke (manual/nightly,
  gated by secrets). The LabArchives backend is not accepted until DSST has
  run it against real LabArchives (R1).
- **Dev environment:** `docker compose up` brings up the fake LabArchives
  server and an eLabFTW instance pinned to `elabimg` 6.0.9, so MetaRelay and Chronoxiv can be
  developed and demoed end to end with no ELN credentials. The fake
  implements only the ELN API calls `labapi` makes (`/api/<class>/<method>`),
  keeps a stateful notebook tree, stubs the redirect auth flow, and can
  optionally verify the HMAC `sig`.
- **CI:** GitHub Actions on 3.12, 3.13, and 3.14, eLabFTW service container, docs
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

Updated with DSST's response in
[#15](https://github.com/nimh-dsst/MetaRelay/issues/15).

| # | Item | Decision or mitigation | Owner |
| --- | --- | --- | --- |
| D1 | **License.** The repo is CC0 today; Chronoxiv is GPL-3. | BSD-3-Clause (permissive, GPL-compatible). DSST agrees, pending objections from the other DSST stakeholders. Switch `LICENSE` and package metadata once confirmed, before the first PyPI release. | Tech Lead, DSST |
| D2 | **Chronoxiv UI stack.** Streamlit or React. | MetaRelay development continues independently; its public API is plain data, so an HTTP layer is additive. DSST is open to React. The decision weighs installation, local file access, updates, and ongoing support, not only interface flexibility. Validate one representative workflow with nontechnical users before committing to a rebuild. Decide alongside D5, before Goal 3 design work starts. | Tech Lead, Designer, DSST |
| D3 | **eLabFTW record storage.** Uploads, `extra_fields`, or body. | Timeboxed spike, recorded as an ADR. Criteria: round-trip fidelity, in-app searchability, whether a user can find and understand the metadata in eLabFTW without Chronoxiv, and what happens when someone edits it directly (D7). | Dev |
| D4 | **Microscopy field scope.** | MVP is simple planar immunostaining. DSST coordinates field discovery with users and confirms the target NWB extension with the DANDI developers before schema work starts. | Designer, Dev, DSST |
| D5 | **Hosting and always-on deployment.** Ad hoc today; interest in an always-available, NIH-hosted internal tool. | Deployment discovery runs alongside D2, not after it. Compare a supported local installation with an institution-managed deployment (for example AWS STRIDES for IRP users). Document where data is processed and stored, and involve the responsible NIH IT and security team before choosing a cloud approach. | Tech Lead, DSST |
| D6 | **Schema boundary for 1b.** The scope asks to "refine the metadata format" without saying where schemas live; the JSON Chronoxiv writes today is mostly its app-shaped workspace document. | Agreed: MetaRelay owns the record envelope and NDX-aligned models; Chronoxiv's workflow models and workspace document stay in Chronoxiv (§3.4). Acceptance requires loading, migrating, and checking representative existing workspaces (§3.5). | Tech Lead, DSST |
| D7 | **Direct edits in the ELN.** Users can edit an entry in LabArchives or eLabFTW without going through MetaRelay. | Proposed: the JSON stays the source of truth. A change to the JSON outside MetaRelay is a conflict, as today. Receipts also store a checksum of the rendered preview, so an edit to the preview alone is detected and reported, never silently overwritten. The caller chooses to keep the edit (by editing the record) or replace it (`force`). Confirm with DSST during the eLabFTW spike. | Tech Lead, DSST |
| R1 | **LabArchives guest account unconfirmed.** | Build fake-first against a stateful fake LabArchives API server (§4); the same contract tests run against fake and live. DSST runs a smoke script with real credentials and captures responses as cassettes, and the fake is corrected wherever it disagrees. DSST tests with real credentials before the backend is accepted. | DSST (access), Dev |
| R2 | **eLabFTW and `elabapi-python` version drift.** | Pin eLabFTW to `elabimg` 6.0.9 (includes the PHP fix; next major release) in CI and the dev stack. An adapter layer isolates the generated client. | Dev |
| R3 | **Chronoxiv regressions during migration.** | Keep the app usable after every PR and public signatures stable through early steps. Chronoxiv's test suite is the regression gate, and DSST reviews each PR. At each milestone, check the workflows users depend on: open existing work, edit and save metadata, authenticate, publish, and export NWB. | Dev, Tech Lead, DSST |
| R4 | **Data scale (5 to 10 GB, up to tens of GB).** | Out of MetaRelay's path (metadata only), but central to Chronoxiv's usability and deployment. Test a representative large dataset early in Chronoxiv: viewing, directory monitoring, and NWB export. Record what stays local, what is transferred, temporary disk needs, and what happens when processing is interrupted. Request HPC node specs and sample data. | Tech Lead, Dev, DSST |
| R5 | **Reviewer capacity.** DSST expects one primary reviewer. | Keep PRs small and self-contained so one person can review each in one sitting. DSST identifies additional reviewers where possible. | DSST, Tech Lead |
| R6 | **Adoption.** Success depends on PIs liking the tool. | Early UX discovery with day-to-day users from Prof. Hugo Tejeda's lab; demo-ready builds at each milestone. | Designer, DSST |

---

## 6. Milestones

One milestone per goal / sub-goal from the kickoff. Repo bootstrapping lives
in 1a (it is the first thing 1a needs); release and demo live in 1c (the
demonstration is the deliverable). Goal 1 is the first priority; Goals 2 and
3 run in parallel on the Chronoxiv side and depend on 1b (schemas) and 1c
(migration) respectively.

| Milestone | Outcome |
| --- | --- |
| **Goal 1a — Multi-backend interface** | Stable record/receipt/session API; local, LabArchives, eLabFTW and GitHub backends all pass the conformance suite; ELN entries show metadata as readable tables; LabArchives verified by DSST with real credentials; CLI and docs site live |
| **Goal 1b — Metadata format** | NDX-aligned fiber-photometry schemas derived from Chronoxiv's models; microscopy MVP (planar immunostaining) schemas agreed and delivered; JSON Schema export and `to_nwb` builders; round-trip proven through every backend |
| **Goal 1c — Demonstrate in Chronoxiv** | Chronoxiv routes all ELN operations through MetaRelay, `labapi` removed as a direct dependency; existing workspaces migrate with meaning and export unchanged; user-workflow checks pass; MetaRelay 1.0 released; same workspace demonstrably publishes to all four backends |
| **Goal 2 — Microscopy modality** | vizarr image viewing, microscopy metadata forms backed by MetaRelay schemas, NWB export |
| **Goal 3 — UX/UI** | UX review and prioritised list agreed with DSST; design artifacts for primary flows; improvements delivered incrementally; one coherent application across both modalities |

---

## 7. Issues

Grouped issues under each milestone; each is an epic that spawns a handful of
PRs. Labels: `goal:1a|1b|1c|2|3`, `backend:<name>`, `schema`, `infra`,
`docs`, `chronoxiv` (filed in the Chronoxiv repo).

### Goal 1a — Multi-backend interface
1. **Project foundation** `infra` `docs` — package scaffold and tooling (uv, ruff, pytest), CI matrix, licence decision and contributor docs, Sphinx docs site with GitHub Pages deploy workflow, PyPI trusted publishing with a first `0.x` release once the core interface and local backend land.
2. **Core interface** — record, receipt, sync index, logical path/container, session protocol and write modes, auth flow, error hierarchy, schema registry, destination URIs and `connect()`, table-based preview renderers with a preview checksum on receipts (D7).
3. **Local backend, conformance suite and CLI** — reference `file://` backend; shared conformance tests and in-memory fake every backend must pass; `metarelay` CLI; quickstart and concepts docs.
4. **LabArchives backend** `backend:labarchives` — fake LabArchives API server first (endpoints `labapi` calls, seeded fixtures, stubbed redirect auth) plus the `docker compose` dev stack; confirm `labapi`'s base URL can be overridden (upstream PR if not); redirect auth flow, container browse/ensure, record upsert with preview and conflict logic, derivative files; cassette harness and DSST-run live smoke script; DSST acceptance test with real credentials; upstream `labapi` contributions as needed.
5. **eLabFTW backend** `backend:elabftw` — storage-mapping spike and ADR (D3 criteria, including direct edits per D7); `elabimg` 6.0.9 docker instance in CI and in the dev stack; full backend to conformance green.
6. **GitHub backend** `backend:github` — Contents-API backend with `sha` conflict token and size-capped files; mock suite and optional live test.

### Goal 1b — Metadata format
1. **Schema framework** `schema` — base model config, versioning and migration runner, example instances, JSON Schema export with golden-file check, generated schema-reference docs.
2. **Shared device models** `schema` — `ndx-ophys-devices`-aligned module used by both modalities.
3. **Fiber-photometry schemas** `schema` — derive the NDX-aligned models from Chronoxiv (optical fiber, excitation source, photodetector, filter, dichroic, indicator, viral vector, injection, fiber insertion, subject); `to_nwb` builders. Workflow models (project, equipment profiles, cohorts, sessions, workspace document, resolved/readiness views) stay in Chronoxiv.
4. **Microscopy schemas** `schema` — planar immunostaining MVP; target NWB extension confirmed with DANDI; field-scope agreement with DSST; microscope, light source, optical channel, imaging space and session models; `to_nwb` builders.
5. **Round-trip verification** — every schema's examples through every backend; documented versioning policy.

### Goal 1c — Demonstrate in Chronoxiv `chronoxiv`
1. **Adopt MetaRelay models and backend** — swap record/receipt models with index migration; representative existing workspaces from DSST as migration fixtures; reimplement LabArchives metadata and structure modules over the MetaRelay backend behind existing signatures; run Chronoxiv locally against the MetaRelay dev stack.
2. **Unify storage modes** — route Local JSON through the local backend; config-driven, multi-destination picker.
3. **Replace session and auth** — MetaRelay session and auth flow on the login page; delete `labapi`-specific error handling; rebuild Chronoxiv's app models on MetaRelay schemas and register its record schemas; remove `labapi` as a direct dependency.
4. **Service layer (conditional on D2)** — FastAPI wrapper over MetaRelay if the UI moves to React.
5. **1.0 release and demo** `docs` `infra` — user-workflow checks (R3), API freeze review with DSST, "write a backend" guide, PyPI trusted publishing, multi-backend demo script and recording.

### Goal 2 — Microscopy modality `chronoxiv`
1. **Image viewing** — vizarr integration with sample data.
2. **Metadata forms** — microscopy form pages backed by MetaRelay schemas, consistent with the fiber-photometry pages.
3. **NWB export** — microscopy session export and end-to-end test.

### Goal 3 — UX/UI `chronoxiv`
1. **Stack and deployment discovery** — compare Streamlit and React on installation, local file access, updates, and support (D2); compare a supported local install with an institution-managed deployment, document data locations, and involve NIH IT and security (D5); large-dataset test covering viewing, directory monitoring, and NWB export (R4).
2. **Discovery and UX review** — access and setup, pre-questionnaire and working session, brief report, prioritised improvement list agreed with DSST.
3. **Design system** — wireframes for primary flows (ELN auth, browsing, record curation, directory monitoring, status, NWB packaging, DANDI upload), design tokens, component-library configuration.
4. **Validation** — usability checks on key flows with a few users.
5. **Incremental delivery** — improvements landed via PRs with design QA; shared visual and interaction model across fiber-photometry and microscopy pages.

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
