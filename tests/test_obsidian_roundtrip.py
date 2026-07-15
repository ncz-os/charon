"""Obsidian <-> MIF bidirectional adapter round-trip tests.

Self-contained: exercises vault_to_mif (import) and mif_to_vault (export)
without a live MNEMOS, and asserts the vault -> MIF -> vault round-trip
preserves content, [[wikilinks]], frontmatter, folder layout, and emits MIF
units with the required Container-Profile shape.
"""
from __future__ import annotations

from pathlib import Path

from mnemos.tools.adapters import obsidian as obs

_REQUIRED = ("@context", "@type", "@id", "conceptType", "content", "created")


def _make_vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "Concepts").mkdir(parents=True)
    (v / "Journal").mkdir(parents=True)
    (v / "Concepts" / "MIF.md").write_text(
        "---\ntags: [spec, format]\ncreated: 2026-06-25\n"
        "aliases: [Modeled Information Format]\n---\n"
        "MIF is a vendor-neutral memory interchange format. See [[Container Profile]].\n"
    )
    (v / "Journal" / "2026-01-15.md").write_text(
        "---\ntags: [incident]\n---\n"
        "Debugged the Phoenix rate spike. Ref [[MIF]].\n"
    )
    return v


def test_import_emits_valid_mif_units(tmp_path):
    corpus = obs.vault_to_mif(_make_vault(tmp_path))
    assert corpus["@type"] == "MemoryCorpus"
    mems = [r["payload"] for r in corpus["records"] if r["kind"] == "memory"]
    assert len(mems) == 2
    for m in mems:
        for field in _REQUIRED:
            assert field in m, f"missing required {field}"
        assert m["@id"].startswith("urn:mif:")
        assert m["conceptType"] in ("semantic", "episodic", "procedural")


def test_typing_and_relationships(tmp_path):
    corpus = obs.vault_to_mif(_make_vault(tmp_path))
    by_title = {r["payload"]["title"]: r["payload"] for r in corpus["records"]}
    assert by_title["MIF"]["conceptType"] == "semantic"        # concept note
    assert by_title["2026-01-15"]["conceptType"] == "episodic"  # journal/date note
    # [[wikilinks]] became MIF relationships
    rels = by_title["MIF"].get("relationships") or []
    assert any(r["type"] == "links-to" for r in rels)
    assert any(r["target"].startswith("urn:mif:") for r in rels)


def test_wikilink_target_resolves_to_linked_note_id(tmp_path):
    corpus = obs.vault_to_mif(_make_vault(tmp_path))
    by_title = {r["payload"]["title"]: r["payload"] for r in corpus["records"]}
    mif_note, journal_note = by_title["MIF"], by_title["2026-01-15"]

    # The Journal note's [[MIF]] link must resolve to the actual MIF note's
    # @id, not a hash of the link text "MIF" (which differs from the note's
    # own path-derived @id whenever the note lives in a subfolder).
    journal_rels = journal_note.get("relationships") or []
    mif_link = next(r for r in journal_rels if r["metadata"]["obsidian"]["wikilink"] == "MIF")
    assert mif_link["target"] == mif_note["@id"]

    # A dangling link (no note titled "Container Profile" exists) still
    # falls back to a stable placeholder rather than resolving to anything.
    mif_rels = mif_note.get("relationships") or []
    dangling = next(r for r in mif_rels if r["metadata"]["obsidian"]["wikilink"] == "Container Profile")
    assert dangling["target"] not in (journal_note["@id"], mif_note["@id"])


def test_roundtrip_preserves_content_and_layout(tmp_path):
    corpus = obs.vault_to_mif(_make_vault(tmp_path))
    out = tmp_path / "rebuilt"
    n = obs.mif_to_vault(corpus, out)
    assert n == 2
    files = {p.relative_to(out).as_posix(): p.read_text() for p in out.rglob("*.md")}
    # folder layout preserved (vault_path round-trip)
    assert "Concepts/MIF.md" in files
    assert "Journal/2026-01-15.md" in files
    blob = "".join(files.values())
    # content preserved
    assert "vendor-neutral memory interchange format" in blob
    assert "Phoenix rate spike" in blob
    # wikilink preserved
    assert "[[Container Profile]]" in blob
    # frontmatter (aliases) preserved
    assert "Modeled Information Format" in blob


def test_empty_vault(tmp_path):
    v = tmp_path / "empty"
    v.mkdir()
    corpus = obs.vault_to_mif(v)
    assert corpus["records"] == []
    assert obs.mif_to_vault(corpus, tmp_path / "out") == 0
