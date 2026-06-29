"""Offline MPF → MIF 1.0 migration.

Converts an existing **MPF** archive (the legacy Memory Portability Format
envelope, or its JSONL form) into a **MIF 1.0 bundle** (a directory of concept
files + manifest), with no running MNEMOS server required. This is the one-time
migration path for archives produced before the MIF cut-over; live data is
migrated by re-exporting with ``--format mif``.

Usage:
    python -m mnemos.tools.mpf_to_mif --file memories.json  --out ./mif-bundle
    python -m mnemos.tools.mpf_to_mif --file memories.jsonl --out ./mif-bundle
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _memory_from_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Flatten one MPF record to a memory row (kind=='memory' only)."""
    if rec.get("kind") != "memory":
        return None
    payload = dict(rec.get("payload") or {})
    payload.setdefault("id", rec.get("id"))
    return payload


def _load_mpf_memories(path: Path) -> List[Dict[str, Any]]:
    """Read an MPF envelope (JSON) or JSONL file into a list of memory rows."""
    text = path.read_text(encoding="utf-8").strip()
    memories: List[Dict[str, Any]] = []
    if path.suffix.lower() == ".jsonl" or (text and not text.lstrip().startswith("{")):
        # JSONL: one MPF record per line; a trailing {"mpf_sidecars": true,...}
        # trailer (sidecars) is ignored for the concept migration.
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("mpf_sidecars"):
                continue
            mem = _memory_from_record(obj)
            if mem is not None:
                memories.append(mem)
        return memories
    # Single MPF envelope object with a records[] array.
    envelope = json.loads(text)
    for rec in envelope.get("records") or []:
        mem = _memory_from_record(rec)
        if mem is not None:
            memories.append(mem)
    return memories


def convert(mpf_file: str, out_dir: str, *, redact_vault: bool = True) -> Dict[str, Any]:
    """Convert an MPF file to a MIF bundle; returns the bundle manifest."""
    from mnemos.portability import charon as mif_charon

    memories = _load_mpf_memories(Path(mpf_file))
    return mif_charon.export_bundle(memories, Path(out_dir), redact_vault=redact_vault)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mpf_to_mif",
        description="Convert a legacy MPF envelope/JSONL file to a MIF 1.0 bundle.",
    )
    parser.add_argument("--file", required=True, metavar="PATH", help="MPF envelope (.json) or .jsonl file")
    parser.add_argument("--out", required=True, metavar="DIR", help="Output MIF bundle directory")
    parser.add_argument(
        "--include-vault",
        action="store_true",
        help="Emit vault (secret) content instead of redacting it (authorized migrations only).",
    )
    args = parser.parse_args(argv)
    manifest = convert(args.file, args.out, redact_vault=not args.include_vault)
    print(
        f"Converted {manifest['count']} memories: MPF {args.file} "
        f"→ MIF {manifest['mif_version']} bundle {args.out}"
    )


if __name__ == "__main__":
    sys.exit(main())
