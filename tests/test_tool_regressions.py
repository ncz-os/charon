from __future__ import annotations

import importlib
import builtins
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

from mnemos.tools import docling_import, mpf_validate
from mnemos.tools.memory_import import BaseImporter, JsonImporter, MifImporter
from mnemos.tools import memory_import
from mnemos.tools.adapters import cognee, letta, mem0
from mnemos.tools.adapters._mnemos_import import (
    import_totals_failed,
    new_import_totals,
    normalize_record_for_mnemos,
)

try:
    from mnemos.tools.adapters import graphiti
except ModuleNotFoundError as exc:
    if exc.name != "mnemos.core":
        raise
    core_module = ModuleType("mnemos.core")
    config_module = ModuleType("mnemos.core.config")
    config_module.get_settings = lambda: None
    sys.modules["mnemos.core"] = core_module
    sys.modules["mnemos.core.config"] = config_module
    try:
        graphiti = importlib.import_module("mnemos.tools.adapters.graphiti")
    finally:
        sys.modules.pop("mnemos.core.config", None)
        sys.modules.pop("mnemos.core", None)


class _Response:
    status = 200

    def __init__(self, body=None):
        self.body = body or {"imported": 1, "skipped": 0, "failed": 0}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def test_docling_posts_to_versioned_memories_route(monkeypatch):
    captured = {}

    def _urlopen(request, timeout):
        captured["url"] = request.full_url
        return _Response()

    monkeypatch.setattr(docling_import.urllib.request, "urlopen", _urlopen)
    importer = docling_import.DoclingImporter(endpoint="http://mnemos.example/")
    assert importer._post_memory({"content": "hello"}) is True
    assert captured["url"] == "http://mnemos.example/v1/memories"


def test_docling_main_returns_failure_when_a_post_fails(tmp_path, monkeypatch):
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"%PDF")

    def _failed_import(self, path: Path):
        self.post_failures += 1
        return [{"content": "failed"}]

    monkeypatch.setattr(docling_import.DoclingImporter, "import_file", _failed_import)
    assert docling_import.main(["--file", str(source)]) == 1


def test_docling_main_returns_failure_when_extraction_fails(tmp_path, monkeypatch):
    """An extraction failure (corrupt PDF, unsupported variant) must
    bump extraction_failures and force a nonzero exit so the operator
    sees the failed import instead of an apparently-successful zero-row
    run."""
    from pathlib import Path
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"%PDF")

    def _failed_extraction(self, path: Path):
        self.extraction_failures += 1
        return []

    monkeypatch.setattr(
        docling_import.DoclingImporter, "import_file", _failed_extraction
    )
    assert docling_import.main(["--file", str(source)]) == 1


@pytest.mark.parametrize("adapter", [letta, mem0, cognee, graphiti])
def test_direct_adapter_post_normalizes_non_memory_records(adapter, monkeypatch):
    captured = []

    def _urlopen(request, timeout):
        captured.append(json.loads(request.data))
        return _Response()

    monkeypatch.setattr(adapter.urllib.request, "urlopen", _urlopen)
    envelope = {
        "mpf_version": "0.1.0",
        "source_system": "foreign",
        "source_version": "1",
        "exported_at": "2026-08-10T00:00:00+00:00",
        "records": [{
            "id": "foreign-1",
            "kind": "fact",
            "payload_version": "mpf-0.1",
            "payload": {"statement": "A related fact", "metadata": {}},
        }],
    }

    adapter._post_to_mnemos(envelope, "http://mnemos.example", "token")

    posted = captured[0]["records"][0]
    assert posted["kind"] == "memory"
    assert posted["payload_version"] == "mnemos-3.1"
    assert posted["payload"]["content"] == "A related fact"
    assert posted["payload"]["metadata"]["mpf"]["original_kind"] == "fact"


@pytest.mark.parametrize("adapter", [letta, mem0, cognee, graphiti])
def test_direct_adapter_post_accumulates_record_sidecar_and_kind_failures(adapter, monkeypatch):
    response = {
        "imported": 1,
        "skipped": 0,
        "failed": 2,
        "sidecars_imported": {"kg_triples": 3},
        "sidecars_failed": {"kg_triples": 4},
        "unsupported_kinds": {"fact": 5},
    }
    monkeypatch.setattr(
        adapter.urllib.request,
        "urlopen",
        lambda request, timeout: _Response(response),
    )
    envelope = {
        "mpf_version": "0.1.1",
        "source_system": "foreign",
        "source_version": "1",
        "exported_at": "2026-08-10T00:00:00+00:00",
        "records": [{
            "id": "foreign-1",
            "kind": "memory",
            "payload_version": "mnemos-3.1",
            "payload": {"content": "memory"},
        }],
    }

    totals = adapter._post_to_mnemos(envelope, "http://mnemos.example", "token")

    assert totals["failed"] == 2
    assert totals["sidecars_imported"] == {"kg_triples": 3}
    assert totals["sidecars_failed"] == {"kg_triples": 4}
    assert totals["unsupported_kinds"] == {"fact": 5}
    assert import_totals_failed(totals)


@pytest.mark.parametrize("adapter", [letta, mem0, cognee, graphiti])
def test_adapter_main_returns_nonzero_for_server_reported_failure(adapter, monkeypatch):
    envelope = {
        "mpf_version": "0.1.1",
        "source_system": "foreign",
        "source_version": "1",
        "exported_at": "2026-08-10T00:00:00+00:00",
        "records": [],
        "record_count": 0,
        "kg_triple_count": 0,
        "diagnostics": {},
    }
    totals = new_import_totals()
    totals["sidecars_failed"] = {"kg_triples": 1}
    monkeypatch.setattr(adapter, "_post_to_mnemos", lambda *args, **kwargs: totals)
    monkeypatch.setattr(adapter, "build_envelope", lambda *args, **kwargs: envelope)

    if adapter is cognee:
        monkeypatch.setattr(adapter, "_require_cognee", lambda: None)
    elif adapter is letta:
        monkeypatch.setattr(adapter, "_resolve_mode", lambda *args: "server")
    elif adapter is graphiti:
        source = type("Source", (), {"close": lambda self: None})()
        monkeypatch.setattr(adapter, "_open_backend", lambda args: source)

    assert adapter.main([
        "--post", "http://mnemos.example", "--api-key", "token"
    ]) == 1


def test_non_memory_normalization_does_not_mutate_export_record():
    record = {
        "id": "event-1",
        "kind": "event",
        "payload_version": "mpf-0.1",
        "payload": {"content": "hello", "metadata": {}},
    }
    normalized = normalize_record_for_mnemos(record)
    assert normalized["kind"] == "memory"
    assert record["kind"] == "event"
    assert record["payload"]["metadata"] == {}


def test_cognee_edge_triple_has_stable_importable_shape():
    first = cognee._edge_to_kg_triple("chunk-1", "doc-1", "is_part_of", {"rank": 1})
    second = cognee._edge_to_kg_triple("chunk-1", "doc-1", "is_part_of", {"rank": 1})
    assert first == second
    assert first["id"].startswith("cognee-edge-")
    assert first["subject_id"] == "chunk-1"
    assert first["predicate"] == "is_part_of"
    assert first["object_id"] == "doc-1"
    assert "subject" not in first and "object" not in first


def test_mpf_validator_default_schema_is_packaged_and_validates_v01(tmp_path):
    envelope = tmp_path / "export.json"
    envelope.write_text(json.dumps({
        "mpf_version": "0.1.1",
        "exported_at": "2026-08-10T00:00:00+00:00",
        "records": [],
    }))
    assert Path(mpf_validate.DEFAULT_SCHEMA).is_file()
    assert mpf_validate.main(["--file", str(envelope), "--quiet"]) == 0


def test_mpf_validator_fails_closed_when_jsonschema_is_unavailable(monkeypatch):
    real_import = builtins.__import__

    def _without_jsonschema(name, *args, **kwargs):
        if name == "jsonschema.validators":
            raise ImportError("jsonschema intentionally unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _without_jsonschema)
    env = {
        "mpf_version": "0.1.1",
        "exported_at": "2026-08-10T00:00:00+00:00",
        "records": [],
    }

    assert mpf_validate.validate(env, {"type": "object"}) == [
        "schema validation unavailable: jsonschema intentionally unavailable"
    ]


def test_memory_import_passthrough_counts_sidecar_and_unknown_kind_failures(monkeypatch):
    response = {
        "imported": 1,
        "failed": 2,
        "sidecars_failed": {"memory_versions": 3},
        "unsupported_kinds": {"acme.observation": 4},
    }
    monkeypatch.setattr(
        memory_import.urllib.request,
        "urlopen",
        lambda request, timeout: _Response(response),
    )
    importer = BaseImporter(preserve_metadata=True)
    importer.source_envelope = {"mpf_version": "0.1.1", "records": []}

    assert importer._post_mpf_passthrough([{"content": "one"}]) == (1, 9)


def test_memory_import_main_returns_nonzero_when_importer_reports_failures(
    tmp_path, monkeypatch
):
    source = tmp_path / "memories.json"
    source.write_text('[{"content": "one"}]')
    monkeypatch.setattr(
        JsonImporter,
        "run",
        lambda self: {"imported": 0, "failed": 3, "skipped": 0},
    )

    assert memory_import.main(["json", "--file", str(source)]) == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["json", "--file", "{path}"],
        ["csv", "--file", "{path}", "--content-col", "content"],
        ["chatgpt", "--file", "{path}"],
        ["obsidian", "--vault", "{path}"],
        ["text", "--source", "{path}"],
        ["mif", "--source", "{path}"],
    ],
)
def test_memory_import_main_returns_nonzero_for_missing_local_input(tmp_path, argv):
    missing = tmp_path / "missing"
    resolved = [part.format(path=str(missing)) for part in argv]
    assert memory_import.main(resolved) == 1


def test_memory_import_main_returns_nonzero_for_malformed_json(tmp_path):
    source = tmp_path / "broken.json"
    source.write_text('{"truncated":')
    assert memory_import.main(["json", "--file", str(source)]) == 1


def test_memory_import_empty_valid_json_is_success(tmp_path):
    source = tmp_path / "empty.json"
    source.write_text("[]")
    assert memory_import.main(["json", "--file", str(source), "--dry-run"]) == 0


def test_mif_preserve_metadata_keeps_recovered_fields(monkeypatch, tmp_path):
    portability_module = ModuleType("mnemos.portability")
    charon_module = ModuleType("mnemos.portability.charon")
    charon_module.import_bundle = lambda source: [{
        "id": "mem-original",
        "content": "portable",
        "category": "decisions",
        "namespace": "tenant-a",
        "owner_id": "alice",
        "created": "2026-01-01T00:00:00+00:00",
        "updated": "2026-01-02T00:00:00+00:00",
        "permission_mode": 640,
        "source_provider": "mif-source",
    }]
    portability_module.charon = charon_module
    monkeypatch.setitem(sys.modules, "mnemos.portability", portability_module)
    monkeypatch.setitem(sys.modules, "mnemos.portability.charon", charon_module)

    captured = {}
    monkeypatch.setattr(
        MifImporter,
        "_post",
        lambda self, rows: (captured.setdefault("rows", rows), (len(rows), 0))[1],
    )
    source = tmp_path / "bundle"
    source.mkdir()
    MifImporter(source=str(source), preserve_metadata=True).run()

    row = captured["rows"][0]
    assert row["id"] == "mem-original"
    assert row["owner_id"] == "alice"
    assert row["namespace"] == "tenant-a"
    assert row["created"] == "2026-01-01T00:00:00+00:00"
    assert row["updated"] == "2026-01-02T00:00:00+00:00"
    assert row["permission_mode"] == 640
    assert row["source_provider"] == "mif-source"


# ─── F13: Letta adapter must accept BOTH bare-array and wrapped response shapes
# ─────────────────────────────────────────────────────────────


class _StubLettaClient:
    """Drop-in stand-in for _LettaClient used by _paginated_get tests.

    Only ``_get`` is exercised; it just returns whatever's queued up
    for the next call. The bare-array / wrapped-envelope distinction
    is the whole point of the F13 fix, so the queue contents are
    what actually matter for each test.
    """

    def __init__(self, responses):
        # ``responses`` is a list of bodies; _get pops one per call.
        self._responses = list(responses)
        self.calls = []

    def _get(self, path, params=None):
        self.calls.append((path, params))
        if not self._responses:
            return None
        return self._responses.pop(0)


def test_letta_paginated_get_handles_bare_array_response():
    """Letta documents a bare-array shape on some endpoints/SDK
    versions: ``GET /v1/agents`` returns ``[{...}, {...}]`` directly,
    NOT ``{"agents": [...]}``. The old adapter treated a non-dict
    payload as "no items" and silently exported zero rows. This
    test exercises that shape with real data and asserts the items
    come through.
    """
    body = [
        {"id": "agent-1", "name": "alpha"},
        {"id": "agent-2", "name": "beta"},
        {"id": "agent-3", "name": "gamma"},
    ]
    client = _StubLettaClient([body])
    rows = letta._paginated_get(client, "/v1/agents", {}, result_key="agents")
    assert len(rows) == 3
    assert [r["id"] for r in rows] == ["agent-1", "agent-2", "agent-3"]


def test_letta_paginated_get_handles_wrapped_envelope_response():
    """The wrapped envelope shape (``{"agents": [...], "after": "..."}``)
    must keep working after F13 — backwards compatibility for the
    older callers that still get the envelope back.
    """
    body = {
        "agents": [{"id": "agent-1"}, {"id": "agent-2"}],
        "after": "cursor-page-2",
    }
    client = _StubLettaClient([body, {"agents": [{"id": "agent-3"}]}])
    rows = letta._paginated_get(client, "/v1/agents", {}, result_key="agents")
    assert [r["id"] for r in rows] == ["agent-1", "agent-2", "agent-3"]


def test_letta_paginated_get_raises_on_unrecognised_object_shape():
    """If the response is a JSON object but lacks the ``result_key``
    field (error envelope, schema change, typo), the adapter MUST
    raise rather than silently returning an empty list. "We don't
    understand this response" should never look like "zero items."
    """
    client = _StubLettaClient([{"error": "oops", "detail": "nope"}])
    with pytest.raises(letta._LettaResponseShapeError) as excinfo:
        letta._paginated_get(client, "/v1/agents", {}, result_key="agents")
    # The error should mention what we expected and what we got.
    msg = str(excinfo.value)
    assert "agents" in msg
    assert "/v1/agents" in msg


def test_letta_paginated_get_raises_on_unrecognised_primitive_shape():
    """A scalar / null body is also unrecognised and must raise
    rather than silently empty. This covers the "server returned a
    message we can't parse" case (string, number, null).
    """
    client = _StubLettaClient([None])
    with pytest.raises(letta._LettaResponseShapeError):
        letta._paginated_get(client, "/v1/agents", {}, result_key="agents")

    client = _StubLettaClient(["<html>some non-json thing</html>"])
    with pytest.raises(letta._LettaResponseShapeError):
        letta._paginated_get(client, "/v1/agents", {}, result_key="agents")


def test_letta_paginated_get_stops_after_bare_array_page():
    """The bare-array shape has no documented cursor — there is no
    way to advance — so the loop must NOT keep calling the server
    looking for a ``after``/``cursor`` field. One call, one page,
    done. (Without this guard, a server echoing back a stale
    cursor on every wrapped call would already be bounded by the
    seen_tokens guard; for bare arrays there's nothing to guard
    against, but the loop must still terminate.)"""
    body = [{"id": "agent-1"}, {"id": "agent-2"}]
    client = _StubLettaClient([body])
    rows = letta._paginated_get(client, "/v1/agents", {}, result_key="agents")
    assert len(rows) == 2
    assert len(client.calls) == 1


# ─── F14: Mem0 adapter must not loop forever on legacy nonpaginated get_all()
# ─────────────────────────────────────────────────────────────


class _LegacyMem0Client:
    """Stand-in for the OLD mem0ai ``MemoryClient``: ``get_all()``
    takes no arguments and returns the FULL set every call. Used to
    reproduce the 250-rows-from-100-unique-records bug."""

    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def get_all(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        # Raise TypeError on pagination kwargs (legacy SDK behaviour:
        # the function signature genuinely doesn't accept those names).
        if "page" in kwargs or "page_size" in kwargs:
            raise TypeError(
                "get_all() got an unexpected keyword argument 'page'"
            )
        # Returning the SAME rows every call is the legacy behaviour
        # that causes the original loop bug.
        return self._rows


class _PaginatedMem0Client:
    """Stand-in for the NEWER mem0ai ``MemoryClient``: ``get_all``
    accepts ``page`` and ``page_size`` kwargs and returns only the
    requested slice. Used to verify the paginated path still works
    after F14 and to drive the non-advancing-page backstop."""

    def __init__(self, pages):
        # ``pages``: list of pages, each a list of dicts.
        self._pages = list(pages)
        self.calls = []

    def get_all(self, *args, page=1, page_size=100, **kwargs):
        self.calls.append({"page": page, "page_size": page_size})
        idx = page - 1
        if idx >= len(self._pages):
            return []
        page_rows = self._pages[idx]
        return page_rows[:page_size]


class _EchoingPaginatedMem0Client:
    """A pathological paginated client that always returns the
    SAME page no matter what ``page`` argument is passed. Exercises
    the non-advancing-page backstop."""

    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def get_all(self, *args, page=1, page_size=100, **kwargs):
        self.calls.append({"page": page, "page_size": page_size})
        # Return the same rows regardless of page — broken server.
        return self._rows


def _mem0_iter_with_client(client_instance):
    """Run ``mem0._iter_platform_records`` against a fake
    ``MemoryClient``. The real function calls ``MemoryClient(...)``
    directly, so we monkeypatch the module attribute (which is a
    class) with a callable that returns the supplied fake instance."""
    saved = mem0.MemoryClient

    class _Factory:
        def __new__(cls, *args, **kwargs):
            return client_instance

    mem0.MemoryClient = _Factory
    try:
        rows = list(
            mem0._iter_platform_records(
                "irrelevant", tenancy_axis="owner_id", page_size=100
            )
        )
    finally:
        mem0.MemoryClient = saved
    return rows, client_instance


def test_mem0_legacy_get_all_called_exactly_once():
    """Reproduces the F14 bug: legacy ``get_all()`` returns the
    FULL set every call. The old loop would call it until it ran
    out of pages, yielding 250 rows from 100 unique records. The
    fixed path must call the no-kwargs form ``get_all()`` exactly
    ONCE (yielding 100 records) — no duplicates. The pagination-
    kwargs probe that fires ``TypeError`` is a separate first call
    and is asserted in
    ``test_mem0_legacy_get_all_raises_typeerror_then_stops``."""
    rows_payload = [
        {"id": f"mem-{i}", "memory": f"row {i}"} for i in range(100)
    ]
    client = _LegacyMem0Client(rows_payload)
    rows, _ = _mem0_iter_with_client(client)
    assert len(rows) == 100
    assert [r["id"] for r in rows] == [f"mem-{i}" for i in range(100)]
    # Exactly 1 call to the legacy no-kwargs form — that's the
    # bug fix. (The probe call with pagination kwargs is separate.)
    legacy_calls = [c for c in client.calls if not c[1]]
    assert len(legacy_calls) == 1


def test_mem0_paginated_get_all_walks_pages_until_short_page():
    """Sanity check: the modern paginated path must still work
    after F14. Two full pages followed by a short page should
    yield all three pages' worth of records and then stop on the
    short page, not the backstop.
    """
    page1 = [{"id": f"mem-{i}", "memory": f"row {i}"} for i in range(100)]
    page2 = [{"id": f"mem-{i}", "memory": f"row {i}"} for i in range(100, 200)]
    page3 = [{"id": "mem-200", "memory": "tail"}]  # short page
    client = _PaginatedMem0Client([page1, page2, page3])
    rows, _ = _mem0_iter_with_client(client)
    assert len(rows) == 201
    assert rows[-1]["id"] == "mem-200"


def test_mem0_non_advancing_page_backstop_stops_loop():
    """The non-advancing-page backstop must terminate the loop
    even if a (broken) paginated client keeps returning the same
    100 rows for every page. Without it, the loop would advance
    the page counter forever (or until some unrelated ceiling);
    with it, we stop the second time we see the same IDs."""
    rows_payload = [
        {"id": f"mem-{i}", "memory": f"row {i}"} for i in range(100)
    ]
    client = _EchoingPaginatedMem0Client(rows_payload)
    rows, _ = _mem0_iter_with_client(client)
    # Exactly the records from one page, no duplicates.
    assert len(rows) == 100
    # And we don't keep hammering the server: two calls (page 1
    # accepted, page 2 caught by the backstop). Anything more than
    # a small constant means the backstop isn't doing its job.
    assert len(client.calls) <= 3


def test_mem0_legacy_get_all_raises_typeerror_then_stops():
    """Direct test of the F14 detection contract: when the SDK
    rejects pagination kwargs with ``TypeError`` (i.e. it's a legacy
    nonpaginated SDK), the adapter must call ``get_all()`` EXACTLY
    ONCE on the no-kwargs form and yield the unique rows exactly
    once. This is the integration-level counterpart to the bare
    mock-based test above and exercises the actual TypeError branch
    in the code.
    """
    rows_payload = [
        {"id": f"mem-{i}", "memory": f"row {i}"} for i in range(100)
    ]
    client = _LegacyMem0Client(rows_payload)
    rows, _ = _mem0_iter_with_client(client)
    assert len(rows) == 100
    assert [r["id"] for r in rows] == [f"mem-{i}" for i in range(100)]
    # The first call attempted pagination kwargs and got TypeError.
    # Subsequent calls (if any) would be on the no-kwargs form.
    # Exactly 1 call to the legacy no-kwargs form.
    legacy_calls = [c for c in client.calls if not c[1]]
    assert len(legacy_calls) == 1
    # And only one pagination-kwarg attempt.
    paged_calls = [c for c in client.calls if "page" in c[1]]
    assert len(paged_calls) == 1
