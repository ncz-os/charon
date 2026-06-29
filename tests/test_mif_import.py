"""MIF bundle import via the CHARON CLI tool (`mnemos.tools.memory_import mif`).

Builds a real bundle with the mnemos-core primitive, then imports it with the
network POST captured — verifies the create payloads and that the base type
folds into metadata.mif_type (Phase 2a) so it persists + round-trips.
"""

from __future__ import annotations

from mnemos.portability import charon as mif_charon
from mnemos.tools.memory_import import MifImporter


def _make_bundle(tmp_path):
    memories = [
        {
            "id": "mem_a",
            "content": "MNEMOS adopts MIF.",
            "category": "decisions",
            "namespace": "default",
            "created": "2026-06-28T20:00:00+00:00",
        },
        {
            "id": "mem_b",
            "content": "Restart the gateway.",
            "category": "rules",
            "namespace": "default",
            "created": "2026-06-28T20:01:00+00:00",
            "mif_type": "procedural",
        },
    ]
    out = tmp_path / "bundle"
    mif_charon.export_bundle(memories, out)
    return out


def test_mif_importer_posts_create_payloads(tmp_path, monkeypatch):
    bundle = _make_bundle(tmp_path)
    captured = {}

    def fake_post(self, memories):
        captured["memories"] = memories
        return (len(memories), 0)

    monkeypatch.setattr(MifImporter, "_post", fake_post)
    result = MifImporter(source=str(bundle), endpoint="http://x").run()

    assert result["imported"] == 2
    posted = {m["content"]: m for m in captured["memories"]}
    assert set(posted) == {"MNEMOS adopts MIF.", "Restart the gateway."}
    # every payload carries content + category + namespace
    for m in captured["memories"]:
        assert m["content"] and m["category"] and m["namespace"]
    # the explicit procedural type folded into metadata.mif_type
    proc = posted["Restart the gateway."]
    assert proc["metadata"]["mif_type"] == "procedural"


def test_mif_importer_namespace_override(tmp_path, monkeypatch):
    bundle = _make_bundle(tmp_path)
    captured = {}
    monkeypatch.setattr(MifImporter, "_post", lambda self, mems: (captured.setdefault("m", mems), (len(mems), 0))[1])
    MifImporter(source=str(bundle), namespace="team-b").run()
    assert all(m["namespace"] == "team-b" for m in captured["m"])
