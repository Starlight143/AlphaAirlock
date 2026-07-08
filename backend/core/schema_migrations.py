"""Lightweight schema migration helpers used until Alembic lands (P3).

`create_all()` only adds MISSING tables — never missing columns. This module
provides idempotent `ALTER TABLE ADD COLUMN` helpers that work on SQLite 3.35+
and PostgreSQL.

Every helper is safe to re-run on an already-migrated database: it inspects
the live schema first and only emits DDL when the target column is absent.
"""

from __future__ import annotations

import logging
from typing import Iterable, Tuple

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger("alpha.schema")


def _existing_columns(engine: Engine, table: str) -> set[str]:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def add_column_if_missing(
    engine: Engine,
    table: str,
    column: str,
    ddl_type: str,
    *,
    not_null: bool = False,
    default_sql: str | None = None,
) -> bool:
    """Add a column unless it already exists.

    Args:
        engine: SQLAlchemy Engine
        table: target table name (must already exist)
        column: column name to ensure
        ddl_type: backend-specific type string e.g. ``VARCHAR(32)`` or ``INTEGER``
        not_null: if True, append ``NOT NULL`` (requires default_sql for existing rows)
        default_sql: if provided, append ``DEFAULT <expr>``; pass quoted string literals

    Returns:
        True if a column was added, False if it already existed.
    """
    cols = _existing_columns(engine, table)
    if not cols:
        # Table doesn't exist yet — caller should ensure create_all() ran first.
        logger.warning("Table %s does not exist; skipping ADD COLUMN %s", table, column)
        return False
    if column in cols:
        return False

    pieces: list[str] = [f'ALTER TABLE {table} ADD COLUMN {column} {ddl_type}']
    if default_sql is not None:
        pieces.append(f"DEFAULT {default_sql}")
    if not_null:
        pieces.append("NOT NULL")
    sql = " ".join(pieces)

    with engine.begin() as conn:
        conn.execute(text(sql))
    logger.info("Schema migration: %s.%s added (%s)", table, column, ddl_type)
    return True


def ensure_columns(engine: Engine, specs: Iterable[Tuple[str, str, str, dict]]) -> int:
    """Bulk-apply `add_column_if_missing` across many columns.

    Each spec is ``(table, column, ddl_type, kwargs)``. Returns the count of
    columns actually added.
    """
    added = 0
    for table, column, ddl_type, kwargs in specs:
        if add_column_if_missing(engine, table, column, ddl_type, **kwargs):
            added += 1
    return added


def add_index_if_missing(
    engine: Engine,
    table: str,
    columns: Iterable[str],
    *,
    name: str | None = None,
    unique: bool = False,
) -> bool:
    """Idempotent ``CREATE INDEX IF NOT EXISTS`` shim. Returns True if created.

    On SQLite + PostgreSQL ``CREATE INDEX IF NOT EXISTS`` is supported, so
    this is a thin wrapper that just resolves the index name when omitted.
    """
    col_list = list(columns)
    if not col_list:
        return False
    idx_name = name or f"ix_{table}_{'_'.join(col_list)}"
    unique_kw = "UNIQUE " if unique else ""
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        logger.warning("Table %s does not exist; skipping CREATE INDEX %s", table, idx_name)
        return False
    existing_indexes = {ix["name"] for ix in inspector.get_indexes(table)}
    if idx_name in existing_indexes:
        return False
    cols_sql = ", ".join(col_list)
    sql = f'CREATE {unique_kw}INDEX IF NOT EXISTS {idx_name} ON {table} ({cols_sql})'
    with engine.begin() as conn:
        conn.execute(text(sql))
    logger.info("Schema migration: %s on %s(%s)", idx_name, table, cols_sql)
    return True


# ---------------------------------------------------------------------------
# P13 schema migrations — production data migrations applied at startup.
#
# Each migration is idempotent: running it twice on the same DB is a no-op
# on the second pass. Migrations are versioned via a ``_schema_meta`` table
# so the application can track which have been applied.
# ---------------------------------------------------------------------------


def _has_table(engine: Engine, name: str) -> bool:
    # P31-DB6: cross-dialect via SQLAlchemy inspector — sqlite_master is
    # SQLite-only and breaks PostgreSQL boot when ALPHA_DATABASE_URL is set.
    return name in inspect(engine).get_table_names()


def _has_index(engine: Engine, name: str) -> bool:
    inspector = inspect(engine)
    for table in inspector.get_table_names():
        if any(ix["name"] == name for ix in inspector.get_indexes(table)):
            return True
    return False


def _ensure_meta_table(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS _schema_meta ("
            "  migration TEXT PRIMARY KEY,"
            "  applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ")"
        ))


def _is_applied(engine: Engine, name: str) -> bool:
    # P15/D-L12 — opens a fresh connection per call. For the current 4
    # migrations this is fine (boot-time cost is negligible); if migration
    # count grows past ~10 consider batching all checks into one SELECT.
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT 1 FROM _schema_meta WHERE migration=:n"),
            {"n": name},
        ).fetchone()
    return row is not None


def _mark_applied(engine: Engine, name: str) -> None:
    # P31-DB6 follow-up: ``INSERT OR IGNORE`` is SQLite-only syntax and is a
    # hard syntax error on PostgreSQL (a documented ALPHA_DATABASE_URL target),
    # which would abort boot because run_pending() calls this for every
    # first-boot migration. Branch on the dialect: PostgreSQL gets the
    # standard ``ON CONFLICT (migration) DO NOTHING`` (migration is the PK so
    # the conflict target is valid), SQLite keeps ``INSERT OR IGNORE``.
    if engine.dialect.name == "postgresql":
        stmt = text(
            "INSERT INTO _schema_meta (migration) VALUES (:n) "
            "ON CONFLICT (migration) DO NOTHING"
        )
    elif engine.dialect.name in ("mysql", "mariadb"):
        stmt = text("INSERT IGNORE INTO _schema_meta (migration) VALUES (:n)")
    else:
        # SQLite: INSERT OR IGNORE is supported natively.
        # For any other dialect not listed above, this will raise a syntax error
        # at runtime — add a branch for that dialect if it becomes a target.
        stmt = text("INSERT OR IGNORE INTO _schema_meta (migration) VALUES (:n)")
    with engine.begin() as conn:
        conn.execute(stmt, {"n": name})


def _dedup_content_hash(engine: Engine) -> None:
    """P13/D-H2: collapse duplicate KnowledgeNode.content_hash rows.

    Keeps the oldest row per hash (lowest id), deletes the rest. Run
    before the UNIQUE constraint is installed at table-create time.
    """
    name = "p13_dedup_knowledge_node_content_hash"
    if _is_applied(engine, name):
        return
    if not _has_table(engine, "knowledge_nodes"):
        _mark_applied(engine, name)
        return
    with engine.begin() as conn:
        groups = conn.execute(text(
            "SELECT content_hash, COUNT(*) AS c, MIN(id) AS keep_id "
            "FROM knowledge_nodes "
            "WHERE content_hash IS NOT NULL AND content_hash != '' "
            "GROUP BY content_hash HAVING c > 1"
        )).fetchall()
        # Collect the exact IDs that will be removed so we can cascade child
        # rows before deleting the parent, avoiding FK constraint failures when
        # ic_history.node_id (NOT NULL FK) or granger_edges.src/dst_node_id
        # (NOT NULL FK) reference a node being deleted.
        del_ids: list = []
        for h, _c, keep_id in groups:
            dup_rows = conn.execute(text(
                "SELECT id FROM knowledge_nodes "
                "WHERE content_hash = :h AND id != :keep"
            ), {"h": h, "keep": keep_id}).fetchall()
            del_ids.extend(int(r[0]) for r in dup_rows)
        deleted = 0
        for del_id in del_ids:
            # Remove NOT-NULL FK children first (cannot be remapped).
            conn.execute(text("DELETE FROM ic_history WHERE node_id = :did"), {"did": del_id})
            conn.execute(text(
                "DELETE FROM granger_edges WHERE src_node_id = :did OR dst_node_id = :did"
            ), {"did": del_id})
            # NULL-out nullable FK references so the parent delete succeeds.
            conn.execute(text(
                "UPDATE ingest_events SET resulting_node_id = NULL "
                "WHERE resulting_node_id = :did"
            ), {"did": del_id})
            conn.execute(text(
                "UPDATE asset_cache SET node_id = NULL WHERE node_id = :did"
            ), {"did": del_id})
            res = conn.execute(text("DELETE FROM knowledge_nodes WHERE id = :did"), {"did": del_id})
            deleted += res.rowcount or 0
        if deleted:
            logger.warning(
                "P13/D-H2: dedup_knowledge_node_content_hash removed %d duplicate rows "
                "across %d hash groups.", deleted, len(groups)
            )
        # Mark applied inside the same transaction so the DELETE and the
        # migration-record insert commit atomically, eliminating the TOCTOU
        # window where a concurrent worker sees the migration as not applied.
        if engine.dialect.name == "postgresql":
            _meta_stmt = text(
                "INSERT INTO _schema_meta (migration) VALUES (:n) "
                "ON CONFLICT (migration) DO NOTHING"
            )
        elif engine.dialect.name in ("mysql", "mariadb"):
            _meta_stmt = text("INSERT IGNORE INTO _schema_meta (migration) VALUES (:n)")
        else:
            _meta_stmt = text("INSERT OR IGNORE INTO _schema_meta (migration) VALUES (:n)")
        conn.execute(_meta_stmt, {"n": name})


def _install_unique_index_content_hash(engine: Engine) -> None:
    """Install a partial UNIQUE index on content_hash (skipping NULLs).

    Defensive: SQLAlchemy's ``unique=True`` on the column may not catch
    every cross-version SQLite quirk, so we install the index by name
    too. NULL values are allowed (multiple intake stubs may have no hash).
    """
    name = "p13_install_unique_index_knowledge_node_content_hash"
    if _is_applied(engine, name):
        return
    idx = "ux_knowledge_nodes_content_hash"
    if _has_index(engine, idx):
        _mark_applied(engine, name)
        return
    try:
        with engine.begin() as conn:
            conn.execute(text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {idx} "
                "ON knowledge_nodes(content_hash) "
                "WHERE content_hash IS NOT NULL AND content_hash != ''"
            ))
    except IntegrityError:
        # P34: residual duplicate content_hash rows (e.g. the dedup migration was
        # marked applied but this index was never created, then a new duplicate
        # was ingested before the index existed) make CREATE UNIQUE INDEX raise
        # IntegrityError, which would otherwise abort ALL remaining migrations in
        # run_pending(). Log and continue so boot proceeds.
        #
        # IMPORTANT: do NOT _mark_applied here. The UNIQUE index was NOT created,
        # so DB-level uniqueness on content_hash is currently UNENFORCED. Marking
        # the migration applied would latch it permanently (nothing ever clears
        # _schema_meta), and the model-level unique=True does NOT retroactively
        # constrain the already-created table nor protect concurrent inserts.
        # Returning without marking lets a later clean boot (after dedup removes
        # the residual duplicates) retry and actually enforce uniqueness.
        logger.warning(
            "P34: could not create %s due to residual duplicate content_hash rows; "
            "leaving migration UNAPPLIED so a later boot retries after dedup. "
            "DB-level uniqueness on content_hash is NOT enforced until this succeeds.",
            idx, exc_info=True,
        )
        return
    _mark_applied(engine, name)


def _add_alpha_strategy_created_at(engine: Engine) -> None:
    """P15/D-M4: add created_at to alpha_strategies. Backfills existing rows
    from updated_at (best available proxy for legacy rows).

    SQLite's ``ALTER TABLE ADD COLUMN`` only accepts constant defaults
    (CURRENT_TIMESTAMP is rejected as "non-constant"), so we add the column
    without a default, backfill every row from updated_at in one statement,
    then mark applied. New rows get their value from the SQLAlchemy
    ``default=`` callable on the AlphaStrategy model.
    """
    name = "p15_add_alpha_strategies_created_at"
    if _is_applied(engine, name):
        return
    inspector = inspect(engine)
    if "alpha_strategies" not in inspector.get_table_names():
        _mark_applied(engine, name)
        return
    existing = {c["name"] for c in inspector.get_columns("alpha_strategies")}
    if "created_at" in existing:
        _mark_applied(engine, name)
        return
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE alpha_strategies ADD COLUMN created_at TIMESTAMP NULL"
        ))
        # Backfill from updated_at where present so analytics don't show NULL.
        conn.execute(text(
            "UPDATE alpha_strategies SET created_at = updated_at "
            "WHERE created_at IS NULL AND updated_at IS NOT NULL"
        ))
        # Belt-and-suspenders: for any remaining NULL rows (e.g. those created
        # with NULL updated_at, which shouldn't happen given the model NOT NULL
        # constraint but defensive code is cheap), stamp with current UTC time.
        conn.execute(text(
            "UPDATE alpha_strategies SET created_at = CURRENT_TIMESTAMP "
            "WHERE created_at IS NULL"
        ))
    _mark_applied(engine, name)


def _add_alpha_strategy_alpha_id(engine: Engine) -> None:
    """P17/H1: add UNIQUE alpha_id column to alpha_strategies + backfill."""
    name = "p17_add_alpha_strategies_alpha_id"
    if _is_applied(engine, name):
        return
    inspector = inspect(engine)
    if "alpha_strategies" not in inspector.get_table_names():
        _mark_applied(engine, name)
        return
    existing = {c["name"] for c in inspector.get_columns("alpha_strategies")}
    if "alpha_id" not in existing:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE alpha_strategies ADD COLUMN alpha_id VARCHAR(20) NULL"
            ))
    import uuid as _uuid
    # P29-S8: narrow to IntegrityError, log each retry, raise after exhausting.
    from sqlalchemy.exc import IntegrityError as _MigIntegrityError
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id FROM alpha_strategies WHERE alpha_id IS NULL OR alpha_id = ''"
        )).fetchall()
        for row in rows:
            _attempts = 0
            _last_exc = None
            while _attempts < 5:
                _attempts += 1
                candidate = f"ALPHA-{_uuid.uuid4().hex[:8].upper()}"
                # P30-DB6: wrap each UPDATE in a SAVEPOINT so a UNIQUE
                # collision rolls back ONLY this row's attempt. Without
                # this, on PostgreSQL an uncaught IntegrityError inside
                # engine.begin() puts the whole transaction in an aborted
                # state — every subsequent statement raises
                # InFailedSqlTransactionError, breaking the migration.
                try:
                    with conn.begin_nested():
                        conn.execute(
                            text("UPDATE alpha_strategies SET alpha_id = :aid WHERE id = :sid"),
                            {"aid": candidate, "sid": int(row[0])},
                        )
                    break
                except _MigIntegrityError as exc:
                    _last_exc = exc
                    logger.warning(
                        "schema_migrations: alpha_id collision sid=%s attempt=%d candidate=%s",
                        int(row[0]), _attempts, candidate,
                    )
                    continue
            else:
                raise RuntimeError(
                    f"schema_migrations: failed to assign unique alpha_id for "
                    f"sid={int(row[0])} after 5 attempts; last_exc={_last_exc!r}"
                )
    idx = "ux_alpha_strategies_alpha_id"
    if not _has_index(engine, idx):
        # Create the index inside a single new transaction so there is no
        # window between the UPDATE commit and the UNIQUE constraint being
        # live.  Standard CREATE UNIQUE INDEX (non-CONCURRENTLY) is allowed
        # inside a transaction on both SQLite and PostgreSQL.
        with engine.begin() as conn:
            conn.execute(text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {idx} "
                "ON alpha_strategies(alpha_id) "
                "WHERE alpha_id IS NOT NULL AND alpha_id != ''"
            ))
            if engine.dialect.name == "postgresql":
                _meta_stmt = text(
                    "INSERT INTO _schema_meta (migration) VALUES (:n) "
                    "ON CONFLICT (migration) DO NOTHING"
                )
            elif engine.dialect.name in ("mysql", "mariadb"):
                _meta_stmt = text("INSERT IGNORE INTO _schema_meta (migration) VALUES (:n)")
            else:
                _meta_stmt = text("INSERT OR IGNORE INTO _schema_meta (migration) VALUES (:n)")
            conn.execute(_meta_stmt, {"n": name})
    else:
        _mark_applied(engine, name)


def _add_manual_orders_exchange_order_id(engine: Engine) -> None:
    """P18/D3: add nullable exchange_order_id to manual_orders for future
    live-trade correlation with exchange fills.

    Additive only: ALTER TABLE ADD COLUMN (SQLite-compatible, nullable, no
    default). No backfill required — existing rows are paper orders that
    never touched a venue. Non-UNIQUE index supports lookup-by-venue-id once
    the live executor wires it up.
    """
    name = "p18_add_manual_orders_exchange_order_id"
    if _is_applied(engine, name):
        return
    inspector = inspect(engine)
    if "manual_orders" not in inspector.get_table_names():
        _mark_applied(engine, name)
        return
    existing = {c["name"] for c in inspector.get_columns("manual_orders")}
    if "exchange_order_id" not in existing:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE manual_orders ADD COLUMN exchange_order_id VARCHAR(128) NULL"
            ))
    idx = "ix_manual_orders_exchange_order_id"
    if not _has_index(engine, idx):
        with engine.begin() as conn:
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS {idx} "
                "ON manual_orders(exchange_order_id) "
                "WHERE exchange_order_id IS NOT NULL"
            ))
    _mark_applied(engine, name)


def _recompute_content_hash_32k(engine: Engine) -> None:
    """Recompute KnowledgeNode.content_hash using the 32 000-char body cap.

    Background: ingest_fetchers._content_hash() was updated to slice the body
    at 32 000 chars (previously 4096). Any existing row whose ``content``
    exceeds 4096 chars has a stored hash computed under the old 4096-cap
    formula. After the code deploy, every re-fetch of such an article
    produces a 32 000-cap hash that does NOT match the stored hash, causing
    the dedup SELECT to miss and a duplicate KnowledgeNode row to be inserted.
    The UNIQUE constraint only catches a true hash collision, not this
    hash-formula mismatch.

    This migration:
    1. Reads every KnowledgeNode whose content length exceeds 4096 chars
       (those are the only rows whose hash changes).
    2. Recomputes the hash in Python using the same logic as
       ingest_fetchers._content_hash() — deliberately inlined to avoid a
       circular import and to keep the migration self-contained even if the
       caller module is refactored.
    3. Updates the row.  Uses SAVEPOINT-level isolation on PostgreSQL so a
       collision on the new hash (two articles that are identical in the first
       32 000 chars) aborts only that one row and keeps the migration going.
    4. After bulk update, runs a dedup pass (keep lowest id per hash) to
       collapse any newly introduced collisions before the UNIQUE index sees
       them.

    Guards:
    - Keyed on ``p_recompute_content_hash_32k`` in ``_schema_meta`` so it
      runs exactly once per database.
    - Skipped (marked applied immediately) when the table does not exist or
      contains no row with content > 4096 chars.
    """
    import hashlib as _hashlib

    name = "p_recompute_content_hash_32k"
    if _is_applied(engine, name):
        return
    if not _has_table(engine, "knowledge_nodes"):
        _mark_applied(engine, name)
        return

    # --- Phase 1: recompute hashes for long-content rows ---
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, title, source_url, content "
            "FROM knowledge_nodes "
            "WHERE content IS NOT NULL AND LENGTH(content) > 4096"
        )).fetchall()

    if not rows:
        _mark_applied(engine, name)
        return

    updated = 0
    skipped_collision = 0

    with engine.begin() as conn:
        for row in rows:
            node_id, title, source_url, content = row[0], row[1], row[2], row[3]
            # Replicate ingest_fetchers._content_hash() exactly.
            h = _hashlib.sha256()
            h.update(((title or "").strip()).encode("utf-8", errors="ignore"))
            h.update(b"|")
            h.update(((source_url or "").strip()).encode("utf-8", errors="ignore"))
            h.update(b"|")
            h.update(((content or "").strip()[:32_000]).encode("utf-8", errors="ignore"))
            new_hash = h.hexdigest()

            try:
                with conn.begin_nested():
                    conn.execute(
                        text("UPDATE knowledge_nodes SET content_hash = :h WHERE id = :id"),
                        {"h": new_hash, "id": int(node_id)},
                    )
                updated += 1
            except IntegrityError:
                # Another row already has this 32k-cap hash. Keep the existing
                # row's (old) hash; the duplicate will be resolved in Phase 2.
                skipped_collision += 1
                logger.warning(
                    "p_recompute_content_hash_32k: hash collision on node id=%s "
                    "(new hash=%s already exists); leaving old hash in place — "
                    "Phase 2 will dedup.", int(node_id), new_hash,
                )

    if updated or skipped_collision:
        logger.info(
            "p_recompute_content_hash_32k: updated %d rows, skipped %d collision(s).",
            updated, skipped_collision,
        )

    # --- Phase 2: dedup any post-recomputation hash collisions ---
    # Identical to _dedup_content_hash logic but scoped to the newly
    # recomputed rows. Runs inside a fresh transaction.
    with engine.begin() as conn:
        groups = conn.execute(text(
            "SELECT content_hash, COUNT(*) AS c, MIN(id) AS keep_id "
            "FROM knowledge_nodes "
            "WHERE content_hash IS NOT NULL AND content_hash != '' "
            "GROUP BY content_hash HAVING COUNT(*) > 1"
        )).fetchall()
        del_ids: list = []
        for h, _c, keep_id in groups:
            dup_rows = conn.execute(text(
                "SELECT id FROM knowledge_nodes "
                "WHERE content_hash = :h AND id != :keep"
            ), {"h": h, "keep": keep_id}).fetchall()
            del_ids.extend(int(r[0]) for r in dup_rows)
        post_dedup_deleted = 0
        for del_id in del_ids:
            conn.execute(text("DELETE FROM ic_history WHERE node_id = :did"), {"did": del_id})
            conn.execute(text(
                "DELETE FROM granger_edges WHERE src_node_id = :did OR dst_node_id = :did"
            ), {"did": del_id})
            conn.execute(text(
                "UPDATE ingest_events SET resulting_node_id = NULL "
                "WHERE resulting_node_id = :did"
            ), {"did": del_id})
            conn.execute(text(
                "UPDATE asset_cache SET node_id = NULL WHERE node_id = :did"
            ), {"did": del_id})
            res = conn.execute(text("DELETE FROM knowledge_nodes WHERE id = :did"), {"did": del_id})
            post_dedup_deleted += res.rowcount or 0
        if post_dedup_deleted:
            logger.warning(
                "p_recompute_content_hash_32k Phase 2: removed %d duplicate rows "
                "across %d hash groups after recomputation.",
                post_dedup_deleted, len(groups),
            )
        # Mark applied inside the same transaction so the DELETE and the
        # migration-record insert commit atomically, eliminating the TOCTOU
        # window where a crash after Phase-2 commits but before _mark_applied
        # would leave the migration unrecorded and cause re-execution on next boot.
        if engine.dialect.name == "postgresql":
            _meta_stmt = text(
                "INSERT INTO _schema_meta (migration) VALUES (:n) "
                "ON CONFLICT (migration) DO NOTHING"
            )
        elif engine.dialect.name in ("mysql", "mariadb"):
            _meta_stmt = text("INSERT IGNORE INTO _schema_meta (migration) VALUES (:n)")
        else:
            _meta_stmt = text("INSERT OR IGNORE INTO _schema_meta (migration) VALUES (:n)")
        conn.execute(_meta_stmt, {"n": name})


def _install_unique_index_asset_original_url(engine: Engine) -> None:
    """Install a partial UNIQUE index on asset_cache.original_url (skipping
    NULL/empty).

    P6 dedup guarantee: at most one AssetCache row per source URL. Without a
    DB-level UNIQUE, two parallel cross-source ingest workers caching the same
    <img src> URL both pass the SELECT-by-original_url dedup in
    asset_cache._cache_one and both INSERT (duplicate rows + double download).
    Mirrors _install_unique_index_content_hash. NOTE: the dedup key is
    original_url, NOT sha256 — the URL-alias path in _cache_one intentionally
    writes multiple rows sharing one sha256, so sha256 must stay non-unique.
    """
    name = "p6_install_unique_index_asset_cache_original_url"
    if _is_applied(engine, name):
        return
    if not _has_table(engine, "asset_cache"):
        _mark_applied(engine, name)
        return
    idx = "ux_asset_cache_original_url"
    if _has_index(engine, idx):
        _mark_applied(engine, name)
        return
    # Collapse any pre-existing duplicate original_url rows first (keep lowest
    # id) so the CREATE UNIQUE INDEX does not raise on legacy data.
    try:
        with engine.begin() as conn:
            groups = conn.execute(text(
                "SELECT original_url, COUNT(*) c, MIN(id) keep_id "
                "FROM asset_cache "
                "WHERE original_url IS NOT NULL AND original_url != '' "
                "GROUP BY original_url HAVING COUNT(*) > 1"
            )).fetchall()
            for url, _c, keep_id in groups:
                conn.execute(text(
                    "DELETE FROM asset_cache "
                    "WHERE original_url = :u AND id != :keep"
                ), {"u": url, "keep": keep_id})
            if groups:
                logger.warning(
                    "P6: removed duplicate asset_cache rows across %d "
                    "original_url groups before installing %s.",
                    len(groups), idx,
                )
    except Exception:  # noqa: BLE001
        logger.warning(
            "P6: asset_cache original_url dedup pass failed; "
            "attempting index creation anyway.", exc_info=True,
        )
    try:
        with engine.begin() as conn:
            conn.execute(text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {idx} "
                "ON asset_cache(original_url) "
                "WHERE original_url IS NOT NULL AND original_url != ''"
            ))
    except IntegrityError:
        # IMPORTANT: do NOT _mark_applied here. The UNIQUE index was NOT created,
        # so DB-level uniqueness on original_url is currently UNENFORCED. Marking
        # the migration applied would latch it permanently (_schema_meta is never
        # cleared), and two concurrent ingest workers caching the same source URL
        # could still insert duplicate rows via asset_cache._cache_one.
        # Returning without marking lets a later clean boot (after dedup removes
        # the residual duplicates) retry and actually enforce uniqueness.
        # Mirrors _install_unique_index_content_hash exactly.
        logger.warning(
            "P6: could not create %s due to residual duplicate original_url "
            "rows; leaving migration UNAPPLIED so a later boot retries after "
            "dedup. DB-level uniqueness on original_url is NOT enforced until "
            "this succeeds.", idx, exc_info=True,
        )
        return
    _mark_applied(engine, name)


def run_pending(engine: Engine) -> None:
    """Run all pending P13 migrations. Idempotent.

    Order matters: dedup must run BEFORE the UNIQUE index is installed,
    otherwise the CREATE INDEX itself raises IntegrityError on existing
    duplicate rows.
    """
    _ensure_meta_table(engine)
    _dedup_content_hash(engine)
    _recompute_content_hash_32k(engine)
    _install_unique_index_content_hash(engine)
    _add_alpha_strategy_created_at(engine)
    _add_alpha_strategy_alpha_id(engine)
    _add_manual_orders_exchange_order_id(engine)
    _install_unique_index_asset_original_url(engine)


__all__ = [
    "add_column_if_missing",
    "ensure_columns",
    "add_index_if_missing",
    "run_pending",
]
