"""MPF import orchestration."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from fastapi import HTTPException

from mnemos.core.persisted_text_classification import classify_persisted_text_fields
from mnemos.core.security import is_root
from mnemos.db import portability_repo as repo
from mnemos.persistence.base import AuditPersistence, Transaction

from .allowlist import _build_referenced_memory_allowlist
from .ids import _derive_caller_scoped_id
from .phases import (
    _import_compression_manifest,
    _import_kg_triples,
    _import_memory_versions,
    _restore_memory_branches,
)
from .schemas import (
    MEMORY_PAYLOAD_VERSION,
    MPF_VERSION_PREFIX,
    MPF_VERSION_PREFIX_V0_2,
    ImportStats,
    MPFEnvelope,
)
from .serializers import _MPF_V0_2_RECORD_FIELDS_METADATA_KEY
from .timestamps import MalformedTimestampError, _parse_iso_naive

logger = logging.getLogger(__name__)


def _new_stats() -> ImportStats:
    return ImportStats(
        imported=0,
        skipped=0,
        failed=0,
        unsupported_kinds={},
        sidecars_imported={},
        sidecars_skipped={},
        sidecars_failed={},
        errors=[],
    )


async def _fetch_verbatim_content_for_legacy_core(conn, memory_id: str):
    """Read a field omitted by older core portability projections.

    CHARON's HTTP portability routes are PostgreSQL-gated, so this query uses
    asyncpg's ``$1`` paramstyle.  Once the minimum core version includes
    ``verbatim_content`` in ``fetch_memory_by_id``, this compatibility query is
    no longer reached.
    """
    row = await conn.fetchrow(
        "SELECT verbatim_content FROM memories "
        "WHERE id = $1 AND deleted_at IS NULL",
        memory_id,
    )
    return row["verbatim_content"] if row is not None else None


def _validate_import_request(envelope: MPFEnvelope, preserve_owner: bool, user) -> None:
    supported_prefixes = (MPF_VERSION_PREFIX, MPF_VERSION_PREFIX_V0_2)
    if not envelope.mpf_version.startswith(supported_prefixes):
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported MPF version {envelope.mpf_version!r}; "
                f"expected {MPF_VERSION_PREFIX}x or {MPF_VERSION_PREFIX_V0_2}x"
            ),
        )

    if envelope.deletion_log:
        raise HTTPException(
            status_code=415,
            detail="MPF deletion_log sidecar import is not supported",
        )

    unsupported_envelope_fields = [
        field
        for field in (
            "relations",
            "compression_candidates",
            "attestations",
        )
        if getattr(envelope, field)
    ]
    if envelope.embeddings is not None:
        unsupported_envelope_fields.append("embeddings")
    if unsupported_envelope_fields:
        raise HTTPException(
            status_code=415,
            detail=(
                "MPF envelope fields are not supported for import without "
                "data loss: " + ", ".join(sorted(unsupported_envelope_fields))
            ),
        )

    if preserve_owner and not is_root(user):
        raise HTTPException(status_code=403, detail="preserve_owner=true requires root")

    if envelope.memory_versions and not (preserve_owner and is_root(user)):
        raise HTTPException(
            status_code=403,
            detail=(
                "memory_versions sidecar import requires root + "
                "preserve_owner=true (the admin/migration path; "
                "use --preserve-metadata in tools/memory_import.py "
                "with a root bearer token). Non-root callers can "
                "import records and rely on the trigger-fired "
                "default v1 history, or ship kg_triples / "
                "compression_manifest sidecars without restriction. "
                "The non-root + memory_versions sidecar combination "
                "is not supported under CHARON v0.2 due to "
                "deterministic-id stale-state interactions."
            ),
        )

    if envelope.memory_versions:
        memory_record_ids = {r.id for r in envelope.records if r.kind == "memory"}
        sidecar_versioned_ids = {e.get("record_id") for e in envelope.memory_versions if e.get("record_id")}
        uncovered = memory_record_ids - sidecar_versioned_ids
        if uncovered:
            raise HTTPException(
                status_code=400,
                detail=(
                    "memory_versions sidecar must cover every kind: memory "
                    f"record being imported. {len(uncovered)} record(s) have "
                    f"no version entry: {sorted(uncovered)[:5]}"
                    f"{'...' if len(uncovered) > 5 else ''}. Either ship a "
                    "complete sidecar or omit the memory_versions array "
                    "entirely (the trigger will synthesize default v1 history)."
                ),
            )


async def import_memories(
    conn,
    *,
    envelope: MPFEnvelope,
    preserve_owner: bool,
    user,
    backend: AuditPersistence | None = None,
    tx: Transaction | None = None,
) -> ImportStats:
    _validate_import_request(envelope, preserve_owner, user)
    stats = _new_stats()

    async with conn.transaction():
        if envelope.memory_versions:
            await repo.set_suppress_version_snapshot(conn)

        inserted_record_ids: set = set()
        rejected_persisted_ids: set = set()
        id_remap: Dict[str, str] = {}
        non_root_id_rewrite = not (preserve_owner and is_root(user))

        for record in envelope.records:
            if record.kind != "memory":
                stats.unsupported_kinds[record.kind] = stats.unsupported_kinds.get(record.kind, 0) + 1
                continue

            if record.payload_version != MEMORY_PAYLOAD_VERSION:
                stats.skipped += 1
                stats.errors.append(
                    f"{record.id}: unsupported payload_version "
                    f"{record.payload_version!r}; expected {MEMORY_PAYLOAD_VERSION}"
                )
                rejected_persisted_ids.add(record.id)
                continue

            p = record.payload
            if not isinstance(p, dict):
                stats.failed += 1
                stats.errors.append(
                    f"{record.id}: kind 'memory' payload must be an object; skipped"
                )
                rejected_persisted_ids.add(record.id)
                continue
            if preserve_owner:
                imported_owner = p.get("owner_id") or user.user_id
                imported_ns = p.get("namespace") or user.namespace
            else:
                imported_owner = user.user_id
                imported_ns = user.namespace

            content = p.get("content")
            if not content or not str(content).strip():
                stats.failed += 1
                stats.errors.append(f"{record.id}: empty content; skipped")
                rejected_persisted_ids.add(record.id)
                continue

            category = p.get("category") or "imported"
            subcategory = p.get("subcategory")
            permission_mode = p.get("permission_mode")
            if permission_mode is None:
                permission_mode = 600
            if (
                not isinstance(permission_mode, int)
                or isinstance(permission_mode, bool)
                or permission_mode < 0
                or permission_mode > 777
                or any(digit not in "01234567" for digit in str(permission_mode))
            ):
                stats.failed += 1
                stats.errors.append(
                    f"{record.id}: permission_mode must be octal-style 0-777; skipped"
                )
                rejected_persisted_ids.add(record.id)
                continue
            metadata = p.get("metadata") or {}
            if not isinstance(metadata, dict):
                stats.failed += 1
                stats.errors.append(f"{record.id}: metadata must be an object; skipped")
                rejected_persisted_ids.add(record.id)
                continue
            metadata = dict(metadata)
            # MPF v0.2 record-level fields (provenance, valid_time_*,
            # transaction_time) are sibling-of-payload per the v0.2 spec
            # but the memories schema has no first-class columns for
            # them. Persist them as explicitly-untrusted imported data
            # under payload.metadata so a round-trip re-export restores
            # them via the serializer bridge — without ever letting them
            # authenticate identity or attribution. If the envelope's
            # payload.metadata already carries a customer-owned value at
            # the bridge key, the importer preserves that customer value
            # AND stores the imported v0.2 envelope fields at a sibling
            # sub-key so they survive the round-trip; without the sibling
            # sub-key the v0.2 fields would be silently dropped on
            # re-export (the export side reconstructs from row columns
            # rather than the bridge).
            record_level_v02: Dict[str, Any] = {}
            if envelope.mpf_version.startswith(MPF_VERSION_PREFIX_V0_2):
                if record.provenance is not None:
                    record_level_v02["provenance"] = record.provenance
                if record.valid_time_start is not None:
                    record_level_v02["valid_time_start"] = record.valid_time_start
                if record.valid_time_end is not None:
                    record_level_v02["valid_time_end"] = record.valid_time_end
                if record.transaction_time is not None:
                    record_level_v02["transaction_time"] = record.transaction_time
            if record_level_v02:
                if _MPF_V0_2_RECORD_FIELDS_METADATA_KEY not in metadata:
                    # Customer hasn't claimed the bridge key — store the
                    # v0.2 fields there.
                    metadata[_MPF_V0_2_RECORD_FIELDS_METADATA_KEY] = record_level_v02
                else:
                    # Customer owns the bridge key with their own value.
                    # Preserve the customer value AND persist the imported
                    # v0.2 envelope fields at a sibling sub-key so they
                    # survive a re-export round-trip instead of being
                    # silently dropped (asserted by
                    # test_import_does_not_replace_customer_metadata_with_v02_bridge
                    # — the customer value is preserved verbatim; the
                    # imported v0.2 fields are stored at the sibling key).
                    metadata[_MPF_V0_2_RECORD_FIELDS_METADATA_KEY + "_imported_envelope"] = record_level_v02
            quality_rating = p.get("quality_rating")
            if quality_rating is None:
                quality_rating = 75
            if (
                not isinstance(quality_rating, int)
                or isinstance(quality_rating, bool)
                or not 0 <= quality_rating <= 100
            ):
                stats.failed += 1
                stats.errors.append(
                    f"{record.id}: quality_rating must be an integer from 0 to 100; skipped"
                )
                rejected_persisted_ids.add(record.id)
                continue
            verbatim_content = p.get("verbatim_content") or content

            classified = classify_persisted_text_fields(
                content=content,
                verbatim_content=verbatim_content,
                metadata=metadata,
                namespace=imported_ns,
                classified_at="mpf_record_import",
                memory_id=record.id,
            )
            metadata = classified.metadata
            imported_ns = classified.namespace

            if non_root_id_rewrite:
                persisted_id = _derive_caller_scoped_id(
                    record.id,
                    caller_owner=imported_owner,
                    caller_namespace=imported_ns,
                    content=str(content),
                )
            else:
                persisted_id = record.id
            id_remap[record.id] = persisted_id

            try:
                async with conn.transaction():
                    row = await repo.insert_memory(
                        conn,
                        memory_id=persisted_id,
                        content=content,
                        category=category,
                        subcategory=subcategory,
                        metadata_json=json.dumps(metadata),
                        quality_rating=quality_rating,
                        owner_id=imported_owner,
                        namespace=imported_ns,
                        permission_mode=permission_mode,
                        source_model=p.get("source_model"),
                        source_provider=p.get("source_provider"),
                        source_session=p.get("source_session"),
                        source_agent=p.get("source_agent"),
                        verbatim_content=verbatim_content,
                        created=_parse_iso_naive(p.get("created")),
                        updated=_parse_iso_naive(p.get("updated")),
                    )
                if row == "INSERT 0 0":
                    existing_mem = await repo.fetch_memory_by_id(conn, persisted_id)
                    envelope_metadata_json = json.dumps(metadata, sort_keys=True)
                    existing_metadata_json = (
                        json.dumps(
                            existing_mem["metadata"]
                            if isinstance(existing_mem["metadata"], dict)
                            else json.loads(existing_mem["metadata"] or "{}"),
                            sort_keys=True,
                        )
                        if existing_mem is not None
                        else None
                    )
                    envelope_created = _parse_iso_naive(p.get("created"))
                    envelope_updated = _parse_iso_naive(p.get("updated"))
                    mismatched_fields: list[str] = []
                    if existing_mem is None:
                        mismatched_fields.append("row missing")
                    else:
                        try:
                            existing_verbatim = existing_mem["verbatim_content"]
                        except (KeyError, IndexError):
                            existing_verbatim = await _fetch_verbatim_content_for_legacy_core(
                                conn, persisted_id
                            )
                        checks = [
                            ("content", existing_mem["content"], content),
                            ("category", existing_mem["category"], category),
                            ("subcategory", existing_mem["subcategory"], subcategory),
                            ("metadata", existing_metadata_json, envelope_metadata_json),
                            ("quality_rating", existing_mem["quality_rating"], quality_rating),
                            ("owner_id", existing_mem["owner_id"], imported_owner),
                            ("namespace", existing_mem["namespace"], imported_ns),
                            ("permission_mode", existing_mem["permission_mode"], permission_mode),
                            ("source_model", existing_mem["source_model"], p.get("source_model")),
                            (
                                "source_provider",
                                existing_mem["source_provider"],
                                p.get("source_provider"),
                            ),
                            (
                                "source_session",
                                existing_mem["source_session"],
                                p.get("source_session"),
                            ),
                            ("source_agent", existing_mem["source_agent"], p.get("source_agent")),
                            ("verbatim_content", existing_verbatim, verbatim_content),
                        ]
                        for col, db_val, env_val in checks:
                            if db_val != env_val:
                                mismatched_fields.append(col)
                        if envelope_created is not None and existing_mem["created"] != envelope_created:
                            mismatched_fields.append("created")
                        if envelope_updated is not None and existing_mem["updated"] != envelope_updated:
                            mismatched_fields.append("updated")

                    if mismatched_fields:
                        stats.failed += 1
                        stats.errors.append(
                            f"{record.id}: existing memory row doesn't match "
                            f"envelope payload ({', '.join(mismatched_fields)} "
                            "differ); sidecar attachment refused"
                        )
                        id_remap.pop(record.id, None)
                        rejected_persisted_ids.add(persisted_id)
                        continue
                    stats.skipped += 1
                else:
                    stats.imported += 1
                    inserted_record_ids.add(persisted_id)
            except Exception as exc:
                stats.failed += 1
                stats.errors.append(f"{record.id}: {type(exc).__name__}: {exc}")
                logger.exception("MPF import failed for record %s", record.id)
                id_remap.pop(record.id, None)
                rejected_persisted_ids.add(persisted_id)
                continue

            # Audit write happens OUTSIDE the insert try/except. An
            # enabled audit chain must fail the entire import (rolled
            # back via the outer transaction) so memory mutations
            # don't commit with permanent gaps in the tamper-evident
            # chain. Catching audit failures here would let the
            # memory row land while the chain is silently incomplete.
            if (
                inserted_record_ids
                and backend is not None
                and tx is not None
            ):
                # The insert above only adds to inserted_record_ids on
                # the non-conflict path. The audit write needs the
                # memory_id, content, category, subcategory, metadata
                # for the row that just landed; recompute them via the
                # current record iteration to keep the call self-contained.
                last_inserted = persisted_id
                await _write_mpf_import_audit_entry(
                    conn,
                    backend,
                    tx,
                    memory_id=last_inserted,
                    content=content,
                    category=category,
                    subcategory=subcategory,
                    metadata=metadata,
                    writer_id=user.user_id,
                )

        if id_remap:
            for entry in envelope.kg_triples or []:
                mid = entry.get("memory_id")
                if mid and mid in id_remap:
                    entry["memory_id"] = id_remap[mid]
            for entry in envelope.memory_versions or []:
                rid = entry.get("record_id")
                if rid and rid in id_remap:
                    entry["record_id"] = id_remap[rid]
            for entry in envelope.compression_manifest or []:
                rid = entry.get("record_id")
                if rid and rid in id_remap:
                    entry["record_id"] = id_remap[rid]

        if is_root(user) and preserve_owner:
            scope_owner: Optional[str] = None
            scope_namespace: Optional[str] = None
        else:
            scope_owner = user.user_id
            scope_namespace = user.namespace
        allowlist = await _build_referenced_memory_allowlist(
            conn,
            envelope,
            scope_owner=scope_owner,
            scope_namespace=scope_namespace,
        )
        for rid in rejected_persisted_ids:
            allowlist.pop(rid, None)

        if envelope.kg_triples:
            await _import_kg_triples(
                conn,
                envelope.kg_triples,
                caller_user_id=user.user_id,
                caller_namespace=user.namespace,
                preserve_owner=preserve_owner,
                stats=stats,
                allowlist=allowlist,
                inserted_record_ids=inserted_record_ids,
            )
        if envelope.memory_versions:
            (
                authorized_version_ids,
                failed_version_record_ids,
                authorized_version_uuids,
            ) = await _import_memory_versions(
                conn,
                envelope.memory_versions,
                caller_user_id=user.user_id,
                caller_namespace=user.namespace,
                preserve_owner=preserve_owner,
                stats=stats,
                allowlist=allowlist,
                inserted_record_ids=inserted_record_ids,
            )
            fatal_record_ids = inserted_record_ids & failed_version_record_ids
            if fatal_record_ids:
                sample = sorted(fatal_record_ids)[:5]
                extra = "..." if len(fatal_record_ids) > 5 else ""
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "CHARON import: memory_versions sidecar had failed "
                        f"entries for {len(fatal_record_ids)} newly inserted "
                        f"record(s): {sample}{extra}. Authoritative history "
                        "is all-or-nothing per record under trigger "
                        "suppression - partial history would be inconsistent. "
                        "Transaction rolled back; fix the sidecar and retry."
                    ),
                )

            if inserted_record_ids:
                await repo.delete_memory_branches_for_memories(
                    conn,
                    list(inserted_record_ids),
                )
            if authorized_version_ids:
                await _restore_memory_branches(
                    conn,
                    list(authorized_version_ids),
                    authorized_version_uuids=list(authorized_version_uuids),
                )
        if envelope.compression_manifest:
            await _import_compression_manifest(
                conn,
                envelope.compression_manifest,
                caller_user_id=user.user_id,
                caller_namespace=user.namespace,
                preserve_owner=preserve_owner,
                stats=stats,
                allowlist=allowlist,
                inserted_record_ids=inserted_record_ids,
            )

        if envelope.memory_versions:
            if inserted_record_ids:
                covered = await repo.fetch_versioned_memory_ids(conn, list(inserted_record_ids))
                covered_ids = {r["memory_id"] for r in covered}
                uncovered = inserted_record_ids - covered_ids
                if uncovered:
                    sample = sorted(uncovered)[:5]
                    extra = "..." if len(uncovered) > 5 else ""
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "CHARON import inserted "
                            f"{len(uncovered)} memory record(s) without "
                            "version history under trigger suppression: "
                            f"{sample}{extra}. Sidecar likely contained "
                            "malformed or rejected entries that did not "
                            "produce rows. Transaction rolled back."
                        ),
                    )

            touched_ids = inserted_record_ids | authorized_version_ids
            if touched_ids:
                head_check = await repo.fetch_memory_head_checks(conn, list(touched_ids))
                missing_inserted = []
                divergent = []
                in_db_inserted = inserted_record_ids
                for r in head_check:
                    rid = r["id"]
                    head_content = r["head_content"]
                    memory_content = r["memory_content"]
                    if head_content is None:
                        if rid in in_db_inserted:
                            missing_inserted.append(rid)
                    elif memory_content != head_content:
                        divergent.append(rid)
                if missing_inserted:
                    sample = sorted(missing_inserted)[:5]
                    extra = "..." if len(missing_inserted) > 5 else ""
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "CHARON import inserted "
                            f"{len(missing_inserted)} memory record(s) with "
                            f"no main-branch HEAD: {sample}{extra}. The "
                            "envelope's memory_versions sidecar must include "
                            "a branch='main' entry for every kind:memory "
                            "record being imported. Transaction rolled back."
                        ),
                    )
                if divergent:
                    sample = sorted(divergent)[:5]
                    extra = "..." if len(divergent) > 5 else ""
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "CHARON import: live memory content diverges "
                            "from restored memory_versions HEAD for "
                            f"{len(divergent)} record(s): {sample}{extra}. "
                            "The envelope's memory_versions sidecar must "
                            "include an entry whose content matches each "
                            "touched memory's content. Transaction rolled back."
                        ),
                    )

    logger.info(
        "[MPF] import: user=%s imported=%d skipped=%d failed=%d unsupported=%s sidecars_imported=%s",
        user.user_id,
        stats.imported,
        stats.skipped,
        stats.failed,
        stats.unsupported_kinds,
        stats.sidecars_imported,
    )
    return stats


async def _write_mpf_import_audit_entry(
    conn,
    backend: AuditPersistence,
    tx: Transaction,
    *,
    memory_id: str,
    content: str,
    category: str,
    subcategory: str | None,
    metadata: dict[str, Any] | None,
    writer_id: str,
) -> None:
    if getattr(backend, "audit_chain", None) is None:
        return
    from mnemos.audit import write_audit_entry
    from mnemos.core.config import get_settings
    from mnemos.workers.audit_sealer import audit_chain_enabled

    if not audit_chain_enabled():
        return
    session_secret = (getattr(get_settings().server, "session_secret", "") or "").encode("utf-8")
    if not session_secret:
        # An enabled audit chain with no configured session_secret would
        # write a tampered/unsignable entry — that's a configuration
        # bug, not an optional degradation. Refuse the import so the
        # operator notices and either fixes the secret or disables the
        # chain explicitly (the disabled path is taken above).
        raise RuntimeError(
            "MNEMOS_AUDIT_CHAIN is enabled but session_secret is empty; "
            "refusing to import without a signing secret (set "
            "MNEMOS_SESSION_SECRET or unset MNEMOS_AUDIT_CHAIN)."
        )
    # asyncpg implements a nested transaction as a savepoint. An audit
    # write failure re-raises so the inner savepoint rolls back AND
    # propagates out of the outer import transaction — without this,
    # memory mutations would commit with permanent gaps in the
    # configured tamper-evident chain.
    async with conn.transaction():
        await write_audit_entry(
            backend,
            tx,
            op="create",
            memory_id_str=memory_id,
            content=content,
            category=category,
            subcategory=subcategory,
            metadata=metadata,
            embedding=None,
            writer_id=writer_id,
            session_secret=session_secret,
            enforce_continuity=True,
        )
