#!/usr/bin/env python3
"""
tools/adapters/obsidian.py — CHARON bidirectional Obsidian <-> MIF adapter.

Unlike the other adapters (import-only: source -> MIF), Obsidian is
**bidirectional** — MNEMOS can both IMPORT an Obsidian vault and EXPORT its
memories back out as a vault. Both directions go through **MIF** (the Container
Profile), not Docling: an Obsidian vault is already Markdown + YAML frontmatter +
[[wikilinks]], so the MIF path is a clean, lossless round-trip; routing through
DoclingDocument would add an extra, lossy hop.

    IMPORT:  Obsidian vault  --vault_to_mif-->  MIF corpus  -->  MNEMOS /v1/import
    EXPORT:  MNEMOS memories  -->  MIF units  --mif_to_vault-->  Obsidian vault

Typing (cognitive triad): a note in a journal/daily folder (or a date-named
note) -> episodic; any other note -> semantic. [[wikilinks]] <-> MIF
relationships[]. Obsidian-specific fields survive under each memory's
`extensions.obsidian`, so a vault -> MIF -> vault round-trip restores the
original frontmatter and folder layout.

Usage:
    # import a vault into MNEMOS
    python -m mnemos.tools.adapters.obsidian import --vault ~/MyVault \
        --post http://mnemos:5002 --api-key $TOKEN
    # export MNEMOS memories to a vault
    python -m mnemos.tools.adapters.obsidian export --vault ~/Exported \
        --endpoint http://mnemos:5002 --api-key $TOKEN
    # offline round-trip (no MNEMOS): vault -> corpus -> vault
    python -m mnemos.tools.adapters.obsidian import --vault ~/MyVault --out v.corpus.json
    python -m mnemos.tools.adapters.obsidian export --vault ~/Rebuilt --mif v.corpus.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

MIF_CONTEXT = "https://mif-spec.dev/schema/context.jsonld"
MIF_VERSION = "1.2.2"
SOURCE_SYSTEM = "obsidian"
OBSIDIAN_NS = uuid.UUID("2f9c7a54-6b1d-5e83-9a20-3c8f4d6e1b7a")

_FRONT_RE = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*\n", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_INLINE_TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9_][A-Za-z0-9_/-]*)")
_DATE_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_JOURNAL_DIRS = ("daily", "daily notes", "daily-notes", "journal", "journals", "diary")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat()


# ─── shared helpers ──────────────────────────────────────────────────────────

def _iso(value: Any) -> Optional[str]:
    if not value:
        return None
    s = str(value)
    return s if ("T" in s or re.match(r"^\d{4}-\d{2}-\d{2}", s)) else None


def _require_http(url: str) -> str:
    """Reject non-http(s) endpoints. The MNEMOS endpoint is operator-supplied
    (CLI --post/--endpoint); guard against file://, ftp://, etc. so a
    misconfigured endpoint cannot become an arbitrary-file read via urllib."""
    import urllib.parse as _u
    if _u.urlparse(url).scheme not in ("http", "https"):
        raise SystemExit(f"refusing non-http(s) MNEMOS endpoint: {url!r}")
    return url


def _ns_component(text: Any) -> str:
    c = re.sub(r"[^A-Za-z0-9_-]+", "-", str(text or "").strip()).strip("-")
    return c or "unknown"


def _jsonable(obj: Any) -> Any:
    import datetime as _dt
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (_dt.date, _dt.datetime)):
        return obj.isoformat()
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    m = _FRONT_RE.match(text or "")
    if not m:
        return {}, text or ""
    raw, body = m.group("body"), text[m.end():]
    try:
        import yaml  # type: ignore
        parsed = yaml.safe_load(raw)
        return (parsed if isinstance(parsed, dict) else {}), body
    except Exception:
        fields: Dict[str, Any] = {}
        for line in raw.splitlines():
            if ":" in line and not line.lstrip().startswith(("-", "#")):
                k, _, v = line.partition(":")
                fields[k.strip()] = v.strip().strip("'\"")
        return fields, body


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [t.strip() for t in re.split(r"[,\s]+", value) if t.strip()]
    if isinstance(value, (list, tuple)):
        return [str(t).strip() for t in value if str(t).strip()]
    return [str(value)]


def _is_episodic(rel: Path) -> bool:
    if any(p.lower() in _JOURNAL_DIRS for p in rel.parts[:-1]):
        return True
    return bool(_DATE_NAME_RE.match(rel.stem))


# ─── IMPORT: Obsidian vault -> MIF ───────────────────────────────────────────

def _resolve_note_id(rel: Path, fm: Dict[str, Any]) -> Optional[str]:
    oid = fm.get("id") or fm.get("uid")
    try:
        return "urn:mif:" + str(uuid.UUID(str(oid))) if oid else None
    except (ValueError, TypeError):
        return None


def _note_to_memory(note: Path, *, rel: Path, fm: Dict[str, Any], body: str,
                     mif_id: str, id_by_key: Dict[str, str]) -> Dict[str, Any]:
    episodic = _is_episodic(rel)
    concept = "episodic" if episodic else "semantic"
    comps = [_ns_component(c) for c in rel.parent.parts]
    base = "_episodic/journal" if episodic else "_semantic"
    namespace = "/".join([base, *comps]) if comps else base

    rels: List[Dict[str, Any]] = []
    seen = set()
    for tn in _WIKILINK_RE.findall(body):
        tn = tn.strip()
        if tn and tn not in seen:
            seen.add(tn)
            # Resolve the link to the actual target note's @id (matched by
            # stem or frontmatter title, built in vault_to_mif) so the
            # relationship is traversable; fall back to a stable hash of the
            # link text only for links that don't resolve to any note in
            # this vault (e.g. genuinely dangling links).
            target = id_by_key.get(tn) or ("urn:mif:" + str(uuid.uuid5(OBSIDIAN_NS, tn)))
            rels.append({"type": "links-to",
                         "target": target,
                         "metadata": {"obsidian": {"wikilink": tn}}})

    tags = sorted(set(_as_list(fm.get("tags")) + _INLINE_TAG_RE.findall(body) + ["obsidian"]))
    mem: Dict[str, Any] = {
        "@context": MIF_CONTEXT,
        "@type": "Memory",
        "@id": mif_id,
        "conceptType": concept,
        "namespace": namespace,
        "content": body.strip() or "(empty note)",
        "created": _iso(fm.get("created") or fm.get("date")) or _mtime_iso(note),
        "title": str(fm.get("title") or rel.stem),
        "tags": tags,
        "extensions": {"obsidian": {
            "vault_path": rel.as_posix(),
            "frontmatter": _jsonable(fm) or None,
            "aliases": _as_list(fm.get("aliases")) or None,
        }},
    }
    if rels:
        mem["relationships"] = rels
    return {"kind": "memory", "payload": mem}


def _mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except Exception:
        return _EPOCH


def iter_notes(vault: Path) -> Iterator[Path]:
    for note in sorted(vault.rglob("*.md")):
        parts = {p.lower() for p in note.relative_to(vault).parts}
        if ".obsidian" not in parts and ".trash" not in parts:
            yield note


def vault_to_mif(vault: Path, *, source_instance: Optional[str] = None) -> Dict[str, Any]:
    """Read an Obsidian vault into a MIF Container Profile corpus."""
    # Pass 1: parse frontmatter and settle each note's own @id first, and
    # index it by stem and frontmatter title (Obsidian's own [[link]]
    # resolution keys) so pass 2 can resolve wikilink relationship targets
    # to the actual linked note's @id instead of a placeholder hashed from
    # the link text alone (first note wins on stem/title collisions).
    parsed: List[Tuple[Path, Path, Dict[str, Any], str, str]] = []
    id_by_key: Dict[str, str] = {}
    for note in iter_notes(vault):
        rel = note.relative_to(vault)
        fm, body = _parse_frontmatter(note.read_text(encoding="utf-8", errors="replace"))
        mif_id = _resolve_note_id(rel, fm) or ("urn:mif:" + str(uuid.uuid5(OBSIDIAN_NS, rel.as_posix())))
        parsed.append((note, rel, fm, body, mif_id))
        for key in (rel.stem, str(fm.get("title") or "").strip()):
            if key:
                id_by_key.setdefault(key, mif_id)

    # Pass 2: build the memory records with relationship targets resolved.
    records = [
        _note_to_memory(note, rel=rel, fm=fm, body=body, mif_id=mif_id, id_by_key=id_by_key)
        for note, rel, fm, body, mif_id in parsed
    ]
    corpus: Dict[str, Any] = {
        "@context": MIF_CONTEXT, "@type": "MemoryCorpus", "mif_version": MIF_VERSION,
        "records": records,
        "provenance": {"@type": "prov:Entity", "prov:wasGeneratedBy": {
            "@type": "prov:Activity", "prov:used": SOURCE_SYSTEM,
            "prov:generatedAtTime": datetime.now(timezone.utc).isoformat()}},
    }
    if source_instance:
        corpus["provenance"]["prov:wasDerivedFrom"] = source_instance
    return corpus


# ─── EXPORT: MIF -> Obsidian vault ───────────────────────────────────────────

def _yaml_scalar(v: Any) -> str:
    s = str(v)
    return f'"{s}"' if re.search(r"[:#\[\]{}]|^\s|\s$", s) else s


def _frontmatter_block(fm: Dict[str, Any]) -> str:
    lines = ["---"]
    for k, v in fm.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            if not v:
                continue
            lines.append(f"{k}:")
            lines.extend(f"  - {_yaml_scalar(i)}" for i in v)
        else:
            lines.append(f"{k}: {_yaml_scalar(v)}")
    lines.append("---")
    return "\n".join(lines)


def _memory_to_note(payload: Dict[str, Any], *, id_to_title: Dict[str, str]) -> Tuple[str, str]:
    """Return (vault-relative path, note text) for one MIF memory unit."""
    obs = (payload.get("extensions") or {}).get("obsidian") or {}
    rel = obs.get("vault_path")
    title = payload.get("title") or (rel and Path(rel).stem) or payload["@id"].split(":")[-1]

    # Reconstruct frontmatter: prefer the round-trip blob, else synthesize.
    fm = dict(obs.get("frontmatter") or {})
    fm.setdefault("id", payload["@id"].replace("urn:mif:", ""))
    fm.setdefault("created", payload.get("created"))
    tags = [t for t in (payload.get("tags") or []) if t != "obsidian"]
    if tags:
        fm["tags"] = tags
    if obs.get("aliases"):
        fm["aliases"] = obs["aliases"]

    body = payload.get("content") or ""
    # Restore [[wikilinks]] from relationships if the body lost them.
    for r in payload.get("relationships") or []:
        wl = ((r.get("metadata") or {}).get("obsidian") or {}).get("wikilink")
        if not wl:
            wl = id_to_title.get(str(r.get("target")))
        if wl and f"[[{wl}]]" not in body:
            body = body.rstrip() + f"\n\nRelated: [[{wl}]]"

    if rel:
        path = rel if rel.endswith(".md") else rel + ".md"
    else:
        # namespace path minus the _semantic/_episodic root -> folder
        ns = payload.get("namespace", "")
        parts = [p for p in ns.split("/")[1:] if p]
        folder = "/".join(p for p in parts if p not in ("journal", "sessions"))
        path = (f"{folder}/{title}.md" if folder else f"{title}.md")

    return path, _frontmatter_block(fm) + "\n\n" + body.strip() + "\n"


def mif_to_vault(corpus: Dict[str, Any], vault: Path) -> int:
    """Render a MIF Container Profile corpus into an Obsidian vault directory.
    Returns the number of notes written."""
    memories = [r["payload"] for r in corpus.get("records", []) if r.get("kind") == "memory"]
    id_to_title = {m["@id"]: (m.get("title") or m["@id"].split(":")[-1]) for m in memories}
    n = 0
    for mem in memories:
        rel, text = _memory_to_note(mem, id_to_title=id_to_title)
        dest = vault / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        n += 1
    return n


# ─── MNEMOS bridge (import POST / export fetch) ──────────────────────────────

def post_mif_to_mnemos(corpus: Dict[str, Any], endpoint: str, api_key: str, *, batch_size: int = 200) -> Dict[str, int]:
    records = corpus.get("records", [])
    totals = {"imported": 0, "skipped": 0, "failed": 0}
    base = _require_http(endpoint.rstrip("/") + "/v1/import?format=mif")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    for start in range(0, len(records), batch_size):
        chunk = {**{k: corpus[k] for k in ("@context", "@type", "mif_version")}, "records": records[start:start + batch_size]}
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected -- endpoint is
        # operator-supplied and scheme-restricted to http(s) via _require_http above.
        req = urllib.request.Request(base, data=json.dumps(chunk).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                r = json.loads(resp.read())
                for k in totals:
                    totals[k] += int(r.get(k, 0))
        except urllib.error.HTTPError as e:
            sys.stderr.write(f"import batch failed: {e.code} {e.read()[:200]!r}\n")
            totals["failed"] += len(records[start:start + batch_size])
    return totals


def fetch_mnemos_as_mif(endpoint: str, api_key: str, *, namespace: Optional[str] = None) -> Dict[str, Any]:
    url = _require_http(endpoint.rstrip("/") + "/v1/export?format=mif")
    if namespace:
        url += "&namespace=" + urllib.parse.quote(namespace)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected -- url scheme-restricted above.
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    import urllib.parse  # noqa: F401  (used by fetch_mnemos_as_mif)
    ap = argparse.ArgumentParser(description="Bidirectional Obsidian <-> MIF adapter for MNEMOS.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("import", help="Obsidian vault -> MIF (-> MNEMOS).")
    imp.add_argument("--vault", required=True, type=Path)
    imp.add_argument("--out", default=None, help="Write the MIF corpus here ('-' for stdout).")
    imp.add_argument("--post", default=None, help="MNEMOS endpoint to POST the corpus to.")
    imp.add_argument("--api-key", default=os.environ.get("MNEMOS_API_KEY", ""))
    imp.add_argument("--source-instance", default=None)

    exp = sub.add_parser("export", help="MIF (from MNEMOS or file) -> Obsidian vault.")
    exp.add_argument("--vault", required=True, type=Path, help="Destination vault directory.")
    exp.add_argument("--mif", default=None, help="Read a MIF corpus from this file instead of MNEMOS.")
    exp.add_argument("--endpoint", default=None, help="MNEMOS endpoint to export from.")
    exp.add_argument("--api-key", default=os.environ.get("MNEMOS_API_KEY", ""))
    exp.add_argument("--namespace", default=None)

    args = ap.parse_args(argv)

    if args.cmd == "import":
        if not args.vault.is_dir():
            raise SystemExit(f"vault not found: {args.vault}")
        corpus = vault_to_mif(args.vault, source_instance=args.source_instance)
        if args.out:
            text = json.dumps(corpus, indent=2, ensure_ascii=False)
            (sys.stdout.write(text + "\n") if args.out == "-" else Path(args.out).write_text(text + "\n", encoding="utf-8"))
        if args.post:
            if not args.api_key:
                raise SystemExit("--post requires --api-key (or MNEMOS_API_KEY).")
            totals = post_mif_to_mnemos(corpus, args.post, args.api_key)
            sys.stderr.write(f"imported {len(corpus['records'])} notes -> MNEMOS: {totals}\n")
        elif not args.out:
            sys.stdout.write(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
        return 0

    # export
    if args.mif:
        corpus = json.loads(Path(args.mif).read_text(encoding="utf-8"))
    elif args.endpoint:
        if not args.api_key:
            raise SystemExit("--endpoint requires --api-key (or MNEMOS_API_KEY).")
        corpus = fetch_mnemos_as_mif(args.endpoint, args.api_key, namespace=args.namespace)
    else:
        raise SystemExit("export needs --mif <file> or --endpoint <mnemos>.")
    n = mif_to_vault(corpus, args.vault)
    sys.stderr.write(f"wrote {n} notes -> {args.vault}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
