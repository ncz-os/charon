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
