# mnemos-charon

CHARON is the Mnemos portability subsystem — the ferry that brings memory data
**into** Mnemos and lets it back **out** without lock-in. It is a separately
installable `mnemos.*` namespace distribution (PEP 420) that overlays onto
`mnemos-core`.

## What's inside

- **MIF 1.0 — the portability format.** The native export/import format is the
  [Memory Interchange Format](https://mif-spec.dev) (MIF 1.0). A MIF bundle is a
  directory of `<conceptType>/<uuid>.md` concept files (Markdown body + JSON-LD
  front matter) plus a `mif-manifest.json`. The MIF mapping and bundle
  read/write primitives live in **`mnemos.portability` in `mnemos-core`**
  (`mnemos.portability.charon` → `export_bundle` / `import_bundle`); CHARON
  drives them from the CLI and route surface.
- **MIF export/import CLI** (`mnemos.tools.memory_export`,
  `mnemos.tools.memory_import` — the `mif` subcommand). Surfaced through the
  core CLI as `mnemos export --format mif <dir>` and
  `mnemos import <dir> --from mif`.
- **MPF — Memory Portability Format (legacy)** (`mnemos.domain.portability`):
  the older schema-versioned JSON envelope (`vendor/mpf-v0.2.json`), serializers,
  ID/version topology, and import/export orchestration. MPF is retired as the
  *preferred* emit format in favour of MIF, but remains supported — and is still
  **read** as a migration source. The `/v1/export`, `/v1/import`, and universal
  ingest routes (`mnemos.api.routes.portability`, `…routes.ingest`) live here.
- **MPF → MIF migration** (`mnemos.tools.mpf_to_mif`): an offline tool that
  converts an existing MPF dump into a MIF 1.0 bundle, so deployments on the
  legacy format graduate to MIF without a live re-export.
- **Migrate-in adapters** (`mnemos.tools.adapters.*`): read foreign memory
  systems — `mem0`, `letta`, `graphiti`, `cognee`, `mempalace` — via their
  on-disk/HTTP formats (stdlib only, no vendor SDKs required) so users can
  migrate **to** Mnemos.
- **Document ingestion** (`mnemos.api.routes.document_import`,
  `mnemos.tools.docling_import`): IBM Docling conversion of PDF/DOCX/HTML into
  memories. Optional — install the `docling` extra.

## Install

```bash
pip install mnemos-core mnemos-charon            # MIF + MPF + adapters
pip install "mnemos-charon[docling]"             # + IBM Docling document import
```

CHARON is installed by default in the Mnemos umbrella image and the
`server`/`full` bundles, and is separable for minimal core installs. When the
distribution is present, `mnemos-core` mounts the CHARON routes automatically;
when absent, core boots without them.

## Relationship to core

CHARON depends on `mnemos-core` (one direction only). The MIF 1.0 mapping and
bundle primitives live in **core** (`mnemos.portability`), as do the SQL/data
access for the portability flow (`mnemos.db.portability_repo` + the persistence
backends own the schema and queries). CHARON owns the MPF schema and
orchestration, the MIF/MPF CLI tooling (`mnemos.tools.memory_export` /
`memory_import` / `mpf_to_mif`), the migrate-in adapters, document ingestion,
and the route surface. The `migrations_charon_trigger_guard.sql` schema
migration is applied by core's migration runner.

> A MIF-native REST portability surface (`/v1/export`, `/v1/import` emitting and
> accepting MIF bundles directly) is a follow-up; today those routes speak MPF
> and the MIF path is exercised through the CLI primitives.


## Build infrastructure & partners

Continuous integration and package distribution for this project are generously
supported by our open-source infrastructure partners:

- **[GitLab](https://gitlab.com/)** — canonical source hosting and CI pipelines
  (format / lint / test gates), via the
  [GitLab for Open Source](https://about.gitlab.com/solutions/open-source/) program.
- **[Buildkite](https://buildkite.com/)** — CI/CD orchestration with hosted macOS
  and Linux agents, and our APT package registry host
  (`packages.buildkite.com/ncz-os/ncz`), via the
  [Buildkite Open Source](https://buildkite.com/pricing) program.

Thank you to both for backing open-source software.
