# mnemos-charon

CHARON is the Mnemos portability subsystem — the ferry that brings memory data
**into** Mnemos and lets it back **out** without lock-in. It is a separately
installable `mnemos.*` namespace distribution (PEP 420) that overlays onto
`mnemos-core`.

## What's inside

- **MPF — Memory Portability Format** (`mnemos.domain.portability`): the open
  import/export schema (`vendor/mpf-v0.2.json`), serializers, ID/version
  topology, and the import/export orchestration.
- **MPF API** (`mnemos.api.routes.portability`, `…routes.ingest`): `/v1/export`,
  `/v1/import`, and universal ingest.
- **Migrate-in adapters** (`mnemos.tools.adapters.*`): read foreign memory
  systems — `mem0`, `letta`, `graphiti`, `cognee`, `mempalace` — via their
  on-disk/HTTP formats (stdlib only, no vendor SDKs required) and emit MPF so
  users can migrate **to** Mnemos.
- **Document ingestion** (`mnemos.api.routes.document_import`,
  `mnemos.tools.docling_import`): IBM Docling conversion of PDF/DOCX/HTML into
  memories. Optional — install the `docling` extra.

## Install

```bash
pip install mnemos-core mnemos-charon            # MPF + adapters
pip install "mnemos-charon[docling]"             # + IBM Docling document import
```

CHARON is installed by default in the Mnemos umbrella image and the
`server`/`full` bundles, and is separable for minimal core installs. When the
distribution is present, `mnemos-core` mounts the CHARON routes automatically;
when absent, core boots without them.

## Relationship to core

CHARON depends on `mnemos-core` (one direction only). The SQL/data-access for
the MPF flow stays in core (`mnemos.db.portability_repo` + the persistence
backends own the schema and queries); CHARON owns validation, orchestration,
adapters, and the route surface. The `migrations_charon_trigger_guard.sql`
schema migration is applied by core's migration runner.
