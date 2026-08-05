"""FastAPI entry point for the Agentic Alpha Research System."""

from __future__ import annotations

# IMPORTANT: load .env BEFORE any backend module reads os.environ.
# `_envloader` is intentionally side-effecting at import time.
from backend import _envloader  # noqa: F401  - keep first

import hmac
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from backend.agents._client import call_messages, describe_provider_config
from backend.agents.intake import process_text_to_node
from backend.agents.researcher import generate_alpha_story
from backend.core.database import (
    KB_CONTENT_MAX_CHARS,
    KIND_ACTIVE,
    KIND_CONCEPT,
    KIND_PAST_ALPHA,
    KIND_POSTMORTEM,
    SOURCE_TYPES,
    AlphaChatMessage,
    AlphaChatSession,
    AlphaStrategy,
    AssetCache,
    IngestEvent,
    IngestSource,
    KnowledgeNode,
    PROJECT_ROOT,
    get_db,
    init_db,
    session_scope,
)
from backend.core.personas import list_personas
from backend._envloader import env_secret_or_none

logger = logging.getLogger("alpha.api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: D401
    init_db()
    logger.info("Database initialized at startup")
    # P11-B-06 — release any auto-pipeline reservations that were orphaned by an
    # earlier process crash (sentinel left on KnowledgeNode rows). Runs after
    # init_db so schema migrations have applied. Never raises.
    try:
        from backend.core.auto_pipeline import recover_orphan_reservations
        released = recover_orphan_reservations(stale_minutes=15)
        if released:
            logger.info("Released %d orphan auto-pipeline reservations on startup", released)
    except Exception:  # noqa: BLE001
        logger.exception("Orphan reservation recovery failed at startup (non-fatal)")
    # P3 — start the ingestion scheduler (no-op if ALPHA_INGEST_ENABLED is off).
    try:
        from backend.core.scheduler import start_scheduler
        await start_scheduler()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to start ingestion scheduler (continuing without it)")
    # P5 — start the Telegram inbound /intake bot (no-op if disabled).
    try:
        from backend.core.telegram_inbound import start_inbound
        await start_inbound()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to start Telegram inbound bot (continuing without it)")
    # P8-FIX/C-5 — start the Discord inbound /intake bot (no-op if disabled).
    try:
        from backend.core.discord_inbound import start_inbound as discord_start
        await discord_start()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to start Discord inbound bot (continuing without it)")
    # P6 — periodic tasks: independent loop for KB relink / IC decay / Granger
    # recompute / Telegram automatic reports / paper-trade tick. Each task is
    # opt-in via its own env flag; none touch the ingest tick.
    try:
        from backend.core.periodic_tasks import start_periodic_tasks
        await start_periodic_tasks()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to start periodic task runner (continuing without it)")
    try:
        yield
    finally:
        try:
            from backend.core.periodic_tasks import stop_periodic_tasks
            await stop_periodic_tasks()
        except Exception:  # noqa: BLE001
            logger.exception("Periodic task runner shutdown error (ignored)")
        try:
            from backend.core.scheduler import stop_scheduler
            await stop_scheduler()
        except Exception:  # noqa: BLE001
            logger.exception("Scheduler shutdown error (ignored)")
        try:
            from backend.core.telegram_inbound import stop_inbound
            await stop_inbound()
        except Exception:  # noqa: BLE001
            logger.exception("Telegram inbound shutdown error (ignored)")
        try:
            from backend.core.discord_inbound import stop_inbound as discord_stop
            await discord_stop()
        except Exception:  # noqa: BLE001
            logger.exception("Discord inbound shutdown error (ignored)")
        try:
            # P32-DB32-7 — release the SQLAlchemy connection pool on graceful
            # shutdown so SQLite WAL checkpoints and idle connections close.
            # Without this, dev reload accumulates open file handles and the
            # DB eventually returns "database is locked" on Windows.
            from backend.core.database import engine as _alpha_engine
            _alpha_engine.dispose()
        except Exception:  # noqa: BLE001
            logger.exception("engine.dispose() failed (ignored)")


app = FastAPI(
    title="Agentic Alpha Research System",
    version="1.0.2",
    description="Multi-agent quant research pipeline (Intake -> Researcher -> Coder -> Backtest -> Critic).",
    lifespan=lifespan,
)

# P29-C5+C10: env-driven CORS allowlist. Operators set ALPHA_ALLOWED_ORIGINS
# (CSV) for staging/prod hostnames so wildcards never reach production.
# P30-S7: per-origin URL-shape validator. Wildcards/typos accepted silently
# would leave operators thinking CORS was configured while every preflight
# silently failed (or worse, with allow_credentials=True the combo is
# spec-illegal).
_ALLOWED_ORIGIN_RE = re.compile(
    r"^https?://[A-Za-z0-9.\-]+(:[0-9]{1,5})?$"
)


def _resolve_allowed_origins() -> List[str]:
    raw = os.environ.get("ALPHA_ALLOWED_ORIGINS", "").strip()
    if not raw:
        return ["http://localhost:3000", "http://127.0.0.1:3000"]
    items = [p.strip() for p in raw.split(",") if p.strip()]
    if not items:
        return ["http://localhost:3000", "http://127.0.0.1:3000"]
    validated: List[str] = []
    for origin in items:
        if "*" in origin:
            raise RuntimeError(
                f"ALPHA_ALLOWED_ORIGINS: wildcard '*' is not permitted with "
                f"allow_credentials=True (got {origin!r}); list every origin "
                f"explicitly"
            )
        if not _ALLOWED_ORIGIN_RE.match(origin):
            raise RuntimeError(
                f"ALPHA_ALLOWED_ORIGINS: {origin!r} is not a valid origin. "
                f"Required form: 'http(s)://host[:port]' (no path/query)"
            )
        validated.append(origin)
    return validated


app.add_middleware(
    CORSMiddleware,
    allow_origins=_resolve_allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "Authorization",
        "X-Operator-Token",
        "Idempotency-Key",
        "X-Actor",
        "X-Terminal-Session",
        "X-Request-ID",
    ],
)

# Global request body size limit. Starlette buffers the full body before
# Pydantic validation runs, so this must be enforced at middleware level.
# 50 MB is generous for the bulk-import endpoint (max 500 items × 200 KB = 100 MB
# theoretical, but 50 MB is sufficient for real payloads and blocks abuse).
try:
    from starlette.middleware.contentsize import ContentSizeLimitMiddleware  # type: ignore[import]
    app.add_middleware(ContentSizeLimitMiddleware, max_content_size=50 * 1024 * 1024)  # 50 MB
except ImportError:
    # Fallback: custom lightweight body-size middleware.
    import starlette.types as _starlette_types

    class _BodySizeLimitMiddleware:
        """Reject requests whose Content-Length exceeds the cap, or stream-abort oversized bodies."""
        _MAX = 50 * 1024 * 1024  # 50 MB

        def __init__(self, app: _starlette_types.ASGIApp) -> None:
            self._app = app

        async def __call__(self, scope: _starlette_types.Scope, receive: _starlette_types.Receive, send: _starlette_types.Send) -> None:
            if scope["type"] == "http":
                headers = dict(scope.get("headers", []))
                cl_raw = headers.get(b"content-length", b"0")
                try:
                    cl = int(cl_raw)
                except (ValueError, TypeError):
                    cl = 0
                if cl > self._MAX:
                    await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json")]})
                    await send({"type": "http.response.body", "body": b'{"error":"request_entity_too_large"}', "more_body": False})
                    return
                received = 0

                async def _limited_receive() -> _starlette_types.Message:
                    nonlocal received
                    msg = await receive()
                    if msg.get("type") == "http.request":
                        received += len(msg.get("body", b""))
                        if received > self._MAX:
                            raise HTTPException(413, {"error": "request_entity_too_large"})
                    return msg

                await self._app(scope, _limited_receive, send)
            else:
                await self._app(scope, receive, send)

    app.add_middleware(_BodySizeLimitMiddleware)


# ---------------------------------------------------------------------------
# Operator authentication (HIGH-RISK guard).
# Fail-closed ONLY when ALPHA_OPERATOR_TOKEN is configured: a static bearer
# token compared in constant time. When the env var is unset the guard is a
# no-op so local dev / CI / tests are unaffected (additive). Attach via
# Depends(require_operator) on every state-changing high-risk route. The
# returned principal replaces the spoofable X-Actor header for audit `actor`.
# ---------------------------------------------------------------------------
def require_operator(request: Request) -> str:
    expected = env_secret_or_none("ALPHA_OPERATOR_TOKEN")
    if expected is None:
        # Guard disabled only when explicitly opted out via env flag.
        # Default (unconfigured) is fail-CLOSED to prevent silent open access
        # in production deployments that forget to set the token.
        from backend._envloader import env_bool as _env_bool_local
        if not _env_bool_local("ALPHA_DISABLE_OPERATOR_GUARD", False):
            raise HTTPException(
                status_code=503,
                detail="ALPHA_OPERATOR_TOKEN is not configured; operator guard cannot function. "
                       "Set ALPHA_OPERATOR_TOKEN or set ALPHA_DISABLE_OPERATOR_GUARD=true to "
                       "explicitly opt into unauthenticated access (dev/CI only).",
            )
        # Explicit opt-out (ALPHA_DISABLE_OPERATOR_GUARD=true): preserve pre-existing behavior.
        return (request.headers.get("X-Actor", "operator") or "operator").strip() or "operator"
    presented = ""
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        presented = auth[7:].strip()
    if not presented:
        presented = (request.headers.get("X-Operator-Token") or "").strip()
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="operator authentication required")
    # Authenticated principal: prefer explicit X-Actor label, else 'operator'.
    return (request.headers.get("X-Actor", "operator") or "operator").strip() or "operator"


# P29-C5: sanitized global exception handler. Returns ``{error, detail}`` with
# the exception *type* (never message — message strings can leak DB paths /
# SQL fragments). Full traceback logged server-side for postmortem.
@app.exception_handler(Exception)
async def _alpha_unhandled_exception(request: Request, exc: Exception):
    logger.exception(
        "Unhandled exception during %s %s",
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal",
            "detail": type(exc).__name__,
        },
    )


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------

class IntakeRequest(BaseModel):
    # P26 — upper bound mirrors the BulkImport.content cap (line 418: 200_000)
    # but tightened to 50_000 for free-form commentary. Without ``max_length``
    # a malicious POST of `raw_text=("A"*10**9)` would:
    #   1. allocate ~1 GB in the FastAPI worker before Pydantic rejects it,
    #   2. be hashed (first 4096 chars only — cheap) but then
    #   3. passed in full to ``process_text_to_node`` which forwards it to the
    #      Claude LLM call, spending real $ on a degenerate request.
    # Every other request model in this file (lines 330, 417, 418, 424, 1236,
    # 1248, 2635, 2813, 3047) already pairs ``min_length`` with ``max_length``;
    # these three older models were anomalies. Pure defense-in-depth — well-
    # behaved clients are unaffected (Telegram/Discord cap at ~4096 chars).
    raw_text: str = Field(
        ..., min_length=10, max_length=50_000,
        description="Unstructured market commentary",
    )


class ResearchRequest(BaseModel):
    # P26 — cap node_ids list. ``generate_alpha_story`` fans the nodes' content
    # into a single LLM prompt; >100 nodes blows the Claude context window
    # AND produces a useless story even if it fits. 100 matches the
    # implicit LLM context budget; ``BulkImportRequest.items`` uses 500 as
    # precedent for batched-but-small payloads.
    node_ids: List[int] = Field(..., min_length=1, max_length=100)


class PipelineRequest(BaseModel):
    raw_text: str = Field(..., min_length=10, max_length=50_000)
    node_ids: Optional[List[int]] = Field(default=None, max_length=100)


# ---------------------------------------------------------------------------
# Health / root
# ---------------------------------------------------------------------------

@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "service": "Agentic Alpha Research System",
        "status": "operational",
        "version": app.version,
        "docs": "/docs",
    }


@app.get("/api/system/checklist")
def api_system_checklist(principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """P-SYSHEALTH — dependency / config readiness checklist for Mission Control.

    Mirrors the reference demo's SYSTEM HEALTH rail: runtime, key Python
    packages, config files, LLM, database, data, and bot wiring. Each item is
    {group, name, state, detail} where state is one of:
      ok   — green  (installed / configured / reachable)
      warn — amber  (works, but a capability is missing, e.g. no LLM key)
      off  — slate  (intentionally disabled, e.g. a default-OFF bot)
      fail — rose   (a genuine problem, e.g. a required package missing)
    Read-only + side-effect-free: package presence is probed with
    importlib.util.find_spec (no heavy imports at request time).
    """
    import sys
    import importlib.util as _ilu
    from backend._envloader import is_real_secret as _is_real, env_bool as _eb

    items: List[Dict[str, Any]] = []

    def add(group: str, name: str, state: str, detail: str = "") -> None:
        items.append({"group": group, "name": name, "state": state, "detail": detail})

    # --- Runtime -----------------------------------------------------------
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    add(
        "Runtime", "Python venv", "ok" if in_venv else "warn",
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        + (" · venv" if in_venv else " · system"),
    )

    # --- Packages (find_spec avoids importing heavy libs) ------------------
    pkgs = [
        ("numpy", "numpy"), ("pandas", "pandas"), ("scipy", "scipy"),
        ("scikit-learn", "sklearn"), ("statsmodels", "statsmodels"),
        ("networkx", "networkx"), ("httpx", "httpx"), ("sqlalchemy", "sqlalchemy"),
        ("feedparser", "feedparser"), ("yfinance", "yfinance"),
        ("youtube-transcript-api", "youtube_transcript_api"),
        ("discord.py", "discord"), ("trafilatura", "trafilatura"),
    ]
    for label, mod in pkgs:
        try:
            present = _ilu.find_spec(mod) is not None
        except (ImportError, ValueError):
            present = False
        add("Packages", label, "ok" if present else "fail",
            "installed" if present else "missing")

    # --- Config + data -----------------------------------------------------
    env_exists = (PROJECT_ROOT / ".env").exists()
    add("Config", ".env", "ok" if env_exists else "fail",
        "present" if env_exists else "missing")
    data_csv = PROJECT_ROOT / "backend" / "data" / "synthetic_btc.csv"
    add("Config", "BTC price CSV", "ok" if data_csv.exists() else "warn",
        "present" if data_csv.exists() else "missing — run ingest or data_gen.py")
    # --- Data source: real Binance ingest vs synthetic fallback ------------
    try:
        from backend.core import market_data as _md
        _meta = _md.read_meta()
    except Exception:  # noqa: BLE001
        _meta = None
    if _meta and int(_meta.get("ok_count", 0)) > 0:
        # Count actual price CSVs on disk so a partial refresh never makes the
        # coverage look smaller than it is (the meta's total reflects only the
        # last run's symbol subset).
        try:
            _pairs = len(list((PROJECT_ROOT / "storage" / "prices").glob("*-USDT.csv")))
        except Exception:  # noqa: BLE001
            _pairs = int(_meta.get("total", 0) or 0)
        add("Data", "Market data",
            "ok" if _meta.get("source") == "binance_public" else "warn",
            f"{_meta.get('source', '?')} · {_pairs} pairs on disk"
            f" · {_meta.get('start', '?')}..{_meta.get('end', '?')}")
        add("Data", "Liquidations", "warn",
            "0.0 — free source has none; needs paid feed (Coinglass/Amberdata)")
    else:
        add("Data", "Market data", "warn",
            "synthetic — POST /api/market-data/ingest for real data")

    # --- LLM ---------------------------------------------------------------
    try:
        llm = describe_provider_config() or {}
    except Exception:  # noqa: BLE001
        llm = {}
    add("LLM", f"{llm.get('resolved') or llm.get('requested') or '?'} / {llm.get('model', '?')}",
        "ok" if llm.get("key_present") else "warn",
        "configured" if llm.get("key_present") else "no API key")

    # --- Database ----------------------------------------------------------
    db_ok = False
    try:
        from sqlalchemy import text as _text
        with session_scope() as _s:
            _s.execute(_text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    add("Database", "SQLAlchemy", "ok" if db_ok else "fail",
        "reachable" if db_ok else "unreachable")

    # --- Bots (default-OFF is shown as neutral 'off', never a red failure) -
    tg_in = _eb("TELEGRAM_INBOUND_ENABLED", False) and bool(_is_real(os.environ.get("TELEGRAM_BOT_TOKEN")))
    add("Bots", "Telegram /intake", "ok" if tg_in else "off", "enabled" if tg_in else "off")
    dc_in = _eb("DISCORD_INBOUND_ENABLED", False) and bool(_is_real(os.environ.get("DISCORD_BOT_TOKEN")))
    add("Bots", "Discord /intake", "ok" if dc_in else "off", "enabled" if dc_in else "off")
    dc_out = _eb("DISCORD_OUTBOUND_ENABLED", False) and bool(_is_real(os.environ.get("DISCORD_WEBHOOK_URL")))
    add("Bots", "Discord posts", "ok" if dc_out else "off", "enabled" if dc_out else "off")

    ok_count = sum(1 for it in items if it["state"] == "ok")
    fail_count = sum(1 for it in items if it["state"] == "fail")
    return {"items": items, "ok_count": ok_count, "fail_count": fail_count, "total": len(items)}


@app.get("/api/health")
def health() -> Dict[str, Any]:
    # P21 — placeholder filter: an unconfigured env file with a literal
    # ``sk-ant-xxxxxxxxxxxxxxxxxxxxxxxx`` value must not report the key as
    # "present" to the UI. Use the canonical _envloader.is_real_secret which
    # filters the project-wide placeholder prefixes (``your_``, ``xxxx``,
    # ``placeholder``, ``changeme``).
    from backend._envloader import is_real_secret
    from sqlalchemy import text as _text
    from backend.core import periodic_tasks as _pt

    llm = describe_provider_config()

    # --- DB liveness probe ---------------------------------------------------
    db_ok = False
    db_error: Optional[str] = None
    try:
        with session_scope() as _s:
            _s.execute(_text("SELECT 1"))
        db_ok = True
    except Exception as _exc:  # noqa: BLE001
        db_error = str(_exc)
        logger.warning("Health check: DB probe failed: %s", _exc)

    # --- Periodic-task liveness probe ----------------------------------------
    tasks_running = _pt.is_running()

    overall_status = "ok" if db_ok else "degraded"

    return {
        "status": overall_status,
        # Back-compat fields used by the existing frontend:
        "anthropic_key_present": is_real_secret(os.environ.get("ANTHROPIC_API_KEY")),
        # New multi-provider block:
        "llm": llm,
        # Subsystem detail (new — additive):
        "db": {"ok": db_ok, "error": db_error},
        "tasks": {"running": tasks_running},
    }


# ---------------------------------------------------------------------------
# Mission 3 endpoints
# ---------------------------------------------------------------------------

# P29-C6: per-IP rate-limit bucket for /api/intake. 5/min/IP — a single call
# kicks an LLM round-trip downstream, so even one a-few-seconds tightens the
# spam floor enough to block scripted abuse before it touches Claude.
_INTAKE_BUCKET: Dict[str, List[float]] = {}
_INTAKE_BUCKET_LOCK = threading.Lock()
_INTAKE_BUCKET_CAP_PER_MIN = 5


def _intake_rate_check(request: Request) -> None:
    # P31-D1: canonical strict-whitelist env_bool — typos like "ture"/"on" now
    # behave predictably; consistent with other env flags in this module.
    from backend._envloader import env_bool as _env_bool_local
    if _env_bool_local("BEHIND_TRUSTED_PROXY", False):
        xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        real_ip = (request.headers.get("X-Real-IP") or "").strip()
        client_ip = xff or real_ip
    else:
        client_ip = ""
    key_ip = client_ip or (request.client.host if request.client else "unknown") or "unknown"
    # P32-TZ32-6 — monotonic clock for rate-limit window; immune to wall-clock jumps.
    now = time.monotonic()
    with _INTAKE_BUCKET_LOCK:
        for k in list(_INTAKE_BUCKET.keys()):
            _INTAKE_BUCKET[k] = [t for t in _INTAKE_BUCKET[k] if now - t < 60.0]
            if not _INTAKE_BUCKET[k]:
                _INTAKE_BUCKET.pop(k, None)
        bucket = [t for t in _INTAKE_BUCKET.get(key_ip, []) if now - t < 60.0]
        if len(bucket) >= _INTAKE_BUCKET_CAP_PER_MIN:
            raise HTTPException(
                status_code=429,
                detail="rate limited — /api/intake capped per minute",
            )
        bucket.append(now)
        _INTAKE_BUCKET[key_ip] = bucket


@app.post("/api/intake")
def api_intake(req: IntakeRequest, request: Request, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """D-H5 — idempotent intake.

    Stage 0 — convert raw text into a KnowledgeNode. Cache key: sha256 of the
    first 4096 chars of raw_text (so the same text yields the same key even
    if the operator hits "Submit" twice in a flaky-network situation).
    """
    import hashlib
    from backend.core.idempotency import (
        lookup_or_record,
        require_idempotency_key,
    )

    # Validate the key BEFORE the rate-limit so a legitimate retry of the same
    # Idempotency-Key can replay the cached 200 instead of being 429'd. A replay
    # never invokes process_text_to_node (no LLM round-trip), so it must NOT
    # consume a rate-limit token. Mirrors /api/sources/{id}/poll (see ~line 1852).
    key = require_idempotency_key(request)
    # Hash the textual prefix so a retry with identical content hits the cache
    # even when the client mints a fresh UUID-flavored key.
    text_for_hash = (req.raw_text or "")[:4096].encode("utf-8", errors="replace")
    req_hash = hashlib.sha256(text_for_hash).hexdigest()

    # Synchronous replay check first — a confirmed replay returns the cached
    # response WITHOUT spending a rate-limit token. A hash mismatch (same key,
    # different text) still raises 409 here. Only genuinely-fresh requests fall
    # through to the rate limiter.
    with session_scope() as _replay_db:
        existing = None
        try:
            from backend.core.database import IdempotencyKey as _IK
            existing = _replay_db.query(_IK).filter(_IK.key == key).one_or_none()
        except Exception:  # noqa: BLE001
            # Don't silently disable replay protection — log with traceback and
            # fall back to fresh-request behaviour (rate-limit + compute).
            logger.exception(
                "intake: idempotency lookup failed key=%s; treating as fresh request",
                key[:16],
            )
            existing = None
        if existing is not None:
            if existing.request_hash != req_hash:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "IDEMPOTENCY_KEY_REUSED",
                        "original_hash": existing.request_hash,
                        "given_hash": req_hash,
                    },
                )
            try:
                cached_payload = json.loads(existing.response_json or "null")
            except (TypeError, ValueError):
                cached_payload = None
            cached_status = int(existing.status_code or 200)
            if cached_status >= 400:
                raise HTTPException(cached_status, cached_payload)
            return {**(cached_payload or {}), "idempotent_replay": True}

    # Not a replay — apply spam protection, then compute + persist.
    _intake_rate_check(request)

    with session_scope() as db:
        def _compute() -> Tuple[Dict[str, Any], int]:
            try:
                node = process_text_to_node(req.raw_text, session=db, entry_point="http")
            except Exception as exc:  # noqa: BLE001
                logger.exception("intake failed")
                # session_scope commits the lookup_or_record row, so returning a
                # 5xx here would persist it and replay the same stale 500 on every
                # retry. Raise instead: session_scope rolls back (no idempotency
                # row, no partial node) and the retry re-executes fresh.
                raise HTTPException(
                    status_code=500,
                    detail={"error": "internal", "detail": f"Intake failed: {type(exc).__name__}"},
                ) from exc
            return ({"node": node}, 200)

        outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)

    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    # P-DISCORD-OUT — best-effort Discord intake post on first-compute only.
    # Runs outside the DB transaction; never blocks or fails the intake.
    if not outcome.replay:
        try:
            _node = (outcome.response_payload or {}).get("node") or {}
            if _node.get("status") != "duplicate":
                from backend.core import discord_notifier
                discord_notifier.notify_intake(
                    node_id=_node.get("id"),
                    title=_node.get("title") or "",
                    entry_point=_node.get("entry_point") or "http",
                    source_url=_node.get("source_url"),
                )
        except Exception:  # noqa: BLE001
            logger.exception("Intake discord notify failed (non-fatal)")
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.post("/api/research")
def api_research(req: ResearchRequest, request: Request, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """Stage 1 — synthesize an Alpha Story from N knowledge nodes.

    Idempotent (mirrors /api/intake + /api/pipeline/run): generate_alpha_story
    fans the nodes into a single paid Claude call (researcher.py call_messages),
    so a flaky-network double-submit must replay the cached story instead of
    re-billing the account. Cache key = sorted node_ids; a retry with the same
    node set is a hit even when the client mints a fresh Idempotency-Key.
    """
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    req_hash = canonical_request_hash(
        {
            "endpoint": "research",
            "node_ids": sorted([int(x) for x in (req.node_ids or [])]),
        }
    )

    # session_scope (NOT get_db) so the idempotency row is committed; get_db
    # never commits, which would defeat the dedup. The LookupError->404 and the
    # P32-D9 generic-500 behaviour are preserved verbatim inside _compute.
    with session_scope() as db:
        def _compute() -> Tuple[Dict[str, Any], int]:
            try:
                story = generate_alpha_story(req.node_ids, session=db)
            except LookupError as exc:
                # 4xx must NOT be cached: session_scope rolls back on raise, so no
                # idempotency row persists and the retry re-executes fresh.
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("researcher failed")
                # P32-D9 policy — surface only the exception class, never the
                # message (may contain DB paths, secret tokens, or internal IDs).
                raise HTTPException(
                    status_code=500, detail=f"Research failed: {type(exc).__name__}"
                ) from exc
            return (story, 200)

        outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)

    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


# ---------------------------------------------------------------------------
# Knowledge / strategy listings (used by frontend graph + workspace)
# ---------------------------------------------------------------------------

# P29-C9: hard server-side cap so a tenant with 100k+ rows can't single-
# handedly DoS the API by ``GET /api/knowledge``. Response shape unchanged.
_LIST_HARD_LIMIT = 1000


@app.get("/api/knowledge")
def list_knowledge(principal: str = Depends(require_operator), db: Session = Depends(get_db)) -> Dict[str, Any]:
    nodes = (
        db.query(KnowledgeNode)
        .order_by(KnowledgeNode.id.desc())
        .limit(_LIST_HARD_LIMIT)
        .all()
    )
    return {"nodes": [n.to_dict() for n in nodes]}


@app.get("/api/knowledge/stats")
def api_knowledge_stats(
    db: Session = Depends(get_db),
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """T2-C — TRUE knowledge-node count (the KPI rail otherwise saturates at the
    /api/graph 1000-node render cap) + KB vital signs. Registered BEFORE the
    int-typed ``/api/knowledge/{node_id}`` route; the int converter already
    rejects the literal 'stats', but explicit ordering is defensive. Does NOT
    touch /api/graph or its cap."""
    from sqlalchemy import func as _func
    from backend.core import graph_intel

    total_nodes = db.query(KnowledgeNode.id).count()
    by_kind = {
        str(k): int(v)
        for k, v in db.query(KnowledgeNode.kind, _func.count()).group_by(KnowledgeNode.kind).all()
    }
    try:
        vitals = graph_intel.vital_signs(session=db)
    except Exception:  # noqa: BLE001 — vital signs are best-effort
        vitals = {}
    return {
        "total_knowledge_nodes": int(total_nodes),
        "by_kind": by_kind,
        "vital_signs": vitals,
    }


@app.get("/api/cost/summary")
def api_cost_summary(
    days: int = 7,
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """FinOps — the per-call LLM cost ledger aggregated by agent / model /
    strategy over the trailing ``days`` window (+ today's spend). Estimate basis
    matches the budget cap. Zeroed shape when the ledger is disabled or empty."""
    from backend.core import cost_ledger
    return cost_ledger.summary(days=max(1, min(int(days), 90)))


@app.get("/api/knowledge/{node_id}")
def get_knowledge(node_id: int, principal: str = Depends(require_operator), db: Session = Depends(get_db)) -> Dict[str, Any]:
    node = db.get(KnowledgeNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"KnowledgeNode {node_id} not found")
    return node.to_dict()


@app.get("/api/knowledge/{node_id}/ic-history")
def get_knowledge_ic_history(
    node_id: int,
    days: int = 90,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """P6-B3: time-series of IC observations for the Node Inspector
    decay-curve view. Read-only — does not trigger any computation."""
    node = db.get(KnowledgeNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"KnowledgeNode {node_id} not found")
    from backend.core.ic_history import get_history
    return {
        "node_id": node_id,
        "current_ic": float(node.ic_score or 0.0),
        "days": int(max(1, min(3650, days))),
        "samples": get_history(db, node_id, days=days),
    }


@app.get("/api/knowledge/{node_id}/postmortems")
def get_knowledge_postmortems(
    node_id: int,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """P8-FIX/H-2: postmortem feedback loop reverse-link.

    Returns the list of KnowledgeNodes with ``kind='postmortem'`` whose
    ``origin_strategy_id`` belongs to any strategy that referenced ``node_id``
    as a ``config.source_node_ids`` member. This is the "← this node was used
    in 3 past alphas, here are their postmortems" UI surface in KB Explorer's
    ArticlePreview.

    Read-only and additive. Stops at 50 results to keep payload bounded.
    """
    target_node = db.get(KnowledgeNode, node_id)
    if target_node is None:
        raise HTTPException(status_code=404, detail=f"KnowledgeNode {node_id} not found")

    # 1) Find every strategy whose source_node_ids contains node_id.
    # Pre-filter using a LIKE pattern to avoid materializing all strategies;
    # the JSON parse loop below handles false positives from the LIKE.
    matched_strategy_ids: List[int] = []
    node_id_str = str(int(node_id))
    candidate_strategies = (
        db.query(AlphaStrategy)
        .filter(AlphaStrategy.config_json.like(f"%\"source_node_ids\"%{node_id_str}%"))
        .all()
    )
    for st in candidate_strategies:
        try:
            srcs = st.config().get("source_node_ids") or []
        except Exception:  # noqa: BLE001
            srcs = []
        for s in srcs:
            try:
                if int(s) == int(node_id):
                    matched_strategy_ids.append(int(st.id))
                    break
            except (TypeError, ValueError):
                continue
    if not matched_strategy_ids:
        return {
            "node_id": node_id,
            "strategy_ids": [],
            "postmortems": [],
        }
    # 2) Pull postmortem KnowledgeNodes whose origin_strategy_id is in that set.
    postmortem_rows = (
        db.query(KnowledgeNode)
        .filter(KnowledgeNode.kind == KIND_POSTMORTEM)
        .filter(KnowledgeNode.origin_strategy_id.in_(matched_strategy_ids))
        .order_by(KnowledgeNode.id.desc())
        .limit(50)
        .all()
    )
    return {
        "node_id": node_id,
        "strategy_ids": matched_strategy_ids,
        "postmortems": [n.to_dict() for n in postmortem_rows],
    }


class SemanticSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000)
    top_k: int = Field(default=8, ge=1, le=50)
    include_categories: Optional[List[str]] = None


# D-L14/P16 — per-client rate-limit bucket for semantic-search. TF-IDF is
# cheap per-call but the sklearn vectorizer is allocated on every miss; a
# burst of queries from a single client can starve other tenants. Mirrors
# the factor-studio _FS_BUCKET pattern (60s window, opportunistic GC of stale
# entries). Cap is intentionally lower than factor-studio because semantic
# search is invoked from a search input that fires on keystroke.
_SS_BUCKET: Dict[str, List[float]] = {}
_SS_BUCKET_LOCK = threading.Lock()
_SS_BUCKET_CAP_PER_MIN = 60


def _semantic_search_rate_check(request: Request) -> None:
    from backend._envloader import env_bool as _env_bool
    if _env_bool("BEHIND_TRUSTED_PROXY", False):
        xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        real_ip = (request.headers.get("X-Real-IP") or "").strip()
        client_ip = xff or real_ip
    else:
        client_ip = ""
    key = client_ip or (request.client.host if request.client else "unknown") or "unknown"
    # P32-TZ32-6 — monotonic clock for rate-limit window; immune to wall-clock jumps.
    now = time.monotonic()
    with _SS_BUCKET_LOCK:
        # GC stale entries so the dict stays bounded across many distinct IPs.
        for k in list(_SS_BUCKET.keys()):
            _SS_BUCKET[k] = [t for t in _SS_BUCKET[k] if now - t < 60.0]
            if not _SS_BUCKET[k]:
                _SS_BUCKET.pop(k, None)
        bucket = [t for t in _SS_BUCKET.get(key, []) if now - t < 60.0]
        if len(bucket) >= _SS_BUCKET_CAP_PER_MIN:
            raise HTTPException(
                status_code=429,
                detail="rate limited — semantic search capped per minute",
            )
        bucket.append(now)
        _SS_BUCKET[key] = bucket


@app.post("/api/knowledge/semantic-search")
def api_knowledge_semantic_search(
    payload: SemanticSearchRequest,
    request: Request,
    db: Session = Depends(get_db),
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """P6-D04 + D-L14/P16: TF-IDF + cosine semantic retrieval over
    KnowledgeNode bodies.

    Uses scikit-learn (no sentence-transformers / torch). Returns ranked node
    IDs with cosine score plus an optional ``why_matched`` list of soft boosts
    (matching category, shared tags). 412 when the sklearn dependency is
    missing in this deployment. 429 when the per-client minute-budget is
    exhausted (D-L14).
    """
    _semantic_search_rate_check(request)
    from backend.core.semantic_retrieval import search as semantic_search
    try:
        hits = semantic_search(
            payload.query,
            top_k=int(payload.top_k),
            include_categories=payload.include_categories,
            session=db,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"semantic search failed: {type(exc).__name__}")
    if not hits and not payload.query.strip():
        return {"query": payload.query, "top_k": int(payload.top_k), "hits": []}
    return {
        "query": payload.query,
        "top_k": int(payload.top_k),
        "hits": [
            {
                "node_id": h.node_id,
                "score": h.score,
                "title": h.title,
                "category": h.category,
                "why_matched": h.why_matched,
            }
            for h in hits
        ],
    }


class BulkImportItem(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    content: str = Field(..., min_length=1, max_length=200_000)
    tags: Optional[List[str]] = None
    category: Optional[str] = None


class BulkImportRequest(BaseModel):
    items: List[BulkImportItem] = Field(..., min_length=1, max_length=500)


@app.post("/api/knowledge/bulk-import", status_code=201)
def api_knowledge_bulk_import(
    payload: BulkImportRequest,
    request: Request,
    principal: str = Depends(require_operator),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """P6-D02 + D-M15/P16: batched markdown import.

    Content arrives as a JSON list rather than as multipart uploads to keep
    this surface trivially callable from any CLI / script. The endpoint
    dedups by content_hash (same as the scheduler) and returns one summary
    row per item so the UI can show ``X created · Y duplicates · Z failed``.

    Inputs are bounded at 500 items per call (request schema enforces it) and
    each ``content`` is capped at 200k chars before truncation to the 32k node
    body limit; this prevents a runaway export from blowing the response.

    D-M15 — wrapped in Idempotency-Key + lookup_or_record so a flaky-network
    retry replays the cached creation summary instead of inserting duplicate
    nodes that happen to slip past the content-hash dedup (e.g. when the
    payload changes by a single whitespace).
    """
    import hashlib as _hashlib
    from datetime import datetime as _dt, timezone as _tz
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    body_payload = {
        "endpoint": "knowledge_bulk_import",
        "items": [
            {
                "title": (it.title or "").strip(),
                "content": (it.content or "")[:4096],
                "tags": sorted([t.strip() for t in (it.tags or []) if t and t.strip()]),
                "category": (it.category or "").strip() or None,
            }
            for it in (payload.items or [])
        ],
    }
    req_hash = canonical_request_hash(body_payload)

    def _compute() -> Tuple[Dict[str, Any], int]:
        if not payload.items:
            return ({"created": 0, "duplicates": 0, "failed": 0, "results": []}, 201)

        results: List[Dict[str, Any]] = []
        now = _dt.now(_tz.utc)
        created = 0
        duplicates = 0
        failed = 0
        for item in payload.items:
            title = (item.title or "").strip() or "(untitled)"
            body = (item.content or "").strip()
            tags_csv = ",".join((t.strip() for t in (item.tags or []) if t and t.strip()))
            cat = (item.category or "").strip() or None
            h = _hashlib.sha256(f"{title}|{cat or ''}|{body[:4096]}".encode("utf-8")).hexdigest()
            existing = (
                db.query(KnowledgeNode).filter(KnowledgeNode.content_hash == h).first()
            )
            if existing is not None:
                duplicates += 1
                results.append({"title": title, "status": "duplicate", "node_id": existing.id})
                continue
            try:
                node = KnowledgeNode(
                    title=title[:512],
                    content=body[:KB_CONTENT_MAX_CHARS],
                    tags=tags_csv,
                    links="[]",
                    ic_score=0.0,
                    kind=KIND_CONCEPT,
                    source_type="manual",
                    category=cat,
                    content_hash=h,
                    ingested_at=now,
                )
                from sqlalchemy.exc import IntegrityError as _IE
                try:
                    with db.begin_nested():  # SAVEPOINT — rolls back only this row on collision
                        db.add(node)
                        db.flush()
                except _IE:
                    # P32-D12 / DB32-13 — race on content_hash UNIQUE constraint.
                    # SAVEPOINT already rolled back; outer transaction is intact.
                    dup = (
                        db.query(KnowledgeNode)
                        .filter(KnowledgeNode.content_hash == h)
                        .first()
                    )
                    duplicates += 1
                    results.append({
                        "title": title,
                        "status": "duplicate",
                        "node_id": int(dup.id) if dup is not None else None,
                    })
                    continue
                created += 1
                results.append({"title": title, "status": "ok", "node_id": int(node.id)})
            except Exception as exc:  # noqa: BLE001
                failed += 1
                # P31-OBS7: exc_info=True preserves the traceback so failures
                # in bulk-import (often shape-drift in inputs) are debuggable
                # from logs alone; type(exc).__name__ adds class context to
                # the WARN line.
                logger.warning(
                    "bulk-import: failed for %r: %s: %s",
                    title[:60], type(exc).__name__, exc,
                    exc_info=True,
                )
                results.append({"title": title, "status": "failed", "error": str(exc)[:200]})
        db.flush()  # P-IDEMP: flush only — outer db.commit() after lookup_or_record commits business rows + idempotency row in ONE transaction (matches live-trade/promote single-session pattern; closes split-transaction window)
        return (
            {
                "created": created,
                "duplicates": duplicates,
                "failed": failed,
                "results": results,
            },
            201,
        )

    outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)
    # P34-IDEMP: get_db() never commits and lookup_or_record only flushes the
    # idempotency row — commit it so replay protection (body-hash conflict +
    # cached response) survives teardown instead of being silently rolled back.
    db.commit()
    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.post("/api/admin/backfill-past-alpha")
def api_admin_backfill_past_alpha(
    request: Request,
    principal: str = Depends(require_operator),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """P6-D05 + D-M9/P16: one-shot backfill of past_alpha node kinds.

    Walks every AlphaStrategy that already has a postmortem and bumps each
    referenced source KnowledgeNode to ``kind=past_alpha``. Used by the
    operator when upgrading an existing deployment (new postmortems handle
    this automatically going forward).

    D-M9 — wrapped in Idempotency-Key keyed on today's date so a retry replays
    the cached result instead of re-scanning every AlphaStrategy.
    """
    from datetime import datetime as _dt, timezone as _tz
    from backend.core.database import KIND_PAST_ALPHA, KIND_POSTMORTEM, AlphaStrategy as _Strat
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    ymd = _dt.now(_tz.utc).strftime("%Y-%m-%d")
    body_payload = {"endpoint": "backfill_past_alpha", "ymd": ymd}
    req_hash = canonical_request_hash(body_payload)

    def _compute() -> Tuple[Dict[str, Any], int]:
        bumped = 0
        skipped_no_sources = 0
        seen_strategies = 0
        for st in db.query(_Strat).all():
            cfg = st.config() or {}
            ids_raw = cfg.get("source_node_ids") or []
            ids: List[int] = []
            for x in ids_raw:
                try:
                    ids.append(int(x))
                except (TypeError, ValueError):
                    continue
            if not ids:
                skipped_no_sources += 1
                continue
            seen_strategies += 1
            updated = (
                db.query(KnowledgeNode)
                .filter(KnowledgeNode.id.in_(ids))
                .filter(KnowledgeNode.kind != KIND_POSTMORTEM)
                .filter(KnowledgeNode.kind != KIND_PAST_ALPHA)
                .update({"kind": KIND_PAST_ALPHA}, synchronize_session=False)
            )
            bumped += int(updated or 0)
        # Do NOT commit here — the outer db.commit() at line 876 covers both
        # the KnowledgeNode updates and the idempotency row atomically.
        # An early commit here would leave a committed-but-unrecorded state
        # if lookup_or_record raises before persisting the idempotency row.
        return (
            {
                "strategies_scanned": seen_strategies + skipped_no_sources,
                "strategies_with_sources": seen_strategies,
                "nodes_bumped_to_past_alpha": bumped,
            },
            200,
        )

    outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)
    # P34-IDEMP: get_db() never commits and lookup_or_record only flushes — commit
    # so a retried backfill replays the cached result instead of re-scanning.
    db.commit()
    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.post("/api/knowledge/{node_id}/relink")
def post_knowledge_relink(
    node_id: int,
    request: Request,
    principal: str = Depends(require_operator),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """P6-B3 + D-M8/P16: manually trigger cross-link recomputation for one node.

    Returns the freshly added neighbour IDs. Honours ``KB_RELINK_THRESHOLD``
    and ``KB_RELINK_TOP_K`` env knobs. No-op (HTTP 412) if sklearn missing.

    D-M8 — wrapped in Idempotency-Key so a retry replays the cached neighbour
    set instead of running the (potentially expensive) TF-IDF recomputation a
    second time.
    """
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    body_payload = {"endpoint": "knowledge_relink", "node_id": int(node_id)}
    req_hash = canonical_request_hash(body_payload)

    def _compute() -> Tuple[Dict[str, Any], int]:
        node = db.get(KnowledgeNode, node_id)
        if node is None:
            return ({"error": "not_found", "detail": f"KnowledgeNode {node_id} not found"}, 404)
        from backend.core.kb_relink import relink_node
        added = relink_node(db, node_id)
        # P-IDEMP: no inner commit — relink_node already flushed; outer db.commit()
        # after lookup_or_record commits the link write + idempotency row atomically
        # (matches live-trade/promote single-session pattern).
        return (
            {
                "node_id": node_id,
                "added_links": added,
                "total_links_now": len(node.link_list()),
            },
            200,
        )

    outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)
    # P34-IDEMP: get_db() never commits and lookup_or_record only flushes — commit
    # so a retried relink replays the cached set instead of re-running TF-IDF.
    db.commit()
    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.get("/api/strategies")
def list_strategies(principal: str = Depends(require_operator), db: Session = Depends(get_db)) -> Dict[str, Any]:
    rows = (
        db.query(AlphaStrategy)
        .order_by(AlphaStrategy.id.desc())
        .limit(_LIST_HARD_LIMIT)
        .all()
    )
    return {"strategies": [r.to_dict() for r in rows]}


@app.get("/api/strategies/{strategy_id}/concepts")
def api_strategy_concepts(strategy_id: int, db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """P6-M17: list the KnowledgeNodes referenced by a strategy's config.

    Reads ``config.source_node_ids`` (populated by the Researcher agent when
    a story is built from existing KB nodes) and joins to ``knowledge_nodes``
    so the StrategyDetail page can render concept chips with one-click jump
    to /kb-explorer. Empty list when the strategy was bootstrapped without
    referencing the KB.
    """
    st = db.get(AlphaStrategy, strategy_id)
    if st is None:
        raise HTTPException(status_code=404, detail=f"AlphaStrategy {strategy_id} not found")
    cfg = st.config() or {}
    raw_ids = cfg.get("source_node_ids") or []
    int_ids: list[int] = []
    for x in raw_ids:
        try:
            int_ids.append(int(x))
        except (TypeError, ValueError):
            continue
    if not int_ids:
        return {"strategy_id": strategy_id, "concepts": []}
    nodes = db.query(KnowledgeNode).filter(KnowledgeNode.id.in_(int_ids)).all()
    return {
        "strategy_id": strategy_id,
        "concepts": [
            {
                "id": n.id,
                "title": n.title,
                "kind": n.kind_value(),
                "category": n.category,
                "ic_score": float(n.ic_score or 0.0),
                "source_url": n.source_url,
                "tags": n.tag_list(),
            }
            for n in nodes
        ],
    }


@app.get("/api/strategies/{strategy_id}")
def get_strategy(strategy_id: int, principal: str = Depends(require_operator), db: Session = Depends(get_db)) -> Dict[str, Any]:
    row = db.get(AlphaStrategy, strategy_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"AlphaStrategy {strategy_id} not found")
    out = row.to_dict()
    # If a results file exists, attach equity curve so the frontend's Tab 3
    # can chart it without a second roundtrip.
    out["equity_curve"] = []
    results_path = PROJECT_ROOT / "storage" / "results" / f"strategy_{strategy_id}.json"
    if results_path.exists():
        try:
            data = json.loads(results_path.read_text(encoding="utf-8"))
            out["equity_curve"] = data.get("equity_curve", [])
            # Trim per_bar out of raw_backtest — it can be 8760+ rows and the
            # frontend has a dedicated endpoint for it. Keep summary fields.
            out["raw_backtest"] = {
                k: v for k, v in data.items()
                if k not in {"equity_curve", "per_bar"}
            }
            out["raw_backtest"]["per_bar_available"] = bool(data.get("per_bar"))
        except Exception as exc:  # noqa: BLE001
            # P13/D-M4 — log + surface so the UI can render an inline error
            # banner instead of silently showing an empty equity curve.
            logger.exception(
                "get_strategy: results file unreadable for strategy %s", strategy_id
            )
            # P32-D9 / OBS32-12 — leak only the exception class, never the
            # message (may contain DB paths, secret tokens, or internal IDs).
            out.setdefault("raw_backtest_error", type(exc).__name__)
    return out


@app.get("/api/strategies/{strategy_id}/trades")
def get_strategy_trades(
    strategy_id: int,
    offset: int = 0,
    limit: int = 500,
    nonzero_only: bool = False,
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """Per-bar trade tape for the BACKTEST CSV tab.

    Read from the on-disk `storage/results/strategy_{id}.json` written by the
    orchestrator. Old results files (pre-P5) lack `per_bar` — those return
    HTTP 409 instructing the operator to rerun the pipeline.

    Pagination: default 500 rows; max 2000 per request. Use `nonzero_only=true`
    to drop bars where `signal == 0` — typically reduces an 8760-row payload
    to a few hundred rows.
    """
    limit = max(1, min(2000, int(limit)))
    offset = max(0, int(offset))
    results_path = PROJECT_ROOT / "storage" / "results" / f"strategy_{strategy_id}.json"
    if not results_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No backtest results for strategy {strategy_id}. Run the pipeline first.",
        )
    try:
        data = json.loads(results_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"results file unreadable: {type(exc).__name__}") from exc
    per_bar = data.get("per_bar")
    if not per_bar:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Per-bar trade tape missing for strategy {strategy_id}. "
                "Rerun the pipeline to regenerate (per-bar data was added in P5)."
            ),
        )
    if nonzero_only:
        per_bar = [r for r in per_bar if abs(float(r.get("signal", 0.0) or 0.0)) > 1e-14]
    total = len(per_bar)
    window = per_bar[offset:offset + limit]
    return {
        "strategy_id": strategy_id,
        "total": total,
        "offset": offset,
        "limit": limit,
        "nonzero_only": nonzero_only,
        "rows": window,
    }


# ---------------------------------------------------------------------------
# Graph payload — combined view of nodes + strategies for vis-network.
# Uses the 4-color knowledge classification (kind) from the reference UI.
# ---------------------------------------------------------------------------

# Reference palette — derived from the YouTube demo's force-directed view.
KIND_COLORS: Dict[str, str] = {
    KIND_PAST_ALPHA: "#EF4444",   # red (dominant cluster in the demo)
    KIND_CONCEPT: "#22C55E",      # green
    KIND_ACTIVE: "#22D3EE",       # cyan (bright/larger anchor nodes)
    KIND_POSTMORTEM: "#D946EF",   # magenta (rare class)
}

# Strategy-status colors used on the same canvas. Picked so a strategy node
# never collides with the knowledge-kind palette above.
STRATEGY_STATUS_COLORS: Dict[str, str] = {
    "APPROVED": "#F59E0B",        # amber
    "PAPER_TRADE": "#F97316",     # orange
    "SMALL_CAPITAL": "#FB923C",   # lighter orange
    "LIVE": "#10B981",            # emerald
    "REJECTED": "#6B7280",        # slate-500
    "GRAVEYARD": "#475569",       # slate-600
}
STRATEGY_DEFAULT_COLOR = "#A855F7"  # purple (in-progress)


def _strategy_color(status: str | None) -> str:
    if not status:
        return STRATEGY_DEFAULT_COLOR
    return STRATEGY_STATUS_COLORS.get(status.upper(), STRATEGY_DEFAULT_COLOR)


@app.get("/api/graph")
def graph_payload(principal: str = Depends(require_operator), db: Session = Depends(get_db)) -> Dict[str, Any]:
    # P12-B-H4 — every node now carries the extra metadata the new graph
    # NodeInspector overlay needs (category, tags, ic_score, source_url,
    # out_degree). out_degree is computed in a second pass after edges are
    # filtered, so the count reflects what the frontend actually renders.
    nodes_payload: List[Dict[str, Any]] = []
    edges_payload: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    # P-FIX: cap nodes server-side so a 13k+ KB can't ship a multi-MB payload
    # that locks the vis-network physics layout / OOMs the tab. Keep the most
    # relevant nodes (highest ic_score, then newest). Mirrors _LIST_HARD_LIMIT
    # used by /api/knowledge (line 381), /api/strategies (833), sources (1590).
    for kn in (
        db.query(KnowledgeNode)
        .order_by(KnowledgeNode.ic_score.desc().nullslast(), KnowledgeNode.id.desc())
        .limit(_LIST_HARD_LIMIT)
        .all()
    ):
        nid = f"k{kn.id}"
        seen_ids.add(nid)
        kind = kn.kind_value()
        nodes_payload.append(
            {
                "id": nid,
                "label": f"K#{kn.id} {kn.title[:36]}",
                "title": kn.title,
                "kind": "knowledge",
                "node_kind": kind,
                "stage": 0,
                "status": "INTAKE",
                "color": KIND_COLORS.get(kind, KIND_COLORS[KIND_CONCEPT]),
                # P12-B-H4 — inspector-overlay metadata.
                "category": kn.category,
                "tags": kn.tag_list(),
                "ic_score": float(kn.ic_score or 0.0),
                "source_url": kn.source_url,
                "out_degree": 0,  # back-filled in the post-pass below
            }
        )
        for tgt in kn.link_list():
            edges_payload.append({"from": nid, "to": f"k{tgt}"})

    # P-FIX: same server-side cap for strategy nodes.
    for st in (
        db.query(AlphaStrategy)
        .order_by(AlphaStrategy.id.desc())
        .limit(_LIST_HARD_LIMIT)
        .all()
    ):
        sid = f"s{st.id}"
        seen_ids.add(sid)
        stage = int(st.stage or 0)
        cfg = st.config() or {}
        metrics = st.metrics() or {}
        nodes_payload.append(
            {
                "id": sid,
                "label": f"S#{st.id} {st.name[:36]}",
                "title": st.name,
                "kind": "strategy",
                "node_kind": "strategy",
                "stage": stage,
                "status": st.status,
                "color": _strategy_color(st.status),
                # P12-B-H4 — inspector-overlay metadata. Strategies don't have
                # tags / source_url today; we expose stable defaults so the
                # frontend never has to special-case node.kind === 'strategy'.
                "category": cfg.get("alpha_category"),
                "tags": [],
                "ic_score": float(metrics.get("annualized_sharpe") or 0.0),
                "source_url": None,
                "out_degree": 0,
            }
        )
        for source in (st.config().get("source_node_ids") or []):
            try:
                source_id = f"k{int(source)}"
            except (TypeError, ValueError):
                continue
            edges_payload.append({"from": source_id, "to": sid})

    # Filter edges that reference missing nodes (avoids vis-network warnings).
    edges_payload = [e for e in edges_payload if e["from"] in seen_ids and e["to"] in seen_ids]

    # P12-B-H4 — compute undirected degree from the final (filtered) edge set
    # and back-fill onto each node. Matches the frontend KnowledgeGraph
    # degree calculation so the inspector overlay shows the same number that
    # drove the dot size.
    deg: Dict[str, int] = {}
    for e in edges_payload:
        a = str(e.get("from") or "")
        b = str(e.get("to") or "")
        if a:
            deg[a] = deg.get(a, 0) + 1
        if b:
            deg[b] = deg.get(b, 0) + 1
    for n in nodes_payload:
        n["out_degree"] = int(deg.get(n["id"], 0))

    return {"nodes": nodes_payload, "edges": edges_payload}


# ---------------------------------------------------------------------------
# Mission Control endpoints — agent personas + pipeline bucket counts.
# Read-only aggregates the dashboard polls every few seconds.
# ---------------------------------------------------------------------------

@app.get("/api/agents")
def api_agents() -> Dict[str, Any]:
    """Return the registered agent personas in stable order."""
    return {"agents": list_personas()}


# Display labels for the 8 buckets shown on Mission Control + the strategy
# stepper. Kept here (not on the frontend) so backend status changes never
# silently desync the UI.
_PIPELINE_BUCKETS: List[Dict[str, Any]] = [
    {"index": 0, "key": "alpha_ideas",     "label": "Alpha Ideas",     "statuses": ["INTAKE"]},
    {"index": 1, "key": "research",        "label": "Research",        "statuses": ["STORY_GEN"]},
    {"index": 2, "key": "factor_dev",      "label": "Factor Dev",      "statuses": ["CODE_GEN"]},
    {"index": 3, "key": "full_backtest",   "label": "Full Backtest",   "statuses": ["BACKTESTING", "CRITIC_LOOP"]},
    {"index": 4, "key": "paper_trade",     "label": "Paper Trade",     "statuses": ["APPROVED", "PAPER_TRADE"]},
    {"index": 5, "key": "small_capital",   "label": "Small Capital",   "statuses": ["SMALL_CAPITAL"]},
    {"index": 6, "key": "live",            "label": "Live",            "statuses": ["LIVE"]},
    # P7 — PAUSED joins the Graveyard bucket since it's a terminal-ish state
    # awaiting operator resume. Still visually distinct via a paused chip in UI.
    {"index": 7, "key": "graveyard",       "label": "Graveyard",       "statuses": ["REJECTED", "GRAVEYARD", "PAUSED"]},
]


# P8-FIX/C-4: V2 pipeline buckets align with the spec's "Stage 0~5" framing.
# SMALL_CAPITAL + LIVE are merged into Stage 5 (the UI distinguishes them
# with a sub-pill); Graveyard moves from index 7 -> 6. Backwards-compatible:
# V1 endpoint stays live and front-end opts into V2 via /api/v2/pipeline/...
_PIPELINE_BUCKETS_V2: List[Dict[str, Any]] = [
    {"index": 0, "key": "alpha_ideas",   "label": "Alpha Ideas",   "statuses": ["INTAKE"]},
    {"index": 1, "key": "research",      "label": "Research",      "statuses": ["STORY_GEN"]},
    {"index": 2, "key": "factor_dev",    "label": "Factor Dev",    "statuses": ["CODE_GEN"]},
    {"index": 3, "key": "full_backtest", "label": "Full Backtest", "statuses": ["BACKTESTING", "CRITIC_LOOP"]},
    {"index": 4, "key": "paper_trade",   "label": "Paper Trade",   "statuses": ["APPROVED", "PAPER_TRADE"]},
    {
        "index": 5,
        "key": "live_trade",
        "label": "Live Trade",
        "statuses": ["SMALL_CAPITAL", "LIVE"],
        "sub_pills": [
            {"key": "small_capital", "label": "Small Cap", "statuses": ["SMALL_CAPITAL"]},
            {"key": "live",          "label": "Live",      "statuses": ["LIVE"]},
        ],
    },
    {"index": 6, "key": "graveyard",     "label": "Graveyard",     "statuses": ["REJECTED", "GRAVEYARD", "PAUSED"]},
]


@app.get("/api/pipeline/buckets")
def api_pipeline_buckets(db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """Return per-bucket strategy counts for the Mission Control pipeline row."""
    counts: Dict[str, int] = {}
    rows = db.query(AlphaStrategy.status).all()
    raw_statuses = [(r[0] or "").upper() for r in rows]
    for bucket in _PIPELINE_BUCKETS:
        wanted = {s.upper() for s in bucket["statuses"]}
        counts[bucket["key"]] = sum(1 for s in raw_statuses if s in wanted)
    return {
        "buckets": [
            {**bucket, "count": counts[bucket["key"]]}
            for bucket in _PIPELINE_BUCKETS
        ],
        "total": len(raw_statuses),
    }


@app.get("/api/v2/pipeline/buckets")
def api_pipeline_buckets_v2(db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """P8-FIX/C-4: V2 pipeline buckets — aligns with the spec's Stage 0~5 framing.

    SMALL_CAPITAL + LIVE are merged into a single Stage 5 (Live Trade);
    each merged bucket includes a ``sub_pills`` list so the UI can still
    show the small-cap / full-live distinction.
    """
    rows = db.query(AlphaStrategy.status).all()
    raw_statuses = [(r[0] or "").upper() for r in rows]
    out_buckets: List[Dict[str, Any]] = []
    for bucket in _PIPELINE_BUCKETS_V2:
        wanted = {s.upper() for s in bucket["statuses"]}
        total = sum(1 for s in raw_statuses if s in wanted)
        sub_pills_out: List[Dict[str, Any]] = []
        for sp in bucket.get("sub_pills", []) or []:
            sp_wanted = {s.upper() for s in sp["statuses"]}
            sp_count = sum(1 for s in raw_statuses if s in sp_wanted)
            sub_pills_out.append({**sp, "count": sp_count})
        out_buckets.append({
            "index": bucket["index"],
            "key": bucket["key"],
            "label": bucket["label"],
            "statuses": bucket["statuses"],
            "count": total,
            "sub_pills": sub_pills_out,
        })
    return {
        "schema_version": 2,
        "buckets": out_buckets,
        "total": len(raw_statuses),
    }


@app.get("/api/daemon-log")
def api_daemon_log(limit: int = 50, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """Return recent in-flight pipeline log lines for the Mission Control daemon panel.

    Pulls from the orchestrator's in-process `_REGISTRY` ring buffer; resilient
    to a missing registry (returns an empty list).
    """
    from backend.core.orchestrator import _REGISTRY, _REGISTRY_LOCK  # noqa: WPS437 (intentional)

    capped = max(1, min(limit, 500))
    rolled: List[Dict[str, Any]] = []
    with _REGISTRY_LOCK:
        states = list(_REGISTRY.values())
    # Carry a per-state insertion sequence (oldest-last => higher seq is newer)
    # purely for ordering; the emitted event payload is unchanged.
    indexed: List[tuple] = []
    for state in states:
        tail = state.logs[-capped:]
        for seq, line in enumerate(tail):
            # Timestamp prefix '[YYYY-MM-DDTHH:MM:SSZ]' is fixed-width ISO, so it
            # sorts chronologically. Falls back to the whole line if no ']' present.
            ts_prefix = line.split("]", 1)[0]
            indexed.append((ts_prefix, seq, {
                "strategy_id": state.strategy_id,
                "status": state.status.value,
                "agent": state.active_agent,
                "line": line,
            }))
    # Newest first: by second-resolution timestamp, then insertion order within a
    # tied second (NEVER by message body). reverse=True puts newer seconds and the
    # later-appended (newer) lines within a second first.
    indexed.sort(key=lambda t: (t[0], t[1]), reverse=True)
    rolled = [item[2] for item in indexed]
    return {"events": rolled[:capped]}


@app.get("/api/agent-dialogue")
def api_agent_dialogue(
    strategy_id: Optional[int] = None,
    limit: int = 200,
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """P8-FIX/H-7: agent self-dialogue transcript.

    Returns the ring buffer of inter-agent hand-offs / critiques / approvals
    that the orchestrator records during each pipeline run. Newest first.
    Filtered to a single strategy when ``strategy_id`` is supplied.
    """
    from backend.core.agent_dialogue import recent_dialogue, buffer_size
    sid = int(strategy_id) if strategy_id is not None else None
    turns = recent_dialogue(strategy_id=sid, limit=limit)
    return {
        "turns": turns,
        "buffer_size": buffer_size(),
        "strategy_id": sid,
        "limit": max(1, min(1000, int(limit or 200))),
    }


# ---------------------------------------------------------------------------
# Mission 5 endpoints — orchestrator integration.
# Imported lazily so this module can be loaded by simple smoke tests even if
# the orchestrator file pulls in heavier deps.
# ---------------------------------------------------------------------------

@app.post("/api/pipeline/run", status_code=202)  # R5/BE-API-005: 202 Accepted (async pipeline)
def api_pipeline_run(
    req: PipelineRequest,
    background: BackgroundTasks,
    request: Request,
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """Kick off the full INTAKE -> APPROVED/REJECTED workflow asynchronously.

    D-H1/P16 — wrapped in Idempotency-Key so a flaky-network retry replays the
    same {strategy_id, status_url} pair instead of bootstrapping a second
    AlphaStrategy and spawning a parallel INTAKE→APPROVED pipeline (duplicate
    LLM spend, duplicate KnowledgeNode, duplicate backtest).
    """
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )
    from backend.core.orchestrator import WorkflowOrchestrator

    key = require_idempotency_key(request)
    # Cache key uses the textual prefix + node_ids so the same input is a hit.
    body_payload = {
        "endpoint": "pipeline_run",
        "raw_text": (req.raw_text or "")[:4096],
        "node_ids": sorted([int(x) for x in (req.node_ids or [])]),
    }
    req_hash = canonical_request_hash(body_payload)

    with session_scope() as db:
        def _compute() -> Tuple[Dict[str, Any], int]:
            orchestrator = WorkflowOrchestrator()
            strategy_id = orchestrator.bootstrap_strategy(req.raw_text)
            # NOTE: background.add_task is intentionally NOT called here.
            # Scheduling the background task inside compute_fn() would cause
            # the race-loser (concurrent retry that hits IntegrityError) to
            # still run a pipeline against its own orphaned strategy_id even
            # though lookup_or_record returns the winner's cached payload.
            # The task is scheduled below, conditioned on not outcome.replay.
            return (
                {
                    "strategy_id": strategy_id,
                    "status": "QUEUED",
                    "status_url": f"/api/pipeline/status/{strategy_id}",
                },
                202,  # R5/BE-API-005: persist 202 in the idempotency record too
            )

        outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)

    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)

    # Only the idempotency winner schedules the pipeline background task.
    # A replay (race-loser or client retry) must NOT spawn a second pipeline
    # against the already-running strategy_id stored in the cached payload.
    if not outcome.replay:
        _bg_orchestrator = WorkflowOrchestrator()
        background.add_task(
            _bg_orchestrator.run_full_pipeline_for_id,
            outcome.response_payload["strategy_id"],
            req.raw_text,
            req.node_ids,
        )

    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.get("/api/pipeline/status/{strategy_id}")
def api_pipeline_status(strategy_id: int, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """Mission 5 live status endpoint."""
    from backend.core.orchestrator import WorkflowOrchestrator

    return WorkflowOrchestrator().get_status(strategy_id)


# ---------------------------------------------------------------------------
# Mission 7 — portfolio allocator endpoint.
# ---------------------------------------------------------------------------

class AllocationRequest(BaseModel):
    # P26 — cap list length. allocate_portfolio_weights does a per-id DB
    # `get` + equity-curve file read (backend/core/portfolio.py:86-94) with no
    # internal cap; an unbounded list is a DB+filesystem DoS in one worker
    # thread. 200 >> any real portfolio. Mirrors the file convention (see
    # IntakeRequest comment) — well-behaved clients are unaffected.
    strategy_ids: List[int] = Field(..., min_length=1, max_length=200)


class CombineRequest(BaseModel):
    # P26 — same cap as AllocationRequest. combine_portfolio does a per-id
    # equity-series JSON file read (backend/core/portfolio.py:253-258) with no
    # internal cap.
    strategy_ids: List[int] = Field(..., min_length=1, max_length=200)
    method: str = Field("equal_weight", description="One of /api/portfolio/methods")

    @field_validator("method")
    @classmethod
    def _method_must_be_known(cls, v: str) -> str:
        # Reject unknown methods up front. Without this, allocators.allocate()
        # silently falls back to inverse_vol while the response still echoes the
        # bogus method label, so the UI mislabels the allocation instead of
        # getting a clear 400.
        from backend.core.allocators import METHOD_KEYS

        if v not in METHOD_KEYS:
            raise ValueError(
                f"Unknown method '{v}'. Must be one of: {', '.join(METHOD_KEYS)}"
            )
        return v


@app.post("/api/portfolio/allocate")
def api_portfolio_allocate(
    req: AllocationRequest,
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    from backend.core.portfolio import allocate_portfolio_weights

    weights = allocate_portfolio_weights(req.strategy_ids)
    return {
        "weights": weights,
        "total_weight": round(sum(weights.values()), 6),
    }


@app.get("/api/portfolio/methods")
def api_portfolio_methods() -> Dict[str, Any]:
    """List the 11 weighting methods supported by /api/portfolio/combine (8 reference + 3 P6-M16 BETA)."""
    from backend.core.portfolio import METHOD_KEYS, METHOD_LABELS

    return {
        "methods": [
            {"key": k, "label": METHOD_LABELS.get(k, k)} for k in METHOD_KEYS
        ],
        "default": "equal_weight",
    }


@app.post("/api/portfolio/combine")
def api_portfolio_combine(
    req: CombineRequest,
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """Run a combined-portfolio backtest with the chosen weighting method.

    Returns {method, method_label, weights, equity_curve, metrics, missing}.
    """
    from backend.core.portfolio import combine_portfolio

    return combine_portfolio(req.strategy_ids, method=req.method)


# ---------------------------------------------------------------------------
# P2 — BG PIPELINE GATE CRITERIA panel.
# ---------------------------------------------------------------------------

# Hard-coded for now; lives here so the frontend has a single source of truth.
# Lines flagged here MUST stay in lockstep with `backend/agents/critic.py`'s
# hard rejection thresholds (`review_strategy()`).
from backend.core.thresholds import (
    GATE_DISPLAY_MAX_DRAWDOWN,
    MIN_PROFIT_FACTOR,
    MIN_SHARPE,
    MIN_TRADES_BACKTEST,
)

_GATE_DD_PCT = int(round(abs(GATE_DISPLAY_MAX_DRAWDOWN) * 100))

_GATE_CRITERIA: List[Dict[str, Any]] = [
    {
        "key": "min_avg_sharpe",
        "label": f"Avg Sharpe > {MIN_SHARPE}",
        "metric": "annualized_sharpe",
        "operator": ">",
        "threshold": MIN_SHARPE,
        "severity": "blocker",
    },
    {
        "key": "max_drawdown_limit",
        "label": f"Worst MaxDD > -{_GATE_DD_PCT}%",
        "metric": "max_drawdown",
        "operator": ">",
        "threshold": GATE_DISPLAY_MAX_DRAWDOWN,
        "severity": "blocker",
    },
    {
        "key": "all_sharpe_positive",
        "label": "All Sharpe > 0",
        "metric": "annualized_sharpe",
        "operator": ">",
        "threshold": 0.0,
        "severity": "warning",
    },
    {
        "key": "min_trades",
        "label": f"Min Trades >= {MIN_TRADES_BACKTEST}",
        "metric": "trades",
        "operator": ">=",
        "threshold": MIN_TRADES_BACKTEST,
        "severity": "blocker",
    },
    {
        "key": "min_profit_factor",
        "label": f"Profit Factor >= {MIN_PROFIT_FACTOR}",
        "metric": "profit_factor",
        "operator": ">=",
        "threshold": MIN_PROFIT_FACTOR,
        "severity": "blocker",
    },
]


# ---------------------------------------------------------------------------
# P-EDIT — operator-tunable gate-criteria thresholds.
#
# _GATE_CRITERIA above is the DISPLAY/evaluation checklist surfaced on
# /gate-review and /backtest-panel. It is read ONLY by the /api/gate-criteria
# endpoints — the Critic's hard-rejection floor (backend/agents/critic.py) and
# the paper-trade health gate (backend/core/paper_trader.py) read the
# backend/core/thresholds.py constants DIRECTLY and are intentionally NOT
# affected by edits here. So tuning these values is a safe "what-if" of the
# checklist, never a silent change to trading/promotion behaviour.
#
# Operators retune thresholds from the UI; overrides persist to
# storage/gate_criteria_overrides.json (keyed by rule `key`) and are merged
# over the defaults on every read. Only the numeric threshold is editable;
# severity stays fixed. `scale` maps the canonical stored threshold to the
# operator-facing value (display = canonical * scale, and the inverse on save)
# so MaxDD stores -0.25 but is shown/edited as -25 with unit "%".
# ---------------------------------------------------------------------------
_GATE_EDIT_META: Dict[str, Dict[str, Any]] = {
    "min_avg_sharpe":      {"name": "Avg Sharpe",    "unit": "",  "kind": "float", "step": 0.1,  "min": -5.0,    "max": 10.0,     "scale": 1.0},
    "max_drawdown_limit":  {"name": "Worst MaxDD",   "unit": "%", "kind": "float", "step": 1.0,  "min": -100.0,  "max": 0.0,      "scale": 100.0},
    "all_sharpe_positive": {"name": "All Sharpe",    "unit": "",  "kind": "float", "step": 0.1,  "min": -5.0,    "max": 10.0,     "scale": 1.0},
    "min_trades":          {"name": "Min Trades",    "unit": "",  "kind": "int",   "step": 1.0,  "min": 0.0,     "max": 100000.0, "scale": 1.0},
    "min_profit_factor":   {"name": "Profit Factor", "unit": "",  "kind": "float", "step": 0.05, "min": 0.0,     "max": 100.0,    "scale": 1.0},
}

_GATE_OVERRIDES_PATH = PROJECT_ROOT / "storage" / "gate_criteria_overrides.json"
_GATE_OVERRIDES_LOCK = threading.Lock()


def _gate_is_finite(v: float) -> bool:
    """True iff v is a real finite float (rejects NaN and ±inf without math)."""
    return v == v and float("-inf") < v < float("inf")


def _gate_format_label(rule: Dict[str, Any]) -> str:
    """Regenerate a rule's human label from its (possibly overridden) threshold,
    preserving the original phrasing (e.g. 'Worst MaxDD > -25%')."""
    meta = _GATE_EDIT_META.get(rule["key"], {})
    op = rule.get("operator", ">")
    scale = float(meta.get("scale", 1.0) or 1.0)
    disp = float(rule["threshold"]) * scale
    unit = meta.get("unit", "")
    name = meta.get("name") or rule["key"]
    disp_s = f"{int(round(disp))}" if meta.get("kind") == "int" else f"{disp:g}"
    return f"{name} {op} {disp_s}{unit}"


def _load_gate_overrides() -> Dict[str, Dict[str, Any]]:
    """Read persisted threshold overrides (keyed by rule key). Tolerant of a
    missing or malformed file → returns {}."""
    try:
        with _GATE_OVERRIDES_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for k, v in data.items():
        if isinstance(v, dict) and "threshold" in v:
            out[str(k)] = v
    return out


def _save_gate_overrides(overrides: Dict[str, Dict[str, Any]]) -> None:
    """Atomically persist overrides to storage/gate_criteria_overrides.json."""
    _GATE_OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _GATE_OVERRIDES_PATH.with_name(_GATE_OVERRIDES_PATH.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(overrides, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, _GATE_OVERRIDES_PATH)  # atomic on POSIX


def _effective_gate_criteria() -> List[Dict[str, Any]]:
    """_GATE_CRITERIA with persisted operator overrides merged in (threshold +
    regenerated label). Unknown / malformed overrides are ignored so a bad file
    can never break the endpoint."""
    overrides = _load_gate_overrides()
    rules: List[Dict[str, Any]] = []
    for base in _GATE_CRITERIA:
        rule = dict(base)
        ov = overrides.get(rule["key"])
        if ov is not None:
            try:
                thr = float(ov["threshold"])
            except (TypeError, ValueError, KeyError):
                thr = None
            if thr is not None and _gate_is_finite(thr):
                rule["threshold"] = thr
                rule["label"] = _gate_format_label(rule)
        rules.append(rule)
    return rules


def _gate_rule_public(rule: Dict[str, Any]) -> Dict[str, Any]:
    """Attach operator-facing edit metadata (display value + unit + bounds) so
    the UI can render a friendly editable control for each rule."""
    meta = _GATE_EDIT_META.get(rule["key"], {})
    scale = float(meta.get("scale", 1.0) or 1.0)
    out = dict(rule)
    out["editable"] = {
        "value": round(float(rule["threshold"]) * scale, 6),
        "unit": meta.get("unit", ""),
        "kind": meta.get("kind", "float"),
        "step": meta.get("step", 0.1),
        "min": meta.get("min"),
        "max": meta.get("max"),
        "scale": scale,
    }
    return out


# ---------------------------------------------------------------------------
# P3 — Source management (CRUD + manual poll)
# ---------------------------------------------------------------------------


class SourceCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=512)
    source_type: str = Field(..., description=f"One of: {', '.join(SOURCE_TYPES)}")
    url: str = Field(..., min_length=1, max_length=1024)
    cadence_minutes: int = Field(60, ge=5, le=60 * 24 * 7)
    enabled: bool = Field(True)
    category: Optional[str] = Field(None, max_length=32)

    # P29-C7: SSRF guard. Resolve hostname; reject RFC1918/loopback/link-local.
    @field_validator("url")
    @classmethod
    def _validate_url_no_ssrf(cls, v: str) -> str:
        import ipaddress
        import socket
        from urllib.parse import urlparse

        raw = (v or "").strip()
        if not raw:
            raise ValueError("url must not be empty")
        # P31-D2: canonical strict-whitelist env_bool.
        from backend._envloader import env_bool as _env_bool_local
        if _env_bool_local("ALPHA_ALLOW_LOCAL_INGEST", False):
            return raw
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                f"url scheme must be http or https (got {scheme!r})"
            )
        host = (parsed.hostname or "").strip()
        if not host:
            raise ValueError("url must contain a hostname")
        try:
            ip_literal = ipaddress.ip_address(host)
            candidates = [ip_literal]
        except ValueError:
            try:
                infos = socket.getaddrinfo(host, None)
            except (socket.gaierror, OSError) as exc:
                raise ValueError(
                    f"url host {host!r} could not be resolved ({exc})"
                ) from exc
            candidates = []
            for info in infos:
                sockaddr = info[4]
                if sockaddr and sockaddr[0]:
                    try:
                        # Strip IPv6 zone IDs (e.g. 'fe80::1%eth0') before parsing.
                        addr_str = sockaddr[0].split("%")[0]
                        candidates.append(ipaddress.ip_address(addr_str))
                    except ValueError:
                        continue
            if not candidates:
                raise ValueError(
                    f"url host {host!r} resolved to no parseable IP addresses; "
                    "refusing for SSRF safety"
                )
        for ip in candidates:
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                raise ValueError(
                    f"url host {host!r} resolves to a non-public address "
                    f"({ip}); refusing for SSRF safety"
                )
        return raw


class SourceUpdateRequest(BaseModel):
    name: Optional[str] = None
    cadence_minutes: Optional[int] = Field(None, ge=5, le=60 * 24 * 7)
    enabled: Optional[bool] = None
    category: Optional[str] = Field(None, max_length=32)


# URL-pattern map used both at create-time (auto-suggest a category when the
# caller omits one) and by the one-off bootstrap that backfills the column on
# pre-P5 rows. Patterns are checked in order; first match wins.
_CATEGORY_URL_PATTERNS = (
    ("youtube",      ("youtube.com", "youtu.be")),
    ("research",     ("arxiv.org", "researchgate", "ssrn.com", "papers.")),
    ("tradeview",    ("tradingview.com",)),
    ("course",       ("udemy.com", "coursera.org", "khanacademy")),
    ("cloud",        ("aws.amazon.com", "console.cloud.google", "azure.")),
    ("grafana_data", ("grafana.", "datadog.")),
    ("ai_image",     ("midjourney.", "dalle.")),
    ("ai",           ("openai.com", "anthropic.com", "huggingface.")),
    ("quant_fund",   ("twosigma", "renaissance", "citadel")),
    ("algo_trading", ("binance.", "okx.com", "bybit.", "ftx.")),
    ("dps",          ("dps.", "dropbox.com")),
    ("invoice",      ("stripe.com", "invoice.")),
    ("trading_tool", ("glassnode", "amberdata", "coinmetrics")),
    ("apps",         ("github.com", "gitlab.com", "appstore.")),
)


def _guess_category(url: str) -> Optional[str]:
    u = (url or "").lower()
    if not u:
        return None
    for cat, patterns in _CATEGORY_URL_PATTERNS:
        for p in patterns:
            if p in u:
                return cat
    return None


def _validate_category(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    from backend.core.database import SOURCE_CATEGORIES

    if s not in SOURCE_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"category must be one of {list(SOURCE_CATEGORIES)}",
        )
    return s


def _backfill_source_categories(db: Session) -> int:
    """One-off: assign a category to any row that has none, based on the URL.

    Runs cheaply on /api/sources GET; idempotent (no-op when nothing missing).
    Returns count of rows updated.
    """
    rows = db.query(IngestSource).filter(IngestSource.category.is_(None)).all()
    n = 0
    for r in rows:
        guess = _guess_category(r.url or "")
        if guess:
            r.category = guess
            n += 1
    if n:
        db.commit()
    return n


@app.get("/api/sources")
def api_sources_list(principal: str = Depends(require_operator), db: Session = Depends(get_db)) -> Dict[str, Any]:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func

    from backend.core.database import SOURCE_CATEGORIES

    # Best-effort backfill so the categorical-tab UI never starts empty.
    _backfill_source_categories(db)

    rows = (
        db.query(IngestSource)
        .order_by(IngestSource.id.desc())
        .limit(_LIST_HARD_LIMIT)
        .all()
    )
    # Aggregate event counts for the trailing 24 h in a single GROUP BY.
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    events_24h_map: Dict[int, int] = {
        int(sid): int(cnt)
        for sid, cnt in db.query(
            IngestEvent.source_id, func.count(IngestEvent.id)
        ).filter(IngestEvent.fetched_at >= since).group_by(IngestEvent.source_id).all()
    }
    sources_out: List[Dict[str, Any]] = []
    for r in rows:
        d = r.to_dict()
        d["events_24h"] = int(events_24h_map.get(int(r.id), 0))
        sources_out.append(d)
    return {
        "sources": sources_out,
        "supported_types": list(SOURCE_TYPES),
        "supported_categories": list(SOURCE_CATEGORIES),
    }


@app.get("/api/sources/{source_id}")
def api_sources_get(source_id: int, principal: str = Depends(require_operator), db: Session = Depends(get_db)) -> Dict[str, Any]:
    row = db.get(IngestSource, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"IngestSource {source_id} not found")
    return row.to_dict()


@app.post("/api/sources", status_code=201)
def api_sources_create(
    req: SourceCreateRequest,
    request: Request,
    principal: str = Depends(require_operator),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """D-M1+D-M18/P16 — idempotent + dedup'd source create.

    * Wrapped in Idempotency-Key: retry of the same payload replays the same
      response (no second row created).
    * Even with a fresh key, a (source_type, url) collision returns 409 with
      the existing row's id so the operator can find it instead of seeing a
      silently failed "duplicate" outcome.
    """
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )
    from backend.core.ingest_fetchers import is_stub_source_type

    key = require_idempotency_key(request)
    body_payload = {
        "endpoint": "sources_create",
        "name": req.name.strip(),
        "source_type": req.source_type,
        "url": req.url.strip(),
        "cadence_minutes": int(req.cadence_minutes),
        "enabled": bool(req.enabled),
        "category": req.category,
    }
    req_hash = canonical_request_hash(body_payload)

    def _compute() -> Tuple[Dict[str, Any], int]:
        if req.source_type not in SOURCE_TYPES:
            return (
                {"error": "bad_request", "detail": f"source_type must be one of {SOURCE_TYPES}"},
                400,
            )
        # D-M18 — SELECT-before-INSERT dedup on (source_type, url). A second
        # operator submitting the same URL (or the same operator retrying with
        # a FRESH idempotency key) should see the existing source instead of a
        # duplicate row.
        normalized_url = req.url.strip()
        existing = (
            db.query(IngestSource)
            .filter(IngestSource.source_type == req.source_type)
            .filter(IngestSource.url == normalized_url)
            .first()
        )
        if existing is not None:
            return (
                {
                    "error": "conflict",
                    "detail": (
                        f"A source already exists for source_type={req.source_type!r}"
                        f" and url={normalized_url!r}."
                    ),
                    "existing_id": int(existing.id),
                },
                409,
            )
        explicit_category = _validate_category(req.category)
        category = explicit_category or _guess_category(req.url) or None
        row = IngestSource(
            name=req.name.strip(),
            source_type=req.source_type,
            url=normalized_url,
            cadence_minutes=req.cadence_minutes,
            enabled=bool(req.enabled),
            category=category,
        )
        db.add(row)
        db.flush()  # R3: atomic with the idempotency row via the single outer commit
        db.refresh(row)
        payload = row.to_dict()
        payload["events_24h"] = 0
        if is_stub_source_type(req.source_type):
            payload["warning"] = (
                f"Source type {req.source_type!r} is currently a stub — content "
                "will not be ingested. Consider creating an 'rss' source pointed "
                "at a Nitter bridge (e.g. https://nitter.privacydev.net/<handle>/rss) "
                "instead."
            )
        return (payload, 201)

    outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)
    # P34-IDEMP: get_db() never commits and lookup_or_record only flushes the
    # idempotency row — commit it so replay protection (body-hash conflict +
    # cached response) survives teardown instead of being silently rolled back.
    db.commit()
    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.patch("/api/sources/{source_id}")
def api_sources_update(
    source_id: int,
    req: SourceUpdateRequest,
    request: Request,
    principal: str = Depends(require_operator),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """D-M2/P16 — idempotent source patch (retry-safe edits, never double-applies)."""
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    body_payload = {
        "endpoint": "sources_update",
        "source_id": int(source_id),
        "payload": {
            "name": req.name,
            "cadence_minutes": req.cadence_minutes,
            "enabled": req.enabled,
            "category": req.category,
        },
    }
    req_hash = canonical_request_hash(body_payload)

    def _compute() -> Tuple[Dict[str, Any], int]:
        row = db.get(IngestSource, source_id)
        if row is None:
            return ({"error": "not_found", "detail": f"IngestSource {source_id} not found"}, 404)
        if req.name is not None:
            row.name = req.name.strip()
        if req.cadence_minutes is not None:
            row.cadence_minutes = int(req.cadence_minutes)
        if req.enabled is not None:
            row.enabled = bool(req.enabled)
            if req.enabled:
                # Re-enable wipes the circuit breaker so the next tick polls.
                row.disabled_until = None
                row.consecutive_failures = 0
                row.last_error_message = None
        if req.category is not None:
            row.category = _validate_category(req.category)
        db.flush()  # R3: atomic with the idempotency row via the single outer commit
        db.refresh(row)
        return (row.to_dict(), 200)

    outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)
    # P34-IDEMP: get_db() never commits and lookup_or_record only flushes — commit
    # so a retried patch replays the cached result instead of re-applying.
    db.commit()
    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.delete("/api/sources/{source_id}", status_code=200)
def api_sources_delete(
    source_id: int,
    request: Request,
    principal: str = Depends(require_operator),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """D-M3/P16 — idempotent source delete (cascades IngestEvent rows)."""
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    body_payload = {"endpoint": "sources_delete", "source_id": int(source_id)}
    req_hash = canonical_request_hash(body_payload)

    def _compute() -> Tuple[Dict[str, Any], int]:
        row = db.get(IngestSource, source_id)
        if row is None:
            return ({"deleted": True, "id": int(source_id), "already_absent": True}, 200)
        # Cascade-delete the event log so we don't leave dangling FKs.
        db.query(IngestEvent).filter(IngestEvent.source_id == source_id).delete()
        db.delete(row)
        db.flush()  # R3: atomic with the idempotency row via the single outer commit
        return ({"deleted": True, "id": int(source_id)}, 200)

    outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)
    # P34-IDEMP: get_db() never commits and lookup_or_record only flushes — commit
    # so a retried delete replays the cached result instead of re-running the cascade.
    db.commit()
    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


# D-H11 — per-source rate-limit window. 10s minimum interval between manual
# polls so a frustrated operator clicking "Poll Now" 5 times doesn't hammer
# the remote source.
_SOURCE_POLL_LAST_TS: Dict[int, float] = {}
_SOURCE_POLL_LOCK = threading.Lock()
_SOURCE_POLL_MIN_INTERVAL_SEC = 10.0


def _source_poll_rate_check(source_id: int) -> Optional[float]:
    """Return seconds remaining until next allowed poll, or None if allowed.

    Sets the timestamp atomically so concurrent callers can't both pass.
    """
    # P32-TZ32-6 — monotonic clock for rate-limit window; immune to wall-clock jumps.
    now = time.monotonic()
    with _SOURCE_POLL_LOCK:
        # P15/D-M21 — opportunistic GC: drop entries > 1 hour old before each
        # check so the dict can't grow unbounded over the process lifetime
        # (every distinct source_id adds an entry forever otherwise).
        cutoff = now - 3600.0
        stale = [sid for sid, ts in _SOURCE_POLL_LAST_TS.items() if ts < cutoff]
        for sid in stale:
            _SOURCE_POLL_LAST_TS.pop(sid, None)
        last = _SOURCE_POLL_LAST_TS.get(int(source_id), 0.0)
        elapsed = now - last
        if elapsed < _SOURCE_POLL_MIN_INTERVAL_SEC:
            return _SOURCE_POLL_MIN_INTERVAL_SEC - elapsed
        _SOURCE_POLL_LAST_TS[int(source_id)] = now
        return None


@app.post("/api/sources/{source_id}/poll", status_code=202)
async def api_sources_poll(source_id: int, request: Request, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """D-H11 — idempotent + rate-limited manual source poll.

    Cache key: source_id. A duplicate Idempotency-Key replays the cached
    response; a fresh key still respects the 10s rate-limit.

    Concurrency model (single-process):
    _SOURCE_POLL_LOCK and _SOURCE_POLL_LAST_TS are process-local. Within a
    single OS process they prevent a second poll within the 10s window. The
    idempotency row is persisted AFTER poll_source_now returns (not before);
    lookup_or_record handles concurrent INSERT races via SAVEPOINT so the
    persisted response is consistent, but the remote source may still be polled
    once per concurrent caller that passes the pre-check before either has
    written the row.

    Multi-worker deployments (uvicorn/gunicorn with --workers > 1):
    The in-memory mutex and rate-limit dict are NOT shared between OS worker
    processes. Two workers may both pass the initial idempotency pre-check
    (neither has yet written the row), both pass the per-process rate-limit
    dict, and both call poll_source_now for the same source within milliseconds.
    The SAVEPOINT in lookup_or_record makes the stored response consistent, but
    the remote source is polled twice. This is acceptable for manual operator
    polls (LOW severity, infrequent). To prevent double-poll in multi-worker
    deployments a DB-level advisory lock on source_id would be required before
    calling poll_source_now.
    """
    from backend.core.scheduler import poll_source_now
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    body_payload = {"endpoint": "sources_poll", "source_id": int(source_id)}
    req_hash = canonical_request_hash(body_payload)

    # Synchronous idempotency check first — replay path returns without invoking the poll.
    with session_scope() as s:
        existing = None
        try:
            from backend.core.database import IdempotencyKey as _IK
            existing = s.query(_IK).filter(_IK.key == key).one_or_none()
        except Exception:  # noqa: BLE001
            # P32-D7 / OBS32-1 — swallowing the lookup error silently meant a
            # broken IdempotencyKey table (schema drift, locked DB, etc.) would
            # disable replay protection invisibly. Log with traceback before
            # falling back to fresh-request behaviour.
            logger.exception(
                "sources_poll: idempotency lookup failed key=%s; treating as fresh request",
                key[:16],
            )
            existing = None
        if existing is not None:
            if existing.request_hash != req_hash:
                raise HTTPException(
                    status_code=409,
                    detail={"error": "IDEMPOTENCY_KEY_REUSED", "original_hash": existing.request_hash, "given_hash": req_hash},
                )
            try:
                cached_payload = json.loads(existing.response_json or "null")
            except (TypeError, ValueError):
                cached_payload = None
            cached_status = int(existing.status_code or 200)
            if cached_status >= 400:
                raise HTTPException(cached_status, cached_payload)
            return {**(cached_payload or {}), "idempotent_replay": True}

    # Not a replay — apply rate-limit and do the async poll.
    wait_s = _source_poll_rate_check(source_id)
    if wait_s is not None:
        raise HTTPException(
            status_code=429,
            detail={"error": "rate_limited", "retry_after_seconds": round(wait_s, 2)},
        )
    try:
        await poll_source_now(source_id)
        outcome_payload: Dict[str, Any] = {"source_id": source_id, "status": "polled"}
        outcome_status = 202  # R5/BE-API-004: match the endpoint's declared status_code=202
    except Exception as exc:  # noqa: BLE001
        logger.exception("source poll failed for source %s", source_id)
        outcome_payload = {"error": "poll_failed", "detail": type(exc).__name__}
        outcome_status = 500

    # session_scope commits whatever lookup_or_record records, so persisting a
    # 5xx would replay the same stale 500 on every retry. Skip recording on
    # server errors and raise live; the next retry re-runs the poll fresh.
    if outcome_status >= 500:
        raise HTTPException(outcome_status, outcome_payload)

    # Persist outcome via lookup_or_record (handles concurrent INSERT race).
    with session_scope() as s:
        def _persist() -> Tuple[Dict[str, Any], int]:
            return (outcome_payload, outcome_status)

        outcome = lookup_or_record(s, key=key, request_hash=req_hash, compute_fn=_persist)

    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.get("/api/sources/{source_id}/events")
def api_sources_events(
    source_id: int,
    limit: int = 50,
    principal: str = Depends(require_operator),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    limit = max(1, min(500, int(limit)))
    rows = (
        db.query(IngestEvent)
        .filter(IngestEvent.source_id == source_id)
        .order_by(IngestEvent.fetched_at.desc())
        .limit(limit)
        .all()
    )
    return {"events": [r.to_dict() for r in rows]}


@app.get("/api/sources/{source_id}/files")
def api_sources_files(
    source_id: int,
    limit: int = 200,
    principal: str = Depends(require_operator),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """P6-A2: list every KnowledgeNode that originated from this source.

    Joins ``ingest_events`` (status='ok' + resulting_node_id) → ``knowledge_nodes``
    and de-dups by node id (one node can be linked by multiple events when
    content_hash collides on a re-poll). Used by the /sources drawer to show
    "this Substack has ingested 838 articles" with the article list.
    """
    from sqlalchemy import desc as sql_desc, distinct, func

    src = db.get(IngestSource, source_id)
    if src is None:
        raise HTTPException(status_code=404, detail=f"Source {source_id} not found")
    limit = max(1, min(1000, int(limit)))

    # P12-B-M5 — compute the *true* total of distinct KnowledgeNodes attached
    # to this source so the drawer header can show "N files" without being
    # capped by the page limit. `returned` exposes how many of those nodes
    # were actually serialised in this response.
    total = (
        db.query(func.count(distinct(IngestEvent.resulting_node_id)))
        .filter(IngestEvent.source_id == source_id)
        .filter(IngestEvent.status == "ok")
        .filter(IngestEvent.resulting_node_id.isnot(None))
        .scalar()
        or 0
    )

    rows = (
        db.query(KnowledgeNode)
        .join(IngestEvent, IngestEvent.resulting_node_id == KnowledgeNode.id)
        .filter(IngestEvent.source_id == source_id)
        .filter(IngestEvent.status == "ok")
        .order_by(sql_desc(IngestEvent.fetched_at))
        .limit(limit)
        .all()
    )
    seen: set[int] = set()
    nodes_out: List[Dict[str, Any]] = []
    for n in rows:
        if n.id in seen:
            continue
        seen.add(n.id)
        nodes_out.append(n.to_dict())
    return {
        "source_id": source_id,
        "source_name": src.name,
        "nodes": nodes_out,
        "total": int(total),
        "returned": len(nodes_out),
        "limit": limit,
    }


@app.get("/api/scheduler/status")
def api_scheduler_status(principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core.scheduler import is_ingest_enabled, is_running, tick_seconds
    from backend.core.telegram_inbound import (
        _allowed_chat_ids,
        _read_last_update_id,
        is_inbound_enabled as tg_enabled,
        is_running as tg_running,
        _RATE_INTERVAL_SECONDS,
    )

    # P8-FIX/H-17: expose Telegram inbound state so the Sources page can show
    # whether the /intake bot is healthy without leaking chat IDs.
    try:
        tg_block: Dict[str, Any] = {
            "enabled": tg_enabled(),
            "running": tg_running(),
            "allowed_chats_count": len(_allowed_chat_ids()),
            "rate_interval_seconds": int(_RATE_INTERVAL_SECONDS),
            "last_update_id": int(_read_last_update_id()),
        }
    except Exception:  # noqa: BLE001
        tg_block = {"enabled": False, "running": False, "error": "introspection failed"}

    # P8-FIX/C-5: Discord inbound block — same shape, sourced from the bot
    # module. Defaults to disabled when the module is absent (e.g. in tests).
    try:
        from backend.core.discord_inbound import (
            is_inbound_enabled as disc_enabled,
            is_running as disc_running,
            allowed_channel_count,
            last_event_at,
        )
        disc_block: Dict[str, Any] = {
            "enabled": disc_enabled(),
            "running": disc_running(),
            "allowed_channels_count": int(allowed_channel_count()),
            "last_event_at": last_event_at(),
        }
    except Exception:  # noqa: BLE001
        disc_block = {"enabled": False, "running": False}

    return {
        "enabled": is_ingest_enabled(),
        "running": is_running(),
        "tick_seconds": tick_seconds(),
        "telegram_inbound": tg_block,
        "discord_inbound": disc_block,
    }


@app.get("/api/telegram/recent-intakes")
def api_telegram_recent_intakes(
    limit: int = 20,
    db: Session = Depends(get_db),
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """P8-FIX/H-17: recent KnowledgeNodes created via the Telegram /intake bot.

    Filters on ``source_type='telegram_intake'`` (stamped by
    :func:`backend.agents.intake.process_text_to_node` when called with
    ``entry_point='telegram'``). Read-only.
    """
    capped = max(1, min(100, int(limit or 20)))
    rows = (
        db.query(KnowledgeNode)
        .filter(KnowledgeNode.source_type == "telegram_intake")
        .order_by(KnowledgeNode.id.desc())
        .limit(capped)
        .all()
    )
    return {
        "items": [n.to_dict() for n in rows],
        "total": len(rows),
        "limit": capped,
    }


@app.get("/api/discord/recent-intakes")
def api_discord_recent_intakes(
    limit: int = 20,
    db: Session = Depends(get_db),
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """P8-FIX/C-5: recent KnowledgeNodes created via the Discord /intake bot."""
    capped = max(1, min(100, int(limit or 20)))
    rows = (
        db.query(KnowledgeNode)
        .filter(KnowledgeNode.source_type == "discord_intake")
        .order_by(KnowledgeNode.id.desc())
        .limit(capped)
        .all()
    )
    return {
        "items": [n.to_dict() for n in rows],
        "total": len(rows),
        "limit": capped,
    }


@app.get("/api/discord/status")
def api_discord_status(principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """P8-FIX/C-5: Discord bot status snapshot."""
    try:
        from backend.core.discord_inbound import (
            is_inbound_enabled,
            is_running,
            allowed_channel_count,
            last_event_at,
            recent_events,
        )
        return {
            "enabled": is_inbound_enabled(),
            "running": is_running(),
            "allowed_channels_count": int(allowed_channel_count()),
            "last_event_at": last_event_at(),
            "recent_events": recent_events(limit=20),
        }
    except Exception:  # noqa: BLE001
        return {
            "enabled": False,
            "running": False,
            "allowed_channels_count": 0,
            "last_event_at": None,
            "recent_events": [],
            "error": "discord module unavailable",
        }


@app.get("/api/cointegration/pairs")
def api_cointegration_pairs(
    lookback_days: int = 180,
    p_threshold: float = 0.05,
    refresh: bool = False,
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """P8-FIX/C-2: pairwise Engle-Granger cointegration scan.

    Cached on disk for 12h per ``(lookback_days, p_threshold)`` key. The
    multi-asset synthetic universe is generated lazily on first call when the
    ``storage/prices`` directory is empty.
    """
    from backend.core import cointegration as coint_mod
    # R6/BE-API-027: clamp inputs to sane bounds (defensive bound on the O(n^2)
    # statsmodels scan; operator auth is enforced via require_operator above).
    lookback_days = max(30, min(730, int(lookback_days)))
    p_threshold = max(0.001, min(0.5, float(p_threshold)))
    try:
        return coint_mod.compute_pairs_cached(
            lookback_days=lookback_days,
            p_threshold=p_threshold,
            force=bool(refresh),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("cointegration scan failed")
        raise HTTPException(status_code=500, detail=f"cointegration scan failed: {type(exc).__name__}") from exc


# --------------------------------------------------------------------------- #
# P-REALDATA — real market-data ingestion (Binance public)                    #
# --------------------------------------------------------------------------- #

class MarketDataIngestRequest(BaseModel):
    """Body for POST /api/market-data/ingest. All fields optional — omitting
    them ingests the configured 30-symbol universe from MARKET_DATA_START
    (default 2024-01-01) to yesterday UTC."""
    symbols: Optional[List[str]] = None
    start: Optional[str] = None    # YYYY-MM-DD
    end: Optional[str] = None      # YYYY-MM-DD
    with_oi: bool = True
    with_funding: bool = True
    provider: Optional[str] = None         # "binance_public" (default) | "yahoo"
    incremental: bool = False              # cheap refresh of existing CSVs
    lookback_days: Optional[int] = None    # with incremental: last N days


# In-process run-state guard so two operators can't launch overlapping ingests
# (they would race on the same CSV files). The detailed coverage report lives
# in the on-disk sidecar read by GET /api/market-data/status.
_market_data_ingest_state: Dict[str, Any] = {
    "running": False, "started_at": None, "error": None,
}
_market_data_ingest_lock = threading.Lock()


def _run_market_data_ingest(params: Dict[str, Any]) -> None:
    from backend.core import market_data as _md
    try:
        if params.get("incremental"):
            _md.update_universe(
                params.get("symbols"),
                lookback_days=params.get("lookback_days"),
                provider=params.get("provider"),
            )
        else:
            _md.ingest_universe(
                params.get("symbols"),
                start=params.get("start"),
                end=params.get("end"),
                with_oi=bool(params.get("with_oi", True)),
                with_funding=bool(params.get("with_funding", True)),
                provider=params.get("provider"),
            )
        # P-SIM — fresh bars just landed; advance every active forward-sim
        # account. Best-effort: a sim failure must never fail the ingest.
        try:
            from backend.core import sim_account as _sim
            stats = _sim.tick_all_active()
            if stats.get("ran") or stats.get("errors"):
                logger.info("post-ingest sim tick: %s", stats)
        except Exception:  # noqa: BLE001
            logger.exception("post-ingest sim-account tick failed (non-fatal)")
    except Exception as exc:  # noqa: BLE001
        logger.exception("market-data ingest thread failed")
        with _market_data_ingest_lock:
            _market_data_ingest_state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        with _market_data_ingest_lock:
            _market_data_ingest_state["running"] = False


@app.post("/api/market-data/ingest", status_code=202)
def api_market_data_ingest(
    req: MarketDataIngestRequest,
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """Fetch REAL OHLCV (+ funding/open-interest for BTC) from Binance public
    data (``data.binance.vision`` — free, no API key, no geo-block) and
    overwrite the on-disk price CSVs in the exact schema each consumer expects.

    The full 30-symbol / multi-year ingest is a multi-minute job, so it runs in
    a background daemon thread; poll ``GET /api/market-data/status`` for
    progress and the per-symbol coverage report.
    """
    with _market_data_ingest_lock:
        if _market_data_ingest_state["running"]:
            raise HTTPException(409, "a market-data ingest is already running")
        _market_data_ingest_state.update({
            "running": True,
            "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "error": None,
        })
    params = {
        "symbols": (
            [s.strip().upper() for s in req.symbols if s and s.strip()]
            if req.symbols else None
        ),
        "start": req.start,
        "end": req.end,
        "with_oi": req.with_oi,
        "with_funding": req.with_funding,
        "provider": (req.provider or None),
        "incremental": bool(req.incremental),
        "lookback_days": req.lookback_days,
    }
    threading.Thread(
        target=_run_market_data_ingest, args=(params,),
        name="market-data-ingest", daemon=True,
    ).start()
    logger.info("market-data ingest started by %s (%s)", principal, params)
    return {
        "status": "started",
        "detail": "real market-data ingest running in background",
        "status_url": "/api/market-data/status",
    }


@app.get("/api/market-data/status")
def api_market_data_status(
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """Current ingest run-state plus the last coverage report sidecar
    (``storage/market_data_meta.json``)."""
    from backend.core import market_data as _md
    with _market_data_ingest_lock:
        state = dict(_market_data_ingest_state)
    return {
        "running": bool(state.get("running")),
        "started_at": state.get("started_at"),
        "error": state.get("error"),
        "last_report": _md.read_meta(),
    }


@app.get("/api/alpha-lab/suggested-topics")
def api_alpha_lab_suggested_topics(
    limit: int = 6,
    principal: str = Depends(require_operator),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """P8-FIX/H-15: rail of suggested alpha-research topics for /alpha-lab.

    Prefers concept-kind KnowledgeNodes with the highest IC; falls back to a
    seed file (``backend/data/seed/suggested_topics.json``) so a fresh repo
    still has something to show.
    """
    cap = max(1, min(24, int(limit or 6)))
    seen_titles: set[str] = set()
    out: List[Dict[str, Any]] = []

    # 1) Try concept nodes from the KB ranked by ic_score descending.
    try:
        node_rows = (
            db.query(KnowledgeNode)
            .filter(KnowledgeNode.kind == KIND_CONCEPT)
            .order_by(KnowledgeNode.ic_score.desc(), KnowledgeNode.id.desc())
            .limit(cap * 2)
            .all()
        )
        # 1a) Compute chip status — "in-progress" if a strategy references this
        # node and is still in a pre-approved status, "proven" if it's APPROVED
        # / PAPER_TRADE / SMALL_CAPITAL / LIVE.
        # Pre-fetch strategies once to keep query count flat.
        strategy_rows = db.query(AlphaStrategy).all()
        active_statuses = {"BACKTESTING", "CRITIC_LOOP", "STORY_GEN", "CODE_GEN", "INTAKE"}
        proven_statuses = {"APPROVED", "PAPER_TRADE", "SMALL_CAPITAL", "LIVE"}
        node_to_chip: Dict[int, str] = {}
        for st in strategy_rows:
            try:
                srcs = st.config().get("source_node_ids") or []
            except Exception:  # noqa: BLE001
                srcs = []
            status_norm = (st.status or "").upper()
            for s in srcs:
                try:
                    nid = int(s)
                except (TypeError, ValueError):
                    continue
                # "proven" beats "in-progress" beats "queued"; never downgrade.
                if status_norm in proven_statuses:
                    node_to_chip[nid] = "proven"
                elif status_norm in active_statuses and node_to_chip.get(nid) != "proven":
                    node_to_chip[nid] = "in-progress"

        for n in node_rows:
            if len(out) >= cap:
                break
            title = (n.title or "").strip()
            if not title or title.lower() in seen_titles:
                continue
            seen_titles.add(title.lower())
            chip = node_to_chip.get(int(n.id), "queued")
            out.append({
                "id": f"kb:{n.id}",
                "title": title,
                "chip": chip,
                "source": "knowledge_base",
                "kb_node_id": int(n.id),
                "ic_score": float(n.ic_score or 0.0),
            })
    except Exception:  # noqa: BLE001
        logger.exception("Failed to read concept nodes for alpha-lab topics")

    # 2) Pad with seed entries when KB has fewer than ``cap`` usable concepts.
    if len(out) < cap:
        try:
            seed_path = PROJECT_ROOT / "backend" / "data" / "seed" / "suggested_topics.json"
            if seed_path.exists():
                seed_data = json.loads(seed_path.read_text(encoding="utf-8"))
                for entry in seed_data.get("topics", []):
                    if len(out) >= cap:
                        break
                    title = str(entry.get("title") or "").strip()
                    if not title or title.lower() in seen_titles:
                        continue
                    seen_titles.add(title.lower())
                    out.append({
                        "id": str(entry.get("id") or f"seed:{len(out)}"),
                        "title": title,
                        "chip": str(entry.get("chip") or "queued"),
                        "source": "seed_fallback",
                        "kb_node_id": None,
                        "ic_score": 0.0,
                    })
        except Exception:  # noqa: BLE001
            logger.exception("Failed to read suggested_topics.json seed file")

    return {
        "topics": out,
        "generated_at": datetime_now_iso(),
        "limit": cap,
    }


def _utc_now_iso_local() -> str:
    from datetime import datetime, timezone as _tz
    return datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# P3 — Knowledge Spaces (19-category taxonomy + per-node detail)
# ---------------------------------------------------------------------------

# Static taxonomy used by /kb-explorer's left-tree. Backed by the same labels
# the reference UI uses in its sidebar.
KNOWLEDGE_CATEGORIES: List[str] = [
    "alpha-ideas",
    "factor-data",
    "analysis-methods",
    "portfolio-management",
    "mental-models",
    "quant-philosophy",
    "market-structure",
    "infrastructure",
    "historical-figures",
    "disposition",
    "contextual",
    "datasources",
    "alpha-portfolio",
    "cross-asset-signals",
    "unclassified",
    "house-datasources",
    "papers",
    "webpage-builds",
    "postmortem",
]


# NB: paths intentionally NOT under /api/knowledge/{...} to avoid being
# intercepted by the existing /api/knowledge/{node_id} integer-typed route.
@app.get("/api/kb-spaces/categories")
def api_kb_categories(db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """Return all 19 categories with live node counts."""
    from sqlalchemy import func
    agg_rows = (
        db.query(KnowledgeNode.category, func.count(KnowledgeNode.id))
        .group_by(KnowledgeNode.category)
        .all()
    )
    counts: Dict[str, int] = {c: 0 for c in KNOWLEDGE_CATEGORIES}
    total = 0
    for raw_cat, cnt in agg_rows:
        total += int(cnt)
        cat = (raw_cat or "unclassified").strip().lower()
        if cat not in counts:
            cat = "unclassified"
        counts[cat] += int(cnt)
    return {
        "categories": [
            {"key": c, "label": c, "count": counts[c]} for c in KNOWLEDGE_CATEGORIES
        ],
        "total": total,
    }


@app.get("/api/kb-spaces/by-category/{category}")
def api_kb_by_category(
    category: str,
    limit: int = 100,
    db: Session = Depends(get_db),
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    limit = max(1, min(500, int(limit)))
    cat_norm = (category or "").strip().lower()
    q = db.query(KnowledgeNode)
    if cat_norm and cat_norm != "all":
        q = q.filter(KnowledgeNode.category == cat_norm)
    nodes = q.order_by(KnowledgeNode.id.desc()).limit(limit).all()
    return {"nodes": [n.to_dict() for n in nodes], "category": cat_norm}


# ---------------------------------------------------------------------------
# P3 — Factor network (derived from knowledge graph + strategies)
# ---------------------------------------------------------------------------


@app.get("/api/factor-network")
def api_factor_network(principal: str = Depends(require_operator), db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Force-directed Factor Network payload (enriched in P5-BE-07).

    Adds PageRank, community ids (greedy modularity), out-degree, source URL,
    and ``data_points`` (count of ``IngestEvent`` rows that landed on each
    node) per node — drives the Factor Network's right-rail Node Inspector
    and the edge-distribution histogram.

    Granger p-values and true correlation edges remain deferred (see
    ``granger_p`` field, always ``None`` until per-asset price series are
    wired). Tag overlap is still the edge proxy.
    """
    import networkx as nx
    from datetime import datetime, timedelta, timezone
    from networkx.algorithms.community import greedy_modularity_communities
    from sqlalchemy import func

    nodes_payload: List[Dict[str, Any]] = []
    edges_payload: List[Dict[str, Any]] = []
    factor_nodes: List[KnowledgeNode] = (
        db.query(KnowledgeNode)
        .filter(
            (KnowledgeNode.category == "factor-data")
            | (KnowledgeNode.tags.like("%factor%"))
        )
        .all()
    )
    if not factor_nodes:
        # Breadth-first fallback: no factor-tagged nodes exist yet. Load the
        # highest-scored nodes up to _LIST_HARD_LIMIT to bound memory and
        # graph-computation cost.
        factor_nodes = (
            db.query(KnowledgeNode)
            .order_by(KnowledgeNode.ic_score.desc().nullslast(), KnowledgeNode.id.desc())
            .limit(_LIST_HARD_LIMIT)
            .all()
        )

    factor_node_ids = [n.id for n in factor_nodes]

    # data_points per knowledge node = number of IngestEvent rows pointing at it.
    data_points_map: Dict[int, int] = {}
    if factor_node_ids:
        rows = (
            db.query(IngestEvent.resulting_node_id, func.count(IngestEvent.id))
            .filter(IngestEvent.resulting_node_id.in_(factor_node_ids))
            .group_by(IngestEvent.resulting_node_id)
            .all()
        )
        data_points_map = {int(r[0]): int(r[1]) for r in rows if r[0] is not None}

    tag_index: Dict[str, List[int]] = {}
    for n in factor_nodes:
        for t in n.tag_list():
            tag_index.setdefault(t.lower(), []).append(n.id)

    # Tag-overlap edges (de-duplicated per pair).
    seen_pairs: set[tuple[int, int]] = set()
    raw_edges: List[Dict[str, Any]] = []
    for tag, ids in tag_index.items():
        if len(ids) < 2 or len(ids) > 25:
            continue
        for i, src_id in enumerate(ids):
            for dst_id in ids[i + 1:]:
                key = (min(src_id, dst_id), max(src_id, dst_id))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                raw_edges.append({
                    "from": f"k{src_id}",
                    "to": f"k{dst_id}",
                    "label": tag,
                })

    # Strategy → factor association edges.
    strategies = db.query(AlphaStrategy).all()
    for st in strategies:
        sid = f"s{st.id}"
        for src in (st.config().get("source_node_ids") or []):
            try:
                raw_edges.append({
                    "from": f"k{int(src)}",
                    "to": sid,
                    "label": "uses",
                })
            except (TypeError, ValueError):
                continue

    edges_payload = list(raw_edges)

    # Build a networkx undirected graph for analytics. Use the de-duplicated
    # edge set; loop edges and self-loops are impossible here by construction.
    G = nx.Graph()
    for n in factor_nodes:
        G.add_node(f"k{n.id}")
    for st in strategies:
        G.add_node(f"s{st.id}")
    for e in edges_payload:
        G.add_edge(e["from"], e["to"])

    # PageRank — handles isolates by giving them the (1-alpha)/N base rank.
    try:
        pagerank = nx.pagerank(G, alpha=0.85) if G.number_of_nodes() else {}
    except Exception:  # noqa: BLE001
        logger.exception("PageRank failed; defaulting to 0.0")
        pagerank = {}

    # Communities — only meaningful with edges. Fall back to "single community"
    # when modularity is too low to be informative or when the algorithm errors.
    community_map: Dict[str, int] = {}
    try:
        if G.number_of_edges() >= 2:
            communities = list(greedy_modularity_communities(G))
            for idx, group in enumerate(communities):
                for nid in group:
                    community_map[str(nid)] = idx
    except Exception:  # noqa: BLE001
        logger.exception("greedy_modularity_communities failed; using single community")
        community_map = {}

    # Out-degree per node id (undirected — equals degree).
    degrees = dict(G.degree()) if G.number_of_nodes() else {}

    for n in factor_nodes:
        nid = f"k{n.id}"
        kind = n.kind_value()
        # P6-M11 — risk_score heuristic: low IC + sparse data + risky tags → high risk.
        # All inputs are clamped before combining; final value clipped to [0, 1].
        ic_norm = max(0.0, min(1.0, float(n.ic_score or 0.0) / 2.0))
        tag_set = {t.lower() for t in n.tag_list()}
        risky_tag_hits = len({"risky", "speculative", "experimental"} & tag_set)
        data_pts = int(data_points_map.get(int(n.id), 0))
        sparse_penalty = 0.3 if data_pts < 5 else 0.0
        risk = max(0.0, min(1.0, (1.0 - ic_norm) * 0.7 + risky_tag_hits * 0.1 + sparse_penalty))
        nodes_payload.append({
            "id": nid,
            "label": f"K#{n.id} {n.title[:36]}",
            "title": n.title,
            "kind": kind,
            "category": n.category or "unclassified",
            "ic_score": float(n.ic_score or 0.0),
            "color": KIND_COLORS.get(kind, KIND_COLORS[KIND_CONCEPT]),
            "size": 8 + min(20, len(n.tag_list()) * 2),
            # P5 enrichments:
            "pagerank": round(float(pagerank.get(nid, 0.0)), 6),
            "out_degree": int(degrees.get(nid, 0)),
            "community_id": community_map.get(nid),
            "source_url": n.source_url,
            "data_points": data_pts,
            "granger_p": None,  # populated post-hoc from granger_edges (see below)
            "risk_score": round(risk, 4),
        })

    for st in strategies:
        sid = f"s{st.id}"
        m = st.metrics() or {}
        sharpe = float(m.get("annualized_sharpe") or 0.0)
        max_dd = float(m.get("max_drawdown") or 0.0)
        # P6-M11: strategy risk_score blends sharpe shortfall with drawdown magnitude.
        risk_s = max(0.0, min(1.0, (1.0 - min(1.0, sharpe / 3.0)) * 0.6 + min(1.0, abs(max_dd)) * 0.4))
        nodes_payload.append({
            "id": sid,
            "label": f"S#{st.id} {st.name[:36]}",
            "title": st.name,
            "kind": "strategy",
            "category": (st.config() or {}).get("alpha_category"),
            "ic_score": sharpe,
            "color": _strategy_color(st.status),
            "size": 12,
            "pagerank": round(float(pagerank.get(sid, 0.0)), 6),
            "out_degree": int(degrees.get(sid, 0)),
            "community_id": community_map.get(sid),
            "source_url": None,
            "data_points": 0,
            "granger_p": None,
            "risk_score": round(risk_s, 4),
        })

    # Edge distribution = bucket count of edges per category of the "from"
    # endpoint. Drives the right-rail edge histogram.
    cat_for_id: Dict[str, str] = {}
    for np in nodes_payload:
        cat_for_id[np["id"]] = str(np.get("category") or "unclassified")
    edge_dist: Dict[str, int] = {}
    for e in edges_payload:
        cat = cat_for_id.get(e["from"], "unclassified")
        edge_dist[cat] = edge_dist.get(cat, 0) + 1

    n_communities = len(set(community_map.values())) if community_map else (
        1 if G.number_of_nodes() else 0
    )

    # P6-B5: enrich edges with Granger causality p-values. Only k-prefixed
    # nodes get Granger (strategies have no IC history series); pair lookup
    # is direction-aware so edge `from`→`to` matches its src→dst Granger run.
    try:
        from backend.core.granger import edges_for_factor_network
        granger_rows = edges_for_factor_network(db, p_max=1.0)
        # Build a lookup keyed on the (src_id, dst_id) pair we stored. For each
        # frontend edge between two KnowledgeNodes, attach the p_value of the
        # matching directed Granger row when present.
        granger_map: Dict[tuple, Dict[str, Any]] = {}
        for r in granger_rows:
            key = (int(r["src_node_id"]), int(r["dst_node_id"]))
            # Keep the smallest p-value if multiple lags exist for the same pair.
            prev = granger_map.get(key)
            if prev is None or float(r["p_value"]) < float(prev["p_value"]):
                granger_map[key] = r
        granger_attached = 0
        # P11-B-09: track which (src,dst) pairs already have a similarity edge so
        # we can emit Granger-only edges for KB-pair causalities the similarity
        # graph never produced an edge for.
        existing_pairs: set = set()
        kb_node_ids: set = set()
        for n in factor_nodes:
            kb_node_ids.add(int(n.id))
        for e in edges_payload:
            src_label = str(e.get("from") or "")
            dst_label = str(e.get("to") or "")
            if not (src_label.startswith("k") and dst_label.startswith("k")):
                continue
            try:
                src_id = int(src_label[1:])
                dst_id = int(dst_label[1:])
            except (TypeError, ValueError):
                continue
            existing_pairs.add((src_id, dst_id))
            existing_pairs.add((dst_id, src_id))
            row = granger_map.get((src_id, dst_id)) or granger_map.get((dst_id, src_id))
            if row is None:
                continue
            e["granger_p"] = float(row["p_value"])
            e["lag"] = int(row["lag"])
            granger_attached += 1
        # P11-B-09: emit Granger-only edges for KB pairs absent from existing edges.
        granger_only_added = 0
        for (src_id, dst_id), r in granger_map.items():
            if (src_id, dst_id) in existing_pairs:
                continue
            if src_id not in kb_node_ids or dst_id not in kb_node_ids:
                continue
            try:
                _p = float(r["p_value"])
                _lag = int(r["lag"])
            except (TypeError, ValueError, KeyError):
                continue
            edges_payload.append({
                "from": f"k{src_id}",
                "to": f"k{dst_id}",
                "label": f"granger p={_p:.3g}",
                "kind": "granger_only",
                "granger_p": _p,
                "lag": _lag,
            })
            existing_pairs.add((src_id, dst_id))
            granger_only_added += 1
        # P12-C-M6 — fan p_value back onto nodes (smallest p across incident edges).
        node_idx: Dict[str, Dict[str, Any]] = {n["id"]: n for n in nodes_payload}
        for e in edges_payload:
            p = e.get("granger_p")
            if p is None:
                continue
            for endpoint in (e.get("from"), e.get("to")):
                target = node_idx.get(str(endpoint))
                if target is None:
                    continue
                cur = target.get("granger_p")
                if cur is None or float(p) < float(cur):
                    target["granger_p"] = float(p)
        n_granger_p = granger_attached + granger_only_added
    except Exception:  # noqa: BLE001
        logger.exception("Granger enrichment failed; serving without granger_p")
        n_granger_p = 0

    # A-M3 — extra KPIs for the right-rail tile grid (Factor×Factor edges +
    # graph isolates). factor_id_set holds every "k{n.id}" id; an edge is a
    # factor-factor edge iff both endpoints are in that set.
    factor_id_set = {f"k{n.id}" for n in factor_nodes}
    n_factor_factor_edges = sum(
        1 for e in edges_payload
        if e["from"] in factor_id_set and e["to"] in factor_id_set
    )
    n_isolates = sum(1 for nid, deg in degrees.items() if deg == 0)

    # A-M3 (P10) — header fields: "new Granger edges (24h)" + "computed_at".
    # Both derive from existing GrangerEdge.computed_at column — no schema
    # change needed. Falls back to (0, now) when granger_edges is empty.
    now_utc = datetime.now(timezone.utc)
    try:
        from backend.core.database import GrangerEdge as _GE
        since_24h = now_utc - timedelta(hours=24)
        n_granger_new_24h = int(
            db.query(func.count(_GE.id))
            .filter(_GE.computed_at >= since_24h)
            .scalar()
            or 0
        )
        last_computed = db.query(func.max(_GE.computed_at)).scalar()
        computed_at_iso = (
            last_computed.isoformat() if last_computed is not None else now_utc.isoformat()
        )
    except Exception:  # noqa: BLE001
        logger.exception("granger header fields fallback")
        n_granger_new_24h = 0
        computed_at_iso = now_utc.isoformat()

    return {
        "nodes": nodes_payload,
        "edges": edges_payload,
        "n_factors": len(factor_nodes),
        "n_edges": len(edges_payload),
        "n_communities": n_communities,
        "n_granger_p": n_granger_p,
        "n_granger_new_24h": n_granger_new_24h,
        "computed_at": computed_at_iso,
        "n_factor_factor_edges": n_factor_factor_edges,
        "n_isolates": n_isolates,
        "edge_distribution": [
            {"category": k, "count": v}
            for k, v in sorted(edge_dist.items(), key=lambda x: -x[1])
        ],
        "kind_palette": dict(KIND_COLORS),
        "strategy_status_palette": dict(STRATEGY_STATUS_COLORS),
    }


# D-H12 — single in-flight Granger recompute. Recompute is CPU-bound and writes
# large factor-network results; running two concurrently can corrupt
# intermediate state and burns CPU. Used in addition to idempotency cache.
_GRANGER_RECOMPUTE_LOCK = threading.Lock()


@app.post("/api/factor-network/granger/recompute")
def api_factor_network_granger_recompute(request: Request, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """D-H12 — idempotent Granger recompute. Cache key: constant "granger-recompute".

    Honors ``GRANGER_ENABLED``; returns ``{enabled: 0, ...}`` when disabled.
    Run automatically by ``periodic_tasks.granger_recompute`` on a weekly
    cadence; this endpoint is for manual debugging or fresh-data flushes.
    Wrapped with a module-level lock so a second concurrent caller gets a 409
    instead of a duplicate compute.
    """
    from backend.core.granger import recompute_top_pairs
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    body_payload = {"endpoint": "granger_recompute"}
    req_hash = canonical_request_hash(body_payload)

    with session_scope() as db:
        def _compute() -> Tuple[Dict[str, Any], int]:
            acquired = _GRANGER_RECOMPUTE_LOCK.acquire(blocking=False)
            if not acquired:
                raise HTTPException(
                    status_code=409,
                    detail={"error": "busy", "detail": "Granger recompute already in progress"},
                )
            try:
                stats = recompute_top_pairs(db)
            finally:
                _GRANGER_RECOMPUTE_LOCK.release()
            return (stats, 200)

        outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)

    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


# ---------------------------------------------------------------------------
# P6-B1 — Auto-pipeline status
# ---------------------------------------------------------------------------


@app.get("/api/auto-pipeline/status")
def api_auto_pipeline_status(principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """Read-only snapshot of the source→pipeline hook: env config + recent
    dispatch history (last 50). Used by Mission Control for an "auto" panel."""
    from backend.core.auto_pipeline import status_snapshot
    return status_snapshot()


# ---------------------------------------------------------------------------
# P6-B2 — IC-priority alpha queue
# ---------------------------------------------------------------------------


@app.get("/api/alpha-queue")
def api_alpha_queue(
    limit: int = 20,
    ic_floor: float = 0.0,
    db: Session = Depends(get_db),
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """Ranked KnowledgeNode backlog awaiting alpha-pipeline promotion.

    Read-only — does NOT dispatch any pipeline runs. The companion
    ``POST /api/alpha-queue/promote/{node_id}`` does the actual dispatch.
    """
    from backend.core.alpha_queue import is_enabled, next_candidates
    limit = max(1, min(200, int(limit)))
    floor = max(0.0, float(ic_floor))
    items = next_candidates(db, limit=limit, ic_floor=floor)
    return {
        "enabled": is_enabled(),
        "limit": limit,
        "ic_floor": floor,
        "items": [it.to_dict() for it in items],
        "generated_at": datetime_now_iso(),
    }


@app.post("/api/alpha-queue/promote/{node_id}")
def api_alpha_queue_promote(node_id: int, request: Request, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """D-H7 — idempotent alpha-queue promote.

    Synchronously promote one KnowledgeNode to a full pipeline run. NOT gated
    by ``ALPHA_QUEUE_ENABLED`` — that flag controls the *automatic* scheduled
    tick. Manual promote always works (subject to the LLM budget cap), so
    operators can shepherd the queue by hand without enabling auto-promotion.
    Cache key: node_id — a retry for the same node replays the cached result
    instead of spawning a duplicate pipeline.
    """
    from backend.core.alpha_queue import promote_node
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    body_payload = {"endpoint": "alpha_queue_promote", "node_id": int(node_id)}
    req_hash = canonical_request_hash(body_payload)

    with session_scope() as s:
        def _compute() -> Tuple[Dict[str, Any], int]:
            result = promote_node(int(node_id))
            if "error" in result and "not found" in str(result.get("error", "")):
                return ({"error": "not_found", "detail": result["error"]}, 404)
            if "error" in result:
                # LLM budget cap or pipeline failure — raise so session_scope
                # rolls back and no idempotency row is committed. The operator
                # can retry with the same Idempotency-Key after the budget
                # resets. (Contrast with the 404 branch above which is
                # deterministic and intentionally cached.)
                raise HTTPException(status_code=503, detail=result)
            return (result, 200)

        outcome = lookup_or_record(s, key=key, request_hash=req_hash, compute_fn=_compute)

    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


def datetime_now_iso() -> str:
    """Centralised ISO timestamp helper — keeps response shape consistent."""
    from datetime import datetime as _dt, timezone as _tz
    return _dt.now(_tz.utc).isoformat()


# ---------------------------------------------------------------------------
# P4 — Alpha Lab chat sessions + messages (with optional image-attachment)
# ---------------------------------------------------------------------------


CHAT_ASSETS_DIR = PROJECT_ROOT / "storage" / "chat_images"
CHAT_ASSETS_DIR.mkdir(parents=True, exist_ok=True)

# Magic-byte sniff — cheaper than pulling PIL just for sniffing. Each entry is
# (mime, [list of (offset, signature_bytes)] all must match).
_IMAGE_MAGIC = {
    "image/png":  [(0, b"\x89PNG\r\n\x1a\n")],
    "image/jpeg": [(0, b"\xff\xd8\xff")],
    "image/gif":  [(0, b"GIF87a"), (0, b"GIF89a")],  # either variant
    "image/webp": [(0, b"RIFF"), (8, b"WEBP")],      # both required
}
_EXT_FOR_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}
_IMAGE_MAX_BYTES = 8 * 1024 * 1024  # 8 MB — matches _client.py:_read_image_b64 cap
_IMAGE_FILENAME_RX = re.compile(r"^[a-f0-9]{32}\.(png|jpg|jpeg|gif|webp)$")


def _sniff_image_mime(data: bytes) -> Optional[str]:
    """Return the canonical mime if `data` matches a known image magic header.

    For GIF, either GIF87a or GIF89a passes. For WEBP, BOTH the RIFF prefix
    (offset 0) AND the WEBP marker (offset 8) must match.
    """
    if not data:
        return None
    for mime, sigs in _IMAGE_MAGIC.items():
        if mime == "image/gif":
            if any(data.startswith(sig) for off, sig in sigs):
                return mime
            continue
        ok = True
        for off, sig in sigs:
            if data[off:off + len(sig)] != sig:
                ok = False
                break
        if ok:
            return mime
    return None


@app.post("/api/chat/upload", status_code=201)
async def api_chat_upload(request: Request, file: UploadFile = File(...), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """Accept a single image (≤8MB, PNG/JPG/GIF/WEBP), store under
    `storage/chat_images/<uuid32>.<ext>`, return its relative path.

    The returned `path` is what the frontend should pass back as part of the
    `image_paths` field in `POST /api/chat/send`. The path is stable: caller
    can also fetch it directly via `GET /api/chat/images/<filename>`.

    Validates the content-type by magic-byte sniffing (not extension trust);
    rejects > 8 MB to mirror the LLM provider cap at `_client.py:_read_image_b64`.
    """
    import uuid

    # Short-circuit on the declared body size BEFORE touching the stream, so a
    # multi-GB upload is rejected without buffering anything. Content-Length is
    # attacker-controlled (can be omitted/lied), so the chunked read below is
    # the real enforcement; this is just a cheap fast-path.
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > _IMAGE_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large ({int(declared)} bytes); max {_IMAGE_MAX_BYTES} bytes (8MB)",
                )
        except ValueError:
            pass  # malformed header — fall through to the authoritative chunked read

    # Stream in bounded chunks and abort the instant the running total exceeds
    # the cap. UploadFile.read(size) reads at most `size` bytes (verified against
    # starlette 0.50.0), so peak allocation is <= _IMAGE_MAX_BYTES + one chunk —
    # never the full (possibly multi-GB) body.
    _CHUNK = 64 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > _IMAGE_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large (> {_IMAGE_MAX_BYTES} bytes); max {_IMAGE_MAX_BYTES} bytes (8MB)",
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    mime = _sniff_image_mime(data[:16])
    if mime is None:
        raise HTTPException(
            status_code=415,
            detail="Unsupported image type. Allowed: PNG, JPG, GIF, WEBP.",
        )
    ext = _EXT_FOR_MIME[mime]
    name = f"{uuid.uuid4().hex}.{ext}"
    dest = CHAT_ASSETS_DIR / name
    dest.write_bytes(data)
    rel_path = f"storage/chat_images/{name}"
    logger.info("chat_upload: saved %d bytes as %s (%s)", len(data), rel_path, mime)
    return {
        "path": rel_path,
        "filename": name,
        "size": len(data),
        "mime": mime,
    }


@app.get("/api/chat/images/{filename}")
def api_chat_image_get(filename: str, principal: str = Depends(require_operator)) -> FileResponse:
    """Serve a previously-uploaded chat image. Filename is regex-validated
    against the `<uuid32>.<ext>` shape we wrote at upload time, blocking
    `..` traversal and arbitrary filesystem reads.
    """
    if not _IMAGE_FILENAME_RX.match(filename or ""):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = CHAT_ASSETS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    # Resolve and double-check we never escaped the directory (paranoia).
    if CHAT_ASSETS_DIR.resolve() not in path.resolve().parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    ext = path.suffix.lower().lstrip(".")
    mime = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "webp": "image/webp",
    }.get(ext, "application/octet-stream")
    return FileResponse(str(path), media_type=mime)


# ---------------------------------------------------------------------------
# P6-B7 — Ingested image cache serve endpoint
# ---------------------------------------------------------------------------

_ASSET_HASH_RX = re.compile(r"^[0-9a-f]{64}$")


@app.get("/api/assets/{hash_hex}")
def api_asset_get(hash_hex: str, db: Session = Depends(get_db)) -> FileResponse:
    """Serve a cached inline image by its content sha256 hash.

    Writes happen in ``backend/core/asset_cache.py`` when a feed item with
    ``<img>`` tags is ingested; reads happen here. The hash format is enforced
    with regex (64 hex chars) and the resolved local_path is realpath-checked
    against ``ASSETS_DIR`` to defend against a poisoned DB row.
    """
    if not _ASSET_HASH_RX.match(hash_hex or ""):
        raise HTTPException(status_code=400, detail="Invalid hash")
    from backend.core.asset_cache import resolve_serve_path
    found = resolve_serve_path(hash_hex, db)
    if found is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    path, mime = found
    return FileResponse(str(path), media_type=mime)


class ChatSendRequest(BaseModel):
    session_id: Optional[int] = None
    user_text: str = Field(..., min_length=1, max_length=8000)
    image_paths: Optional[List[str]] = Field(default=None, description="Absolute paths to local images already uploaded")
    # P-MODEL-SEL — optional per-message model override (Alpha Lab model picker).
    # Must be one of the ids returned by GET /api/chat/models; omitted => server
    # default. Validated server-side (never trusted) in api_chat_send.
    model: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Optional model id for THIS message; must be in the /api/chat/models allowlist. Omit to use the server default.",
    )


ALPHA_LAB_SYSTEM_PROMPT = (
    "You are the Alpha Lab co-researcher inside an Agentic Alpha Research "
    "System. The user is brainstorming new alpha ideas, debating factor "
    "design, or asking you to read screenshots of charts. Always reason "
    "concretely about (a) the proposed mechanism, (b) data availability, "
    "(c) likely failure modes, and (d) one concrete next experiment the user "
    "could run. If the user attaches an image of a chart, describe what you "
    "see explicitly and only THEN connect it to alpha hypotheses. End every "
    "reply with one short follow-up question. Use plain markdown."
)


def _alpha_lab_model_allowlist() -> List[str]:
    """Selectable model ids for the Alpha Lab model picker.

    The configured/default model (from describe_provider_config) is ALWAYS the
    first entry. Operators offer additional choices by setting
    ``ALPHA_LAB_MODELS`` to a comma-separated list of provider model ids, e.g.
    ``anthropic/claude-sonnet-4.6,anthropic/claude-opus-4.1``. Blanks and
    duplicates are dropped; insertion order is preserved (default first). When
    the env var is unset the list is just the single default model, and the
    frontend picker degrades to a read-only single choice — no behaviour change.
    """
    try:
        default_model = str((describe_provider_config() or {}).get("model") or "").strip()
    except Exception:  # noqa: BLE001
        default_model = ""
    out: List[str] = []
    seen: set[str] = set()
    if default_model:
        out.append(default_model)
        seen.add(default_model)
    for tok in (os.environ.get("ALPHA_LAB_MODELS", "") or "").split(","):
        m = tok.strip()
        if m and m not in seen:
            out.append(m)
            seen.add(m)
    return out


def _resolve_chat_model(requested: Optional[str]) -> Optional[str]:
    """Validate a requested chat model against the allowlist.

    Returns the model id to hand to call_messages. ``None`` means "use the
    server default" (caller requested nothing). Raises HTTP 400 when a non-empty
    model is requested that is not in the allowlist: the frontend only ever
    submits ids from /api/chat/models, so a mismatch indicates a stale client or
    a hand-crafted request, which we reject rather than silently downgrade to a
    different model than the user believes they selected.
    """
    if requested is None:
        return None
    want = requested.strip()
    if not want:
        return None
    allow = _alpha_lab_model_allowlist()
    if want not in allow:
        raise HTTPException(
            status_code=400,
            detail=f"model {want!r} is not selectable; choose one of {allow}",
        )
    return want


@app.get("/api/chat/models")
def api_chat_models(principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """Selectable models for the Alpha Lab picker.

    ``default`` is the server's configured model (always the first allowlist
    entry). ``models`` is the full selectable set (default + any
    ``ALPHA_LAB_MODELS`` extras). With no extras configured the list contains
    just the default and the picker shows a single read-only choice.
    """
    allow = _alpha_lab_model_allowlist()
    return {"models": allow, "default": allow[0] if allow else None}


@app.get("/api/chat/sessions")
def api_chat_sessions(limit: int = 50, db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    limit = max(1, min(500, int(limit)))
    rows = (
        db.query(AlphaChatSession)
        .order_by(AlphaChatSession.last_msg_at.desc())
        .limit(limit)
        .all()
    )
    return {"sessions": [r.to_dict() for r in rows], "limit": limit}


@app.get("/api/chat/sessions/{session_id}")
def api_chat_session_detail(session_id: int, db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    session = db.get(AlphaChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"AlphaChatSession {session_id} not found")
    msgs = (
        db.query(AlphaChatMessage)
        .filter(AlphaChatMessage.session_id == session_id)
        .order_by(AlphaChatMessage.ts.asc())
        .all()
    )
    return {
        "session": session.to_dict(),
        "messages": [m.to_dict() for m in msgs],
    }


# P-CHAT-RL — per-client token bucket for /api/chat/send (mirrors
# _factor_studio_rate_check). chat/send runs a blocking LLM round-trip in the
# bounded threadpool with no offload, so an unthrottled caller can exhaust
# workers and burn credits. Cap defaults to 20/min; override via
# ALPHA_CHAT_SEND_RATE_PER_MIN.
_CHAT_SEND_BUCKET: Dict[str, List[float]] = {}
_CHAT_SEND_BUCKET_LOCK = threading.Lock()


def _chat_send_rate_cap() -> int:
    try:
        cap = int(str(os.environ.get("ALPHA_CHAT_SEND_RATE_PER_MIN", "20")).strip() or "20")
    except (TypeError, ValueError):
        cap = 20
    return cap if cap > 0 else 20


def _chat_send_rate_check(request: Request) -> None:
    cap = _chat_send_rate_cap()
    from backend._envloader import env_bool as _env_bool
    if _env_bool("BEHIND_TRUSTED_PROXY", False):
        xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        real_ip = (request.headers.get("X-Real-IP") or "").strip()
        client_ip = xff or real_ip
    else:
        client_ip = ""
    key = client_ip or (request.client.host if request.client else "unknown") or "unknown"
    now = time.monotonic()
    with _CHAT_SEND_BUCKET_LOCK:
        for k in list(_CHAT_SEND_BUCKET.keys()):
            _CHAT_SEND_BUCKET[k] = [t for t in _CHAT_SEND_BUCKET[k] if now - t < 60.0]
            if not _CHAT_SEND_BUCKET[k]:
                _CHAT_SEND_BUCKET.pop(k, None)
        bucket = [t for t in _CHAT_SEND_BUCKET.get(key, []) if now - t < 60.0]
        if len(bucket) >= cap:
            raise HTTPException(429, "rate limited — chat sends capped per minute")
        bucket.append(now)
        _CHAT_SEND_BUCKET[key] = bucket


@app.post("/api/chat/send")
def api_chat_send(req: ChatSendRequest, request: Request, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """Send a user message + return the assistant reply.

    Creates a new session if `session_id` is None. Vision-capable models can
    receive `image_paths` (absolute server paths under storage/chat_images).

    P34-IDEMP: this is a paid LLM round-trip that persists rows + bumps
    message_count, so it MUST be idempotent. A double-click / slow-response
    client retry / flaky-network resend would otherwise insert duplicate
    user+assistant rows, double-bill the LLM, and corrupt message_count.
    Cache key = Idempotency-Key header; body hash = session_id + user_text +
    sorted(image_paths). Same key + same body replays the cached reply.

    NOTE: AlphaChatMessage.tool_calls is a forward-compat field. Today the
    assistant call is pure LLM-only; no tools are dispatched.
    """
    from datetime import datetime as _dt, timezone as _tz
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    req_hash = canonical_request_hash({
        "endpoint": "chat_send",
        "session_id": req.session_id,
        "user_text": req.user_text,
        "image_paths": sorted(req.image_paths or []),
        # P-MODEL-SEL — fold the model id into the body hash so the SAME prompt
        # sent with a different model is a distinct request, not an idempotent
        # replay of the earlier model's answer.
        "model": (req.model or "").strip() or None,
    })

    # P-MODEL-SEL — resolve + validate the optional per-message model override
    # against the allowlist BEFORE any idempotency/rate work. None => default.
    effective_model = _resolve_chat_model(req.model)

    # Synchronous replay check first — a confirmed replay returns the cached
    # response WITHOUT spending a rate-limit token. Only genuinely-fresh
    # requests fall through to the rate limiter (mirrors api_intake lines 422-459
    # and api_sources_poll lines 2096-2128).
    with session_scope() as _replay_db:
        _existing = None
        try:
            from backend.core.database import IdempotencyKey as _IK
            _existing = _replay_db.query(_IK).filter(_IK.key == key).one_or_none()
        except Exception:  # noqa: BLE001
            logger.exception(
                "chat_send: idempotency lookup failed key=%s; treating as fresh request",
                key[:16],
            )
            _existing = None
        if _existing is not None:
            if _existing.request_hash != req_hash:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "IDEMPOTENCY_KEY_REUSED",
                        "original_hash": _existing.request_hash,
                        "given_hash": req_hash,
                    },
                )
            try:
                _cached_payload = json.loads(_existing.response_json or "null")
            except (TypeError, ValueError):
                _cached_payload = None
            _cached_status = int(_existing.status_code or 200)
            if _cached_status >= 400:
                raise HTTPException(_cached_status, _cached_payload)
            return {**(_cached_payload or {}), "idempotent_replay": True}

    # Not a replay — apply rate-limit, then compute + persist.
    _chat_send_rate_check(request)

    with session_scope() as db:
        def _compute() -> Tuple[Dict[str, Any], int]:
            now = _dt.now(_tz.utc)
            session: Optional[AlphaChatSession] = None
            if req.session_id is not None:
                session = db.get(AlphaChatSession, req.session_id)
                if session is None:
                    return ({"error": "not_found", "detail": f"AlphaChatSession {req.session_id} not found"}, 404)
            if session is None:
                session = AlphaChatSession(
                    title=req.user_text[:80] or "New conversation",
                    started_at=now,
                    last_msg_at=now,
                    message_count=0,
                )
                db.add(session)
                db.flush()

            # B2-1 FIX: validate image paths BEFORE writing any DB rows.
            # Previously, db.add(user_msg) and the message_count UPDATE both
            # executed before this check. SQLAlchemy autoflush (triggered by the
            # 'prior' query below) would flush user_msg into the transaction, and
            # session_scope.commit() would persist the orphaned row + inflated
            # message_count even when a 400 was returned. By resolving images
            # first, any validation failure returns before touching the DB.
            resolved_images: Optional[List[str]] = None
            if req.image_paths:
                resolved_images = []
                assets_root = CHAT_ASSETS_DIR.resolve()
                for raw_p in req.image_paths:
                    p = Path(raw_p)
                    if not p.is_absolute():
                        p = PROJECT_ROOT / raw_p
                    try:
                        resolved = p.resolve(strict=True)
                    except (FileNotFoundError, OSError) as exc:
                        return ({"error": "validation", "detail": f"image_paths: {raw_p!r} is not a readable file ({exc})"}, 400)
                    if assets_root != resolved and assets_root not in resolved.parents:
                        return ({"error": "validation", "detail": f"image_paths: {raw_p!r} resolves outside the chat assets directory; refusing for path-traversal safety"}, 400)
                    resolved_images.append(str(resolved))

            image_paths_json = json.dumps(req.image_paths or [])
            user_msg = AlphaChatMessage(
                session_id=session.id,
                role="user",
                content=req.user_text,
                image_paths=image_paths_json,
                ts=now,
            )
            db.add(user_msg)
            # Atomic SQL increment instead of in-Python read-modify-write so two
            # concurrent /api/chat/send calls for the same session can't lose-update
            # message_count. db.refresh(session) below re-reads the authoritative value.
            db.query(AlphaChatSession).filter(AlphaChatSession.id == session.id).update(
                {
                    "message_count": AlphaChatSession.message_count + 1,
                    "last_msg_at": now,
                },
                synchronize_session=False,
            )

            # P-MINIMAX/CHAT-FIX — SessionLocal is autoflush=False, so the
            # just-added `user_msg` is NOT visible to the `prior` SELECT below
            # unless we flush first. Without this, the transcript omits the
            # CURRENT turn: a brand-new session sends an EMPTY user message
            # (strict providers like MiniMax reject it with "chat content is
            # empty"; lenient ones reply with a generic greeting), and follow-up
            # turns make the model answer the PREVIOUS message (off-by-one).
            # Flushing makes the current user message part of the prompt.
            db.flush()

            prior = (
                db.query(AlphaChatMessage)
                .filter(AlphaChatMessage.session_id == session.id)
                .order_by(AlphaChatMessage.ts.asc())
                .all()
            )
            transcript_lines: List[str] = []
            for m in prior:
                prefix = "USER" if m.role == "user" else "ASSISTANT"
                transcript_lines.append(f"=== {prefix} ===\n{m.content.strip()}\n")
            transcript = "\n".join(transcript_lines)
            # Defensive: never hand an empty user turn to the model. After the
            # flush above this is unreachable (user_text has min_length=1), but
            # it guarantees strict providers never get a "content is empty" 400.
            if not transcript.strip():
                transcript = f"=== USER ===\n{req.user_text.strip()}\n"

            try:
                raw = call_messages(
                    system=ALPHA_LAB_SYSTEM_PROMPT,
                    user=transcript,
                    max_tokens=1500,
                    temperature=0.4,
                    images=resolved_images,
                    model=effective_model,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Alpha Lab LLM call failed")
                # P34-IDEMP: do NOT return a 500 tuple — lookup_or_record would
                # record it and session_scope would commit it, replaying the stale
                # 500 forever. Raise so session_scope rolls back and the next retry
                # recomputes fresh (mirrors api_chat_extract).
                raise HTTPException(status_code=500, detail=f"LLM call failed: {type(exc).__name__}") from exc

            assistant_msg = AlphaChatMessage(
                session_id=session.id,
                role="assistant",
                content=raw.strip(),
                ts=_dt.now(_tz.utc),
            )
            db.add(assistant_msg)
            _assistant_now = _dt.now(_tz.utc)
            db.query(AlphaChatSession).filter(AlphaChatSession.id == session.id).update(
                {
                    "message_count": AlphaChatSession.message_count + 1,
                    "last_msg_at": _assistant_now,
                },
                synchronize_session=False,
            )
            db.flush()
            db.refresh(assistant_msg)
            db.refresh(session)

            return ({
                "session": session.to_dict(),
                "user_message": user_msg.to_dict(),
                "assistant_message": assistant_msg.to_dict(),
            }, 200)

        outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)

    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.delete("/api/chat/sessions/{session_id}", status_code=200)
def api_chat_session_delete(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """D-M4/P16 — idempotent chat session delete (cascades AlphaChatMessage)."""
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    body_payload = {"endpoint": "chat_session_delete", "session_id": int(session_id)}
    req_hash = canonical_request_hash(body_payload)

    def _compute() -> Tuple[Dict[str, Any], int]:
        session = db.get(AlphaChatSession, session_id)
        if session is None:
            return ({"deleted": True, "id": int(session_id), "already_absent": True}, 200)
        db.query(AlphaChatMessage).filter(AlphaChatMessage.session_id == session_id).delete()
        db.delete(session)
        db.flush()  # R3: atomic with the idempotency row via the single outer commit
        return ({"deleted": True, "id": int(session_id)}, 200)

    outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)
    # get_db() never commits; lookup_or_record only flushes. Commit so the
    # idempotency row persists across retries — otherwise replay protection
    # (body-hash conflict detection + cached response) is silently lost.
    db.commit()
    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


class ChatExtractRequest(BaseModel):
    title: Optional[str] = Field(None, max_length=120)


@app.post("/api/chat/sessions/{session_id}/extract", status_code=202)
def api_chat_extract(
    session_id: int,
    req: ChatExtractRequest,
    background: BackgroundTasks,
    request: Request,
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """D-H8a — idempotent chat extract → AlphaStrategy promotion.

    Cache key: session_id + payload hash. Retries replay the cached strategy_id
    instead of spawning a duplicate. The chat session's
    `extracted_to_strategy_id` is set on first-compute and the endpoint then
    refuses to re-extract the same session (409 inside _compute) — avoid
    duplicate strategies from a session that may have drifted topic.
    """
    from backend.agents.researcher import extract_from_chat_messages
    from backend.core.orchestrator import (
        PipelineStatus,
        WorkflowOrchestrator,
        _persist_status,
    )
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    body_payload = {
        "endpoint": "chat_extract",
        "session_id": int(session_id),
        "title": (req.title or "").strip(),
    }
    req_hash = canonical_request_hash(body_payload)

    # Captured for the background pipeline scheduling (only on first compute).
    captured: Dict[str, Any] = {}

    with session_scope() as db:
        def _compute() -> Tuple[Dict[str, Any], int]:
            session = db.get(AlphaChatSession, session_id)
            if session is None:
                return ({"error": "not_found", "detail": f"AlphaChatSession {session_id} not found"}, 404)
            if session.extracted_to_strategy_id:
                return ({
                    "error": "conflict",
                    "detail": (
                        f"Session already extracted to strategy "
                        f"{session.extracted_to_strategy_id}. Start a new conversation "
                        "if you want a different strategy."
                    ),
                }, 409)
            messages = (
                db.query(AlphaChatMessage)
                .filter(AlphaChatMessage.session_id == session_id)
                .order_by(AlphaChatMessage.ts.asc())
                .all()
            )
            if not messages:
                return ({"error": "validation", "detail": "Session has no messages to extract"}, 400)

            try:
                extracted = extract_from_chat_messages(messages)
            except ValueError as exc:
                return ({"error": "validation", "detail": str(exc)}, 422)
            except Exception as exc:  # noqa: BLE001
                logger.exception("extract LLM call failed")
                # P34-IDEMP: do NOT return a 500 tuple — lookup_or_record would
                # record it and session_scope would commit it, replaying the stale
                # 500 on every retry. Raise instead so nothing is persisted and the
                # next retry recomputes fresh (mirrors the sources_poll >=500 guard).
                # Surface only the exception class per the leakage policy.
                raise HTTPException(
                    status_code=500,
                    detail=f"Extract failed: {type(exc).__name__}",
                ) from exc

            title = (req.title or extracted.get("title") or "Extracted Alpha")[:120]

            new_row = AlphaStrategy(
                name=title,
                stage=2,  # STORY_GEN landing stage
                status=PipelineStatus.STORY_GEN.value,
                config_json=json.dumps({
                    "source": "alpha_lab_chat",
                    "source_chat_session_id": session_id,
                }),
            )
            db.add(new_row)
            db.flush()
            new_strategy_id = int(new_row.id)
            session.extracted_to_strategy_id = new_strategy_id

            captured["strategy_id"] = new_strategy_id
            captured["alpha_story"] = str(extracted.get("story") or "")
            captured["yaml_raw"] = str(extracted.get("backtest_config_yaml") or "")
            captured["backtest_config"] = extracted.get("backtest_config")
            captured["yaml_invalid"] = bool(extracted.get("config_yaml_invalid"))

            return ({
                "strategy_id": new_strategy_id,
                "status": "QUEUED",
                "status_url": f"/api/pipeline/status/{new_strategy_id}",
                "session_id": session_id,
            }, 202)  # R5/BE-API-005: persist 202 in the idempotency record, matching the declared status_code=202 and /api/pipeline/run line 1499

        outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)

    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)

    # Background pipeline ONLY on first-compute path (not replays) so retries
    # don't spawn a duplicate run.
    if not outcome.replay and captured:
        orchestrator = WorkflowOrchestrator()
        background.add_task(
            orchestrator.run_pipeline_from_story,
            captured["strategy_id"],
            alpha_story=captured["alpha_story"],
            backtest_config_yaml=captured["yaml_raw"],
            backtest_config=captured["backtest_config"],
            config_yaml_invalid=captured["yaml_invalid"],
            source_node_ids=[],
        )

    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


# ---------------------------------------------------------------------------
# P4 — Paper Trade forward simulation
# ---------------------------------------------------------------------------


class PaperTradeRequest(BaseModel):
    strategy_id: int = Field(..., gt=0)
    window_days: int = Field(30, ge=7, le=180)


@app.post("/api/paper-trade/run")
def api_paper_trade_run(req: PaperTradeRequest, request: Request, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """D-H6 — idempotent paper-trade run. Cache key: strategy_id + cadence.

    The per-strategy lock lives inside run_paper_trade so concurrent LLM /
    sandbox work serializes (relocated from _persist by D-M13). D-M18 maps
    FileNotFoundError (missing market data) to 503 — it is a system
    unavailability condition, not a client-side conflict.
    """
    from backend.core.paper_trader import run_paper_trade
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    body_payload = {
        "endpoint": "paper_trade_run",
        "strategy_id": int(req.strategy_id),
        "window_days": int(req.window_days),
    }
    req_hash = canonical_request_hash(body_payload)

    with session_scope() as s:
        def _compute() -> Tuple[Dict[str, Any], int]:
            try:
                result = run_paper_trade(req.strategy_id, window_days=req.window_days)
            except LookupError as exc:
                return ({"error": "not_found", "detail": type(exc).__name__}, 404)
            except FileNotFoundError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={"error": "data_unavailable", "detail": type(exc).__name__},
                ) from exc
            except Exception as exc:  # noqa: BLE001
                logger.exception("paper trade failed")
                raise HTTPException(
                    status_code=500,
                    detail={"error": "internal", "detail": type(exc).__name__},
                ) from exc
            # QT-27/PS: stamp the paper-scheduler cooldown so the periodic ticker
            # does not immediately re-simulate this strategy on its next tick
            # (manual /api/paper-trade/run previously bypassed _LAST_RUN). Best-effort.
            try:
                from backend.core import paper_scheduler as _ps
                _ps.record_last_run(int(req.strategy_id))
            except Exception:  # noqa: BLE001
                logger.warning(
                    "paper-trade run: failed to stamp scheduler cooldown for %s",
                    req.strategy_id,
                )
            return (result.to_dict(), 200)

        outcome = lookup_or_record(s, key=key, request_hash=req_hash, compute_fn=_compute)

    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.get("/api/paper-trade")
def api_paper_trade_list(limit: int = 100, offset: int = 0, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """D-L12/P16 — paginated paper-trade list, newest-first by mtime.

    Default page size 100, capped at 500. Without pagination this endpoint
    serialized the entire paper-trade store (potentially thousands of JSON
    blobs) on every poll.
    """
    from backend.core.paper_trader import list_all

    limit = max(1, min(500, int(limit)))
    offset = max(0, int(offset))
    runs = list_all()
    # Sort newest-first by run_at when present, falling back to strategy_id.
    runs.sort(
        key=lambda r: (r.get("run_at") or "", int(r.get("strategy_id") or 0)),
        reverse=True,
    )
    total = len(runs)
    window = runs[offset:offset + limit]
    return {
        "runs": window,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/paper-trade/{strategy_id}")
def api_paper_trade_detail(strategy_id: int, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core.paper_trader import latest_for

    data = latest_for(strategy_id)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"No paper-trade run for strategy {strategy_id}. POST /api/paper-trade/run first.",
        )
    return data


# ---------------------------------------------------------------------------
# P-SIM — Forward SIM account (genuine walk-forward, simulation only).
# Pins a start bar and scores only bars after it, accumulating as ingest
# advances. Full-ledger matching: see backend.core.sim_account. Never live.
# ---------------------------------------------------------------------------


class SimAccountStartRequest(BaseModel):
    strategy_id: int = Field(..., gt=0)
    initial_capital: float = Field(100000.0, gt=0, le=1e12)
    fee_bps: float = Field(0.0005, ge=0.0, le=0.01)
    slippage_bps: float = Field(0.0002, ge=0.0, le=0.01)


@app.post("/api/sim-account/start", status_code=201)
def api_sim_account_start(
    req: SimAccountStartRequest, request: Request, principal: str = Depends(require_operator)
) -> Dict[str, Any]:
    """Pin a strategy to forward simulation from the latest bar. Idempotent;
    re-pinning the same strategy RESETS its ledger for a fresh walk-forward."""
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )
    from backend.core import sim_account

    key = require_idempotency_key(request)
    req_hash = canonical_request_hash(
        {
            "endpoint": "sim_account_start",
            "strategy_id": int(req.strategy_id),
            "initial_capital": float(req.initial_capital),
            "fee_bps": float(req.fee_bps),
            "slippage_bps": float(req.slippage_bps),
        }
    )
    with session_scope() as s:
        def _compute() -> Tuple[Dict[str, Any], int]:
            try:
                result = sim_account.start_account(
                    req.strategy_id,
                    initial_capital=req.initial_capital,
                    fee_bps=req.fee_bps,
                    slippage_bps=req.slippage_bps,
                )
            except LookupError as exc:
                return ({"error": "not_found", "detail": str(exc)}, 404)
            except FileNotFoundError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={"error": "data_unavailable", "detail": str(exc)},
                ) from exc
            except Exception as exc:  # noqa: BLE001
                logger.exception("sim-account start failed")
                raise HTTPException(
                    status_code=500, detail={"error": "internal", "detail": type(exc).__name__}
                ) from exc
            return (result, 201)

        outcome = lookup_or_record(s, key=key, request_hash=req_hash, compute_fn=_compute)
    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.post("/api/sim-account/{strategy_id}/tick")
def api_sim_account_tick(strategy_id: int, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """Advance a SIM account over any new bars. Naturally idempotent (recompute
    from the immutable ledger), so no Idempotency-Key is required."""
    from backend.core import sim_account

    try:
        data = sim_account.tick_account(strategy_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail={"error": "data_unavailable", "detail": str(exc)}) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("sim-account tick failed")
        raise HTTPException(status_code=500, detail={"error": "internal", "detail": type(exc).__name__}) from exc
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"No SIM account for strategy {strategy_id}. POST /api/sim-account/start first.",
        )
    return data


@app.post("/api/sim-account/{strategy_id}/stop")
def api_sim_account_stop(strategy_id: int, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import sim_account

    data = sim_account.stop_account(strategy_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"No SIM account for strategy {strategy_id}.")
    return data


@app.get("/api/sim-account")
def api_sim_account_list(principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import sim_account

    accounts = sim_account.list_accounts()
    return {"accounts": accounts, "total": len(accounts)}


@app.get("/api/sim-account/{strategy_id}")
def api_sim_account_detail(strategy_id: int, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import sim_account

    data = sim_account.latest_for(strategy_id)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"No SIM account for strategy {strategy_id}. POST /api/sim-account/start first.",
        )
    return data


# ---------------------------------------------------------------------------
# P4 — Operator promotion / retirement endpoints
# ---------------------------------------------------------------------------


class PromoteRequest(BaseModel):
    target: str = Field(..., description="One of: PAPER_TRADE, SMALL_CAPITAL, LIVE")
    force: bool = Field(
        False,
        description=(
            "Bypass the paper-trade health-gate check. The override is logged "
            "and surfaced in the Telegram audit trail with a [FORCED] prefix. "
            "Use only for disaster-recovery or controlled demos."
        ),
    )
    force_reason: Optional[str] = Field(
        None, max_length=512,
        description="Human-readable reason recorded alongside force=True.",
    )


VALID_PROMOTIONS = {"PAPER_TRADE", "SMALL_CAPITAL", "LIVE"}

# Promote targets that REQUIRE a healthy paper-trade run before they fire.
# PAPER_TRADE itself is the run-enabling promotion so it stays unguarded.
GATED_PROMOTIONS = {"SMALL_CAPITAL", "LIVE"}


@app.post("/api/strategies/{strategy_id}/promote")
def api_strategy_promote(
    strategy_id: int,
    req: PromoteRequest,
    request: Request,
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """P13/D-H5 — idempotent promote.

    The endpoint mutates ``alpha_strategies.status``, writes a stage_transition
    row, and fires a telegram notification — all of which double up dangerously
    on retry. We require an ``Idempotency-Key`` header and cache the response
    so a network-timeout retry is a no-op replay.
    """
    from datetime import datetime, timezone

    from backend.core import paper_trader
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )
    from backend.core.thresholds import PAPER_RUN_MAX_AGE_HOURS

    key = require_idempotency_key(request)
    body_payload = {
        "endpoint": "promote",
        "strategy_id": int(strategy_id),
        "target": (req.target or "").upper(),
        "force": bool(req.force),
        "force_reason": (req.force_reason or "").strip(),
    }
    req_hash = canonical_request_hash(body_payload)

    target = (req.target or "").upper()
    if target not in VALID_PROMOTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"target must be one of {sorted(VALID_PROMOTIONS)}",
        )

    # D-H3 — single-session pattern. Compute mutations + idempotency row commit
    # in one transaction. Telegram notify happens AFTER commit to avoid
    # rollback-then-notify failure mode.
    captured: Dict[str, Any] = {}
    with session_scope() as db:
        def _compute() -> Tuple[Dict[str, Any], int]:
            row = db.get(AlphaStrategy, strategy_id)
            if row is None:
                return ({"error": "not_found", "detail": f"AlphaStrategy {strategy_id} not found"}, 404)
            if row.status not in {"APPROVED", "PAPER_TRADE", "SMALL_CAPITAL", "LIVE"}:
                return (
                    {"error": "conflict", "detail": f"Cannot promote strategy in status {row.status!r} — must be APPROVED or already promoted."},
                    409,
                )

            # P31-STATE2: enforce monotonic promotion. Reject demotions
            # (e.g. LIVE → PAPER_TRADE) — this endpoint is a one-way ratchet.
            # Use /retire to take the strategy out of production.
            _PROMOTION_RANK = {"APPROVED": 0, "PAPER_TRADE": 1, "SMALL_CAPITAL": 2, "LIVE": 3}
            cur_rank = _PROMOTION_RANK.get(row.status, -1)
            tgt_rank = _PROMOTION_RANK.get(target, -1)
            if tgt_rank < cur_rank:
                return (
                    {
                        "error": "conflict",
                        "detail": (
                            f"Refusing demotion {row.status!r} -> {target!r}. "
                            "Promote is monotonic; use /retire to take the "
                            "strategy out of production."
                        ),
                    },
                    409,
                )

            # --- Health gate (P5-BE-02) --------------------------------------------
            gate_info: Dict[str, Any] = {
                "target": target,
                "required": target in GATED_PROMOTIONS,
                "force": bool(req.force),
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            if target in GATED_PROMOTIONS and not req.force:
                latest = paper_trader.latest_for(strategy_id) or {}
                is_healthy = bool(latest.get("is_healthy"))
                run_at_raw = str(latest.get("run_at") or "").strip()
                age_hours: Optional[float] = None
                if run_at_raw:
                    try:
                        run_at_dt = datetime.strptime(run_at_raw, "%Y-%m-%dT%H:%M:%SZ").replace(
                            tzinfo=timezone.utc
                        )
                        age_hours = (datetime.now(timezone.utc) - run_at_dt).total_seconds() / 3600.0
                    except ValueError:
                        age_hours = None
                gate_info.update({
                    "is_healthy": is_healthy,
                    "health_notes": latest.get("health_notes") or [],
                    "run_at": run_at_raw or None,
                    "age_hours": round(age_hours, 2) if age_hours is not None else None,
                    "max_age_hours": PAPER_RUN_MAX_AGE_HOURS,
                })
                if not latest:
                    raise HTTPException(
                        status_code=412,
                        detail={
                            "message": (
                                f"Promotion to {target} requires a passing paper-trade run. "
                                "None found — POST /api/paper-trade/run first."
                            ),
                            "gate": gate_info,
                        },
                    )
                if not is_healthy:
                    raise HTTPException(
                        status_code=412,
                        detail={
                            "message": (
                                f"Promotion to {target} blocked — latest paper-trade run is "
                                "UNHEALTHY. Set `force=true` to override (audit-logged)."
                            ),
                            "gate": gate_info,
                        },
                    )
                if age_hours is None or age_hours > PAPER_RUN_MAX_AGE_HOURS:
                    raise HTTPException(
                        status_code=412,
                        detail={
                            "message": (
                                f"Promotion to {target} blocked — paper-trade run is stale "
                                f"(age={age_hours}h, max={PAPER_RUN_MAX_AGE_HOURS}h). "
                                "Rerun paper-trade or set `force=true` to override."
                            ),
                            "gate": gate_info,
                        },
                    )

            from_status = row.status
            from backend.core.orchestrator import PipelineStatus, STAGE_FOR_STATUS

            try:
                new_stage = STAGE_FOR_STATUS[PipelineStatus(target)]
            except (ValueError, KeyError):
                new_stage = row.stage
            # P32-WF32-1 / P31-STATE4: conditional UPDATE — only flip status
            # if it is STILL `from_status` in DB. A concurrent /retire,
            # /pause-all, or a duplicate /promote (same idempotency_key racing
            # the lookup) could have committed a different transition while we
            # were running the gate checks. updated==0 => return 409 so the
            # caller knows the transition was rejected by a race, not silently
            # overwritten.
            updated = (
                db.query(AlphaStrategy)
                .filter(AlphaStrategy.id == int(strategy_id))
                .filter(AlphaStrategy.status == from_status)
                .update(
                    {"status": target, "stage": new_stage},
                    synchronize_session=False,
                )
            )
            if not updated:
                db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"strategy {strategy_id} is no longer {from_status!r} "
                        "(concurrent transition committed by another request); "
                        "re-read /api/strategies and retry if still appropriate."
                    ),
                )
            db.refresh(row)

            cfg = row.config()
            cfg["gate_passed_at"] = gate_info["checked_at"]
            cfg["gate_target"] = target
            cfg["gate_force"] = bool(req.force)
            if req.force and req.force_reason:
                cfg["gate_force_reason"] = req.force_reason.strip()[:512]
            if "is_healthy" in gate_info:
                cfg["gate_was_healthy"] = bool(gate_info.get("is_healthy"))
            row.config_json = json.dumps(cfg, default=str)

            # P13/D-M7 — fail-closed on audit log: if record_transition fails we
            # MUST roll back the status change, otherwise we have a silent
            # promotion with no transition row (untraceable in /alpha-flow).
            from backend.core.transition_log import record_transition

            reason_parts = []
            if req.force:
                reason_parts.append("forced")
            if req.force and req.force_reason:
                reason_parts.append(req.force_reason.strip()[:200])
            try:
                record_transition(
                    db,
                    strategy_id=strategy_id,
                    from_status=from_status,
                    to_status=target,
                    from_stage=int(row.stage or 0),
                    to_stage=new_stage,
                    actor="operator",
                    reason=("; ".join(reason_parts) or None),
                )
            except Exception as audit_exc:  # noqa: BLE001
                logger.exception("StageTransition write failed during promote — rolling back")
                db.rollback()
                raise HTTPException(
                    status_code=500,
                    detail=f"Promote aborted: audit write failed ({type(audit_exc).__name__})",
                ) from audit_exc

            db.flush()
            db.refresh(row)
            result = row.to_dict()
            captured["result"] = result
            captured["metrics"] = row.metrics()
            captured["name"] = row.name or f"strategy_{strategy_id}"
            captured["from_status"] = from_status
            return (result, 200)

        outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)

    # Notify outside the DB transaction so a notify failure cannot corrupt the
    # committed state. Only on first-compute (skip on replay).
    if not outcome.replay and captured:
        note = "[FORCED] " if req.force else ""
        try:
            from backend.core.telegram_notifier import notify_strategy_transition

            notify_strategy_transition(
                strategy_id,
                name=f"{note}{captured['name']}",
                from_status=captured["from_status"],
                to_status=target,
                metrics=captured["metrics"],
            )
        except Exception:  # noqa: BLE001
            logger.exception("Promote telegram notify failed (non-fatal)")
        # P-DISCORD-OUT — parallel best-effort Discord post (off by default).
        try:
            from backend.core import discord_notifier
            discord_notifier.notify_strategy_transition(
                strategy_id,
                name=f"{note}{captured['name']}",
                from_status=captured["from_status"],
                to_status=target,
                metrics=captured["metrics"],
            )
        except Exception:  # noqa: BLE001
            logger.exception("Promote discord notify failed (non-fatal)")

    response = outcome.response_payload or {}
    return {**response, "idempotent_replay": bool(outcome.replay)}


@app.post("/api/strategies/{strategy_id}/retire")
def api_strategy_retire(strategy_id: int, request: Request, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """P13/D-H5 — idempotent retire.

    Same retry-safety story as promote: mutates status to GRAVEYARD, writes a
    transition row, fires a telegram. Wrapped in lookup_or_record so retries
    replay the cached response.
    """
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    body_payload = {"endpoint": "retire", "strategy_id": int(strategy_id)}
    req_hash = canonical_request_hash(body_payload)

    # D-H3 — single-session pattern. Compute mutations + idempotency row commit
    # in one transaction. Telegram notify happens AFTER commit to avoid
    # rollback-then-notify failure mode.
    captured: Dict[str, Any] = {}
    with session_scope() as db:
        def _compute() -> Tuple[Dict[str, Any], int]:
            row = db.get(AlphaStrategy, strategy_id)
            if row is None:
                # R5/BE-API-002: return a tuple (not raise) so lookup_or_record
                # caches the 404 and retries replay it instead of re-hitting the DB.
                return ({"error": "not_found", "detail": f"AlphaStrategy {strategy_id} not found"}, 404)
            # P31-STATE8: retire is one-way. Reject no-op retires of strategies
            # that are already terminal — those just generate phantom transition
            # rows and duplicate notifications.
            if row.status == "GRAVEYARD":
                return (row.to_dict(), 200)
            if row.status == "REJECTED":
                return (
                    {
                        "error": "already_terminal",
                        "detail": (
                            f"strategy {strategy_id} is already terminal "
                            f"({row.status!r}); /retire only valid for active "
                            "strategies (APPROVED/PAPER_TRADE/SMALL_CAPITAL/LIVE)."
                        ),
                    },
                    409,
                )
            if row.status == "PAUSED":
                return (
                    {
                        "error": "paused",
                        "detail": (
                            f"strategy {strategy_id} is PAUSED — resume it first "
                            "(POST /api/live-trade/resume/{id}) before /retire so "
                            "the pre_pause_status snapshot is not silently discarded."
                        ),
                    },
                    409,
                )
            from_status = row.status
            prior_stage = int(row.stage or 0)
            # P32-WF32-1 / P31-STATE4: conditional UPDATE — only flip status
            # if it is STILL `from_status` in DB. A concurrent /retire or
            # /promote could have committed while the pre-retire validation
            # above ran (early-return for GRAVEYARD only fires for the row we
            # READ, not what's in DB right now). updated==0 => 409 so the
            # caller re-reads and the second /retire is not silently lost.
            updated = (
                db.query(AlphaStrategy)
                .filter(AlphaStrategy.id == int(strategy_id))
                .filter(AlphaStrategy.status == from_status)
                .update(
                    {"status": "GRAVEYARD", "stage": 7},
                    synchronize_session=False,
                )
            )
            if not updated:
                db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"strategy {strategy_id} is no longer {from_status!r} "
                        "(concurrent transition committed); re-read and retry."
                    ),
                )
            db.refresh(row)
            # P13/D-M7 — fail-closed audit write.
            from backend.core.transition_log import record_transition
            try:
                record_transition(
                    db,
                    strategy_id=strategy_id,
                    from_status=from_status,
                    to_status="GRAVEYARD",
                    from_stage=prior_stage,
                    to_stage=7,
                    actor="operator",
                    reason="retired",
                )
            except Exception as audit_exc:  # noqa: BLE001
                logger.exception("StageTransition write failed during retire — rolling back")
                db.rollback()
                raise HTTPException(
                    status_code=500,
                    detail=f"Retire aborted: audit write failed ({type(audit_exc).__name__})",
                ) from audit_exc
            db.flush()
            db.refresh(row)
            captured["result"] = row.to_dict()
            captured["name"] = row.name or f"strategy_{strategy_id}"
            captured["from_status"] = from_status
            return (row.to_dict(), 200)

        outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)

    # R5/BE-API-002: replay cached error responses as real HTTP errors, matching
    # every other idempotency-wrapped handler. Without this, an error tuple from
    # _compute (404/409) would be returned as HTTP 200 with an error body.
    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)

    # Notify outside the DB transaction so a notify failure cannot corrupt the
    # committed state.
    if not outcome.replay and captured:
        try:
            from backend.core.telegram_notifier import notify_strategy_transition
            notify_strategy_transition(
                strategy_id,
                name=captured["name"],
                from_status=captured["from_status"],
                to_status="GRAVEYARD",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Retire telegram notify failed (non-fatal)")
        # P-DISCORD-OUT — parallel best-effort Discord post (off by default).
        try:
            from backend.core import discord_notifier
            discord_notifier.notify_strategy_transition(
                strategy_id,
                name=captured["name"],
                from_status=captured["from_status"],
                to_status="GRAVEYARD",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Retire discord notify failed (non-fatal)")

    response = outcome.response_payload or {}
    return {**response, "idempotent_replay": bool(outcome.replay)}


# ---------------------------------------------------------------------------
# P4 — Stage 6 Live Deploy (stub — exchange SDK integration ships separately)
# ---------------------------------------------------------------------------


@app.post("/api/live/deploy/{strategy_id}", status_code=501)
def api_live_deploy(strategy_id: int, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    raise HTTPException(
        status_code=501,
        detail=(
            f"Live deployment for strategy {strategy_id} requires an exchange SDK "
            "(Binance / OKX / Bybit) integration that is intentionally stubbed in "
            "this iteration. Wire your exchange client into "
            "backend/core/live_executor.py and re-enable this endpoint."
        ),
    )


@app.get("/api/gate-criteria")
def api_gate_criteria(db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """Return the BG Pipeline Gate Criteria rules + per-rule pass state for
    every strategy that has metrics. Drives the right-hand checklist panel
    on /backtest-panel.
    """
    rules = _effective_gate_criteria()
    strategies = db.query(AlphaStrategy).order_by(AlphaStrategy.id.desc()).limit(_LIST_HARD_LIMIT).all()

    # Parse each strategy's metrics and config once; reuse across all rules.
    parsed_strategies: List[tuple] = []
    for st in strategies:
        m = st.metrics() or {}
        c = st.config() or {}
        parsed_strategies.append((m, c))

    evaluations: List[Dict[str, Any]] = []
    for rule in rules:
        passed = 0
        total = 0
        for m, c in parsed_strategies:
            metrics = m
            raw = metrics.get(rule["metric"])
            if raw is None and rule["metric"] == "trades":
                raw = c.get("trades")
            if raw is None:
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            total += 1
            op = rule["operator"]
            thr = rule["threshold"]
            if op == ">":
                ok = v > thr
            elif op == ">=":
                ok = v >= thr
            elif op == "<":
                ok = v < thr
            elif op == "<=":
                ok = v <= thr
            else:
                ok = False
            if ok:
                passed += 1
        evaluations.append({
            **_gate_rule_public(rule),
            "passed": passed,
            "total": total,
            "ratio": round((passed / total) if total else 0.0, 4),
        })
    return {"rules": evaluations}


class GateCriterionUpdate(BaseModel):
    key: str = Field(..., min_length=1, max_length=64)
    # Operator-facing DISPLAY value (e.g. -25 for MaxDD "%"); converted back to
    # the canonical stored threshold via the rule's scale on save.
    value: float


class GateCriteriaUpdateRequest(BaseModel):
    rules: List[GateCriterionUpdate] = Field(..., min_length=1, max_length=64)


@app.put("/api/gate-criteria")
def api_gate_criteria_update(
    body: GateCriteriaUpdateRequest,
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """Persist operator overrides for the gate-criteria checklist thresholds.

    Scope: affects ONLY the /api/gate-criteria evaluation (the at-a-glance
    checklist on /gate-review and /backtest-panel). The Critic's hard-rejection
    floor and the paper-trade health gate read backend/core/thresholds.py and
    are intentionally NOT changed here. Each `value` is the operator-facing
    display value; it is clamped to the rule's [min, max], converted to the
    canonical stored threshold via the rule's scale, and (for int rules) rounded.
    """
    valid_keys = {r["key"] for r in _GATE_CRITERIA}
    with _GATE_OVERRIDES_LOCK:
        overrides = _load_gate_overrides()
        applied: List[str] = []
        for upd in body.rules:
            if upd.key not in valid_keys:
                raise HTTPException(status_code=422, detail=f"unknown gate rule key: {upd.key}")
            dv = float(upd.value)
            if not _gate_is_finite(dv):
                raise HTTPException(status_code=422, detail=f"{upd.key}: value must be finite")
            meta = _GATE_EDIT_META.get(upd.key, {})
            lo, hi = meta.get("min"), meta.get("max")
            if lo is not None and dv < lo:
                dv = float(lo)
            if hi is not None and dv > hi:
                dv = float(hi)
            scale = float(meta.get("scale", 1.0) or 1.0)
            canonical = dv / scale if scale else dv
            if meta.get("kind") == "int":
                canonical = float(int(round(canonical)))
            overrides[upd.key] = {"threshold": canonical}
            applied.append(upd.key)
        _save_gate_overrides(overrides)
    logger.info("gate-criteria overrides updated by %s: %s", principal, applied)
    # Return fresh effective rules (display only — the FE refetches GET for the
    # live pass/total counts against the new thresholds).
    return {"ok": True, "updated": applied,
            "rules": [_gate_rule_public(r) for r in _effective_gate_criteria()]}


@app.post("/api/gate-criteria/reset")
def api_gate_criteria_reset(principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """Clear ALL operator overrides → revert the checklist to backend defaults."""
    with _GATE_OVERRIDES_LOCK:
        try:
            _GATE_OVERRIDES_PATH.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="could not reset gate overrides") from exc
    logger.info("gate-criteria overrides reset by %s", principal)
    return {"ok": True, "rules": [_gate_rule_public(r) for r in _effective_gate_criteria()]}


# ===========================================================================
# P7 — 11 placeholder pages turned live. 46 new endpoints grouped by page.
# ---------------------------------------------------------------------------
# Conventions:
#   - All read-only endpoints return 200 + payload even when empty (the FE
#     renders an empty state); only validation errors raise HTTPException.
#   - High-risk endpoints (live-trade pause-all, trading-terminal submit /
#     cancel) require Idempotency-Key headers via backend.core.idempotency.
#   - Cost-guard env flags default OFF — endpoints return 503 when disabled
#     so the FE can show a clear "set X=1 to enable" hint.
# ===========================================================================

from fastapi import Header
from pydantic import BaseModel as _PydBaseModel


# --- P7-01 /pipeline-analytics ---------------------------------------------

@app.get("/api/pipeline-analytics/throughput")
def api_pa_throughput(days: int = 30, bucket: str = "day", db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import pipeline_analytics as pa
    return pa.throughput(db, days=days, bucket=bucket)


@app.get("/api/pipeline-analytics/time-in-stage")
def api_pa_time_in_stage(days: int = 90, db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import pipeline_analytics as pa
    return pa.time_in_stage(db, days=days)


@app.get("/api/pipeline-analytics/gate-pass-rate")
def api_pa_gate_pass_rate(days: int = 90, window: int = 14, db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import pipeline_analytics as pa
    return pa.gate_pass_rate(db, days=days, window=window)


@app.get("/api/pipeline-analytics/occupancy")
def api_pa_occupancy(db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import pipeline_analytics as pa
    return pa.occupancy(db)


# --- P7-02 /mission-panel ---------------------------------------------------

@app.get("/api/mission-panel/snapshot")
def api_mp_snapshot(db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import mission_panel
    return mission_panel.snapshot(db)


@app.get("/api/mission-panel/incidents")
def api_mp_incidents(hours: int = 24, limit: int = 50, db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import mission_panel
    return mission_panel.incidents_paged(db, hours=hours, limit=limit)


_MANUAL_FIRE_LOCK = threading.Lock()


@app.post("/api/mission-panel/fire-six-hour-now")
def api_mp_fire_six_hour(request: Request, force: bool = False, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """D-H9 — idempotent mission-panel manual six-hour pulse.

    Cache key includes ymd_hour so retries within the same hour replay; a new
    hour gets a fresh first-compute path. The Idempotency-Key header is also
    required so concurrent clicks dedup atomically.
    """
    from datetime import datetime, timezone
    from backend.core import telegram_reports
    from backend.core.telegram_notifier import is_enabled
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )
    if not is_enabled():
        raise HTTPException(503, "Telegram notifier not configured (TELEGRAM_ENABLED=0)")

    key = require_idempotency_key(request)
    ymd_hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    body_payload = {"endpoint": "manual-pulse", "ymd_hour": ymd_hour, "force": bool(force)}
    req_hash = canonical_request_hash(body_payload)

    # P34-IDEMP: reject cooldown BEFORE lookup_or_record. A 409 returned inside
    # _compute would be recorded and committed by session_scope, replaying the
    # same 409 for the rest of the UTC-hour cache window even after the cooldown
    # expires. Raising here keeps the transient cooldown out of the idempotency
    # cache so the next retry re-evaluates the live state. _within_cooldown is a
    # read-only check; the in-_compute check below remains as a race-safe guard.
    if not force:
        with session_scope() as _s_cd:
            if telegram_reports._within_cooldown(_s_cd, telegram_reports.RPT_SIX_HOUR):
                raise HTTPException(
                    status_code=409,
                    detail={"error": "cooldown", "detail": "Within cooldown — pass ?force=true to override"},
                )

    with session_scope() as s:
        def _compute() -> Tuple[Dict[str, Any], int]:
            with _MANUAL_FIRE_LOCK:
                if not force and telegram_reports._within_cooldown(s, telegram_reports.RPT_SIX_HOUR):
                    raise HTTPException(status_code=409, detail={"error": "cooldown", "detail": "Within cooldown — pass ?force=true to override"})
                ok = telegram_reports.send_six_hour_report(force=True)
            return ({"ok": True, "sent": bool(ok)}, 200)

        outcome = lookup_or_record(s, key=key, request_hash=req_hash, compute_fn=_compute)

    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


# --- P7-03 /backtest-lab ----------------------------------------------------

class _SweepCreateReq(_PydBaseModel):
    strategy_id: int
    param_x_name: str
    param_x_values: List[float] = Field(..., min_length=1, max_length=50)
    param_y_name: Optional[str] = None
    param_y_values: Optional[List[float]] = Field(None, max_length=50)
    seed: int = 42


@app.get("/api/backtest-lab/params")
def api_bl_params() -> Dict[str, Any]:
    from backend.core import backtest_lab
    return backtest_lab.list_params()


@app.post("/api/backtest-lab/sweep")
def api_bl_create_sweep(
    req: _SweepCreateReq,
    background: BackgroundTasks,
    request: Request,
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """D-H10 — idempotent backtest-lab sweep create. Cache key: canonical_json(payload)."""
    from backend.core import backtest_lab
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )
    if not backtest_lab.is_enabled():
        raise HTTPException(503, "BACKTEST_LAB_ENABLED=0")

    key = require_idempotency_key(request)
    payload = req.dict()
    req_hash = canonical_request_hash(payload)

    with session_scope() as db:
        def _compute() -> Tuple[Dict[str, Any], int]:
            try:
                row = backtest_lab.create_sweep(
                    db,
                    strategy_id=req.strategy_id,
                    param_x_name=req.param_x_name,
                    param_x_values=req.param_x_values,
                    param_y_name=req.param_y_name,
                    param_y_values=req.param_y_values,
                    seed=req.seed,
                )
                db.flush()
            except PermissionError as exc:
                return ({"error": "conflict", "detail": str(exc)}, 409)
            except ValueError as exc:
                return ({"error": "validation", "detail": str(exc)}, 422)
            return ({"sweep_id": int(row.id), "cells_total": int(row.cells_total), "status": row.status}, 200)

        outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)

    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)

    response = outcome.response_payload or {}
    sweep_id_val = int(response.get("sweep_id") or 0)

    # Only spawn the background runner on first-compute path (skip on replay).
    if not outcome.replay and sweep_id_val > 0:
        background.add_task(backtest_lab.run_sweep, sweep_id_val)

    return {**response, "idempotent_replay": bool(outcome.replay)}


@app.get("/api/backtest-lab/sweep/{sweep_id}")
def api_bl_get_sweep(sweep_id: int, db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core.database import BacktestSweep
    row = db.get(BacktestSweep, int(sweep_id))
    if row is None:
        raise HTTPException(404, f"sweep {sweep_id} not found")
    return row.to_dict()


@app.get("/api/backtest-lab/sweeps")
def api_bl_list_sweeps(strategy_id: int, limit: int = 20, db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core.database import BacktestSweep
    rows = (
        db.query(BacktestSweep)
        .filter(BacktestSweep.strategy_id == int(strategy_id))
        .order_by(BacktestSweep.created_at.desc())
        .limit(max(1, min(int(limit), 100)))
        .all()
    )
    return {"sweeps": [r.to_dict() for r in rows]}


@app.delete("/api/backtest-lab/sweep/{sweep_id}")
def api_bl_cancel(
    sweep_id: int,
    request: Request,
    db: Session = Depends(get_db),
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """D-M5/P16 — idempotent sweep cancel."""
    from backend.core import backtest_lab
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    body_payload = {"endpoint": "bl_cancel", "sweep_id": int(sweep_id)}
    req_hash = canonical_request_hash(body_payload)

    def _compute() -> Tuple[Dict[str, Any], int]:
        ok = backtest_lab.cancel_sweep(db, sweep_id)
        db.flush()  # R3: atomic with the idempotency row via the single outer commit
        return ({"cancelled": bool(ok), "id": int(sweep_id), "deleted": True}, 200)

    outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)
    # get_db() never commits; lookup_or_record only flushes. Commit so the
    # idempotency row persists — otherwise a retried cancel re-runs _compute
    # instead of replaying the cached response.
    db.commit()
    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


# --- P7-05 /alpha-flow ------------------------------------------------------

@app.get("/api/alpha-flow/sankey")
def api_af_sankey(days: int = 30, db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import alpha_flow
    return alpha_flow.sankey(db, days=days)


@app.get("/api/alpha-flow/strategy/{strategy_id}/timeline")
def api_af_strategy_timeline(strategy_id: int, db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import alpha_flow
    return alpha_flow.strategy_timeline(db, strategy_id)


@app.get("/api/alpha-flow/dropout-stats")
def api_af_dropout_stats(days: int = 30, db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import alpha_flow
    return alpha_flow.dropout_stats(db, days=days)


# --- P7-06 /ir-explorer -----------------------------------------------------

def _parse_statuses(q: Optional[str]) -> List[str]:
    if not q:
        return list(("APPROVED", "PAPER_TRADE", "SMALL_CAPITAL", "LIVE"))
    return [s.strip().upper() for s in str(q).split(",") if s.strip()]


@app.get("/api/ir-explorer/book-ir")
def api_ir_book(statuses: Optional[str] = None, benchmark: str = "btc", db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import ir_explorer
    return ir_explorer.book_ir(db, statuses=_parse_statuses(statuses), benchmark=benchmark)


@app.get("/api/ir-explorer/by-category")
def api_ir_cat(statuses: Optional[str] = None, benchmark: str = "btc", db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import ir_explorer
    return ir_explorer.by_category(db, statuses=_parse_statuses(statuses), benchmark=benchmark)


@app.get("/api/ir-explorer/by-regime")
def api_ir_regime(statuses: Optional[str] = None, benchmark: str = "btc", db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import ir_explorer
    return ir_explorer.by_regime(db, statuses=_parse_statuses(statuses), benchmark=benchmark)


@app.get("/api/ir-explorer/by-asset")
def api_ir_asset(statuses: Optional[str] = None, benchmark: str = "btc", db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import ir_explorer
    return ir_explorer.by_asset(db, statuses=_parse_statuses(statuses), benchmark=benchmark)


@app.get("/api/ir-explorer/rolling")
def api_ir_rolling(statuses: Optional[str] = None, benchmark: str = "btc", window: int = 30, db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import ir_explorer
    return ir_explorer.rolling(db, statuses=_parse_statuses(statuses), benchmark=benchmark, window=window)


@app.get("/api/ir-explorer/waterfall")
def api_ir_waterfall(statuses: Optional[str] = None, benchmark: str = "btc", db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import ir_explorer
    return ir_explorer.waterfall(db, statuses=_parse_statuses(statuses), benchmark=benchmark)


# --- P7-07 /portfolio-optimizer ---------------------------------------------

class _PoConstraints(_PydBaseModel):
    max_weight: float = 0.30
    min_weight: float = 0.0
    allow_short: bool = False
    vol_target_annual: float = 0.15
    rebalance_freq: str = "D"

    @model_validator(mode="after")
    def _v_weight_bounds(self) -> "_PoConstraints":
        mx = float(self.max_weight)
        mn = float(self.min_weight)
        vt = float(self.vol_target_annual)
        # reject NaN / inf
        if mx != mx or mx in (float("inf"), float("-inf")):
            raise ValueError("max_weight must be a finite number")
        if mn != mn or mn in (float("inf"), float("-inf")):
            raise ValueError("min_weight must be a finite number")
        if vt != vt or not (0.0 < vt < 10.0):
            raise ValueError("vol_target_annual must be a finite positive number less than 10.0 (1000% annualised)")
        if not (0.0 < mx <= 1.0):
            raise ValueError("max_weight must be in (0, 1]")
        if not (0.0 <= mn <= mx):
            raise ValueError("min_weight must be in [0, max_weight]")
        return self


class _PoCombineReq(_PydBaseModel):
    # R5/SEC-05: bound the list (was unbounded -> CPU/DB DoS). min 2 mirrors the
    # explicit check in api_po_combine; 50 is far above any real portfolio.
    strategy_ids: List[int] = Field(..., min_length=2, max_length=50)
    method: str = "equal_weight"
    constraints: Optional[_PoConstraints] = None


class _PoSaveReq(_PydBaseModel):
    name: str
    strategy_ids: List[int]
    weights: Dict[str, float]
    method: str
    constraints: Optional[Dict[str, Any]] = None


@app.post("/api/portfolio-optimizer/combine")
def api_po_combine(req: _PoCombineReq, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import portfolio_optimizer
    if len(req.strategy_ids) < 2:
        raise HTTPException(422, "pick at least 2 strategies")
    return portfolio_optimizer.combine_extended(
        strategy_ids=req.strategy_ids,
        method=req.method,
        constraints=(req.constraints.dict() if req.constraints else None),
    )


@app.post("/api/portfolio-optimizer/save")
def api_po_save(req: _PoSaveReq, request: Request, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """D-H8b — idempotent portfolio-optimizer save. Cache key: canonical_json(payload)."""
    from backend.core import portfolio_optimizer
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    payload = req.dict()
    req_hash = canonical_request_hash(payload)

    with session_scope() as db:
        def _compute() -> Tuple[Dict[str, Any], int]:
            try:
                weights_int = {int(k): float(v) for k, v in (req.weights or {}).items()}
                row = portfolio_optimizer.save_portfolio(
                    db,
                    name=req.name,
                    strategy_ids=req.strategy_ids,
                    weights=weights_int,
                    method=req.method,
                    constraints=req.constraints,
                )
                db.flush()
                return (row.to_dict(), 200)
            except ValueError as exc:
                return ({"error": "validation", "detail": str(exc)}, 422)

        outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)

    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.get("/api/portfolio-optimizer/saved")
def api_po_saved(db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import portfolio_optimizer
    return portfolio_optimizer.list_saved(db)


@app.delete("/api/portfolio-optimizer/saved/{portfolio_id}")
def api_po_delete(
    portfolio_id: int,
    request: Request,
    db: Session = Depends(get_db),
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """D-M6/P16 — idempotent saved-portfolio delete."""
    from backend.core import portfolio_optimizer
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    body_payload = {"endpoint": "po_delete", "portfolio_id": int(portfolio_id)}
    req_hash = canonical_request_hash(body_payload)

    def _compute() -> Tuple[Dict[str, Any], int]:
        ok = portfolio_optimizer.delete_saved(db, portfolio_id)
        db.flush()  # R3: atomic with the idempotency row via the single outer commit
        if not ok:
            return (
                {"error": "not_found", "detail": f"portfolio {portfolio_id} not found"},
                404,
            )
        return ({"deleted": True, "id": int(portfolio_id)}, 200)

    outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)
    # get_db() never commits; lookup_or_record only flushes. Commit here so the
    # idempotency row survives — otherwise a retried successful delete re-runs
    # _compute, finds the portfolio already gone, and returns 404 instead of the
    # cached 200.
    db.commit()
    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.get("/api/portfolio-optimizer/frontier")
def api_po_frontier(strategies: str, vol_min: float = 0.05, vol_max: float = 0.30, steps: int = 10, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import portfolio_optimizer
    if not strategies or not strategies.strip():
        raise HTTPException(422, "strategies must be a non-empty comma-separated list of integers")
    try:
        sids = [int(x) for x in strategies.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(422, "strategies must be a comma-separated list of integers")
    if len(sids) < 2:
        raise HTTPException(422, "at least 2 strategy IDs required")
    if not (0.0 < vol_min < vol_max <= 1.0):
        raise HTTPException(422, f"vol_min/vol_max must satisfy 0 < vol_min < vol_max <= 1.0 (got vol_min={vol_min}, vol_max={vol_max})")
    if steps < 2:
        raise HTTPException(422, f"steps must be >= 2 (got {steps})")
    try:
        return portfolio_optimizer.efficient_frontier(
            strategy_ids=sids, vol_min=vol_min, vol_max=vol_max, steps=steps,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))


# --- P7-08 /factor-studio ---------------------------------------------------

class _FsEvalReq(_PydBaseModel):
    formula_code: str = Field(..., min_length=1, max_length=65_536)
    asset_symbol: str = Field("BTC", max_length=32)
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    timeout_seconds: Optional[float] = 8.0


class _FsSaveReq(_PydBaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    formula_code: str = Field(..., min_length=1, max_length=65_536)
    asset_symbol: str = Field("BTC", max_length=32)
    params: Optional[Dict[str, Any]] = None
    eval_result: Optional[Dict[str, Any]] = None
    overwrite: bool = False


# Tiny token-bucket rate limit (per-process, in-memory).
_FS_BUCKET: Dict[str, List[float]] = {}
_FS_BUCKET_LOCK = threading.Lock()


def _factor_studio_rate_check(request: Request) -> None:
    from backend.core import factor_evaluator
    cap = factor_evaluator.rate_limit_per_min()
    # P15/D-M11 — when behind a trusted reverse proxy, honour X-Forwarded-For
    # (first hop) or X-Real-IP so the rate limit applies per real client, not
    # per proxy IP. Only trust the header when explicitly enabled via env so
    # deployments without a proxy can't be spoofed.
    from backend._envloader import env_bool as _env_bool
    if _env_bool("BEHIND_TRUSTED_PROXY", False):
        xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        real_ip = (request.headers.get("X-Real-IP") or "").strip()
        client_ip = xff or real_ip
    else:
        client_ip = ""
    key = client_ip or (request.client.host if request.client else "unknown") or "unknown"
    # P32-TZ32-6 — monotonic clock for rate-limit window; immune to wall-clock jumps.
    now = time.monotonic()
    with _FS_BUCKET_LOCK:
        # P15/D-M22 — drop bucket entries with no live tokens (oldest > 60s)
        # so the per-process dict can't grow unbounded over many distinct IPs.
        for k in list(_FS_BUCKET.keys()):
            _FS_BUCKET[k] = [t for t in _FS_BUCKET[k] if now - t < 60.0]
            if not _FS_BUCKET[k]:
                _FS_BUCKET.pop(k, None)
        bucket = [t for t in _FS_BUCKET.get(key, []) if now - t < 60.0]
        if len(bucket) >= cap:
            raise HTTPException(429, "rate limited — factor evaluations capped per minute")
        bucket.append(now)
        _FS_BUCKET[key] = bucket


@app.post("/api/factor-studio/evaluate")
def api_fs_evaluate(req: _FsEvalReq, request: Request, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import factor_evaluator
    from backend.core.sandbox import SandboxExecutionError, SandboxValidationError
    if not factor_evaluator.is_enabled():
        raise HTTPException(503, "FACTOR_STUDIO_ENABLED=0")
    _factor_studio_rate_check(request)
    try:
        return factor_evaluator.evaluate(
            req.formula_code,
            asset_symbol=req.asset_symbol,
            period_start=req.period_start,
            period_end=req.period_end,
            timeout_seconds=req.timeout_seconds or 8.0,
        )
    except SandboxValidationError as exc:
        raise HTTPException(400, {"error_class": "validation", "detail": str(exc)})
    except SandboxExecutionError as exc:
        raise HTTPException(422, {"error_class": "execution", "detail": str(exc)})


@app.post("/api/factor-studio/save")
def api_fs_save(req: _FsSaveReq, request: Request, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """D-H8c — idempotent factor-studio save. Cache key: canonical_json(payload)."""
    from backend.core import factor_evaluator
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    payload = req.dict()
    req_hash = canonical_request_hash(payload)

    with session_scope() as db:
        def _compute() -> Tuple[Dict[str, Any], int]:
            try:
                row = factor_evaluator.save_factor(
                    db,
                    name=req.name,
                    formula_code=req.formula_code,
                    asset_symbol=req.asset_symbol,
                    params=req.params,
                    eval_result=req.eval_result,
                    overwrite=req.overwrite,
                )
                db.flush()
                return ({"factor": row.to_dict()}, 200)
            except ValueError as exc:
                return ({"error": "conflict", "detail": str(exc)}, 409)

        outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)

    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.get("/api/factor-studio/factors")
def api_fs_list(db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core.database import Factor
    rows = db.query(Factor).order_by(Factor.updated_at.desc()).limit(_LIST_HARD_LIMIT).all()
    return {"factors": [r.to_dict() for r in rows]}


@app.get("/api/factor-studio/factors/{factor_id}")
def api_fs_one(factor_id: int, db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core.database import Factor
    row = db.get(Factor, int(factor_id))
    if row is None:
        raise HTTPException(404, f"factor {factor_id} not found")
    return row.to_dict()


@app.delete("/api/factor-studio/factors/{factor_id}", status_code=200)
def api_fs_delete(
    factor_id: int,
    request: Request,
    db: Session = Depends(get_db),
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """D-M7/P16 — idempotent factor delete."""
    from backend.core.database import Factor
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    body_payload = {"endpoint": "fs_delete", "factor_id": int(factor_id)}
    req_hash = canonical_request_hash(body_payload)

    def _compute() -> Tuple[Dict[str, Any], int]:
        row = db.get(Factor, int(factor_id))
        if row is None:
            return ({"error": "not_found", "detail": f"factor {factor_id} not found"}, 404)
        db.delete(row)
        db.flush()  # R3: atomic with the idempotency row via the single outer commit
        return ({"deleted": True, "id": int(factor_id)}, 200)

    outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)
    # get_db() never commits; lookup_or_record only flushes. Commit so the
    # idempotency row persists — otherwise a retried delete re-runs _compute,
    # finds the factor gone, and returns 404 instead of the cached 200.
    db.commit()
    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.post("/api/factor-studio/promote/{factor_id}")
def api_fs_promote(
    factor_id: int,
    request: Request,
    principal: str = Depends(require_operator),
    name_override: Optional[str] = None,
) -> Dict[str, Any]:
    """D-H8d — idempotent factor-studio promote. Cache key: factor_id."""
    from backend.core import factor_evaluator
    from backend.core.database import Factor
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    body_payload = {"endpoint": "fs_promote", "factor_id": int(factor_id), "name_override": name_override or ""}
    req_hash = canonical_request_hash(body_payload)

    with session_scope() as db:
        # Capture raw_text BEFORE compute so we have it for the background
        # pipeline regardless of replay vs. fresh-compute path.
        raw_text = ""
        try:
            fac_row = db.get(Factor, int(factor_id))
            if fac_row is not None:
                raw_text = str(fac_row.formula_code or "")
        except Exception:  # noqa: BLE001
            logger.exception("factor-studio promote: raw_text capture failed (non-fatal)")

        def _compute() -> Tuple[Dict[str, Any], int]:
            try:
                sid = factor_evaluator.promote_to_strategy(db, factor_id, name_override=name_override)
            except ValueError as exc:
                return ({"error": "not_found", "detail": str(exc)}, 404)
            return ({"strategy_id": int(sid), "status_url": f"/api/pipeline/status/{int(sid)}"}, 200)

        outcome = lookup_or_record(db, key=key, request_hash=req_hash, compute_fn=_compute)

    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)

    payload = outcome.response_payload or {}
    sid = int(payload.get("strategy_id") or 0)

    # Background pipeline ONLY on first-compute path (not replays) to avoid
    # spawning a duplicate run on retry.
    if not outcome.replay and sid > 0:
        try:
            from backend.core.orchestrator import WorkflowOrchestrator

            def _bg_run(_sid: int, _raw: str) -> None:
                try:
                    WorkflowOrchestrator().run_full_pipeline_for_id(
                        strategy_id=int(_sid),
                        raw_text=str(_raw),
                        prior_node_ids=[],
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("factor-studio promote: background pipeline crashed for sid=%s", _sid)

            threading.Thread(
                target=_bg_run,
                args=(int(sid), str(raw_text)),
                daemon=True,
                name=f"fs-promote-{sid}",
            ).start()
        except Exception:  # noqa: BLE001
            logger.exception("factor-studio promote: failed to spawn background pipeline (non-fatal)")
    return {**payload, "idempotent_replay": bool(outcome.replay)}


# --- P7-09 /alpha-genealogy -------------------------------------------------

@app.get("/api/alpha-genealogy/forest")
def api_ag_forest(db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import genealogy
    return genealogy.build_forest(db)


@app.get("/api/alpha-genealogy/tree/{strategy_id}")
def api_ag_tree(strategy_id: int, db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import genealogy
    return genealogy.get_tree(db, strategy_id)


@app.get("/api/alpha-genealogy/lineage/{strategy_id}")
def api_ag_lineage(strategy_id: int, db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import genealogy
    return genealogy.get_lineage(db, strategy_id)


# --- P7-10 /live-trade ------------------------------------------------------

@app.get("/api/live-trade/dashboard")
def api_lt_dashboard(db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import live_trade_ops
    return live_trade_ops.dashboard(db)


@app.get("/api/live-trade/exchange-ping")
def api_lt_ping(principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core.exchange_adapter import ExchangeAdapter
    ping = ExchangeAdapter().ping()
    return {"status": ping.status, "latency_ms": ping.latency_ms, "venue": ping.venue, "note": ping.note}


@app.post("/api/live-trade/pause-all")
def api_lt_pause_all(request: Request, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )
    from backend.core import live_trade_ops
    key = require_idempotency_key(request)
    body_payload = {"action": "pause_all"}
    req_hash = canonical_request_hash(body_payload)
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")

    # D-H1 — single-session pattern: _compute reuses outer `s`. Atomic commit
    # via session_scope context exit covers BOTH the pause writes + the
    # idempotency row (eliminates split-transaction race).
    with session_scope() as s:
        def _compute():
            result = live_trade_ops.pause_all(
                s,
                actor=principal,
                idempotency_key=key,
                request_ip=ip,
                user_agent=ua,
            )
            return (result, 200)

        outcome = lookup_or_record(s, key=key, request_hash=req_hash, compute_fn=_compute)
    response = outcome.response_payload
    # P34-IDEMP: escalate a recorded error outcome (consistent with every other
    # lookup_or_record call-site) so a non-200 tuple can never return as HTTP 200.
    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, response)
    return {**(response or {}), "idempotent_replay": bool(outcome.replay)}


@app.post("/api/live-trade/resume/{strategy_id}")
def api_lt_resume(strategy_id: int, request: Request, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    """P13/D-H5 — idempotent resume.

    Mirrors the pause-all pattern: resuming a strategy clears the pause flag,
    writes an audit row, and is exactly the sort of operator action that gets
    double-clicked. Wrapped in lookup_or_record.
    """
    from backend.core import live_trade_ops
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )

    key = require_idempotency_key(request)
    body_payload = {"endpoint": "resume", "strategy_id": int(strategy_id)}
    req_hash = canonical_request_hash(body_payload)
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    actor = principal

    # D-H2 — single-session pattern: _compute reuses outer `s`. Atomic commit
    # via session_scope context exit covers BOTH the resume + the idempotency
    # row (eliminates split-transaction race).
    with session_scope() as s:
        def _compute() -> Tuple[Dict[str, Any], int]:
            try:
                result = live_trade_ops.resume_one(
                    s,
                    strategy_id,
                    actor=actor,
                    request_ip=ip,
                    user_agent=ua,
                    idempotency_key=key,
                )
            except ValueError as exc:
                return ({"error": "conflict", "detail": str(exc)}, 409)
            return (result, 200)

        outcome = lookup_or_record(s, key=key, request_hash=req_hash, compute_fn=_compute)
    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    response = outcome.response_payload or {}
    return {**response, "idempotent_replay": bool(outcome.replay)}


@app.get("/api/live-trade/audit")
def api_lt_audit(limit: int = 50, db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import live_trade_ops
    return {"items": live_trade_ops.recent_audit(db, limit=limit)}


# --- P7-11 /trading-terminal ------------------------------------------------

# Tiny per-session rate limit (token bucket, in-memory).
_TT_BUCKET: Dict[str, List[float]] = {}
_TT_BUCKET_LOCK = threading.Lock()


def _trading_terminal_rate_check(request: Request) -> None:
    from backend.core import trading_terminal
    cap = trading_terminal.rate_limit_per_min()
    # P15/D-M11 — fall back to X-Forwarded-For / X-Real-IP when the deployment
    # is behind a trusted reverse proxy. X-Terminal-Session still wins so the
    # frontend's per-session bucket isn't disturbed.
    from backend._envloader import env_bool as _env_bool
    session_key = (request.headers.get("X-Terminal-Session") or "").strip()
    if session_key:
        key = session_key
    else:
        if _env_bool("BEHIND_TRUSTED_PROXY", False):
            xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            real_ip = (request.headers.get("X-Real-IP") or "").strip()
            client_ip = xff or real_ip
        else:
            client_ip = ""
        key = client_ip or (request.client.host if request.client else "unknown") or "unknown"
    # P32-TZ32-6 — monotonic clock for rate-limit window; immune to wall-clock jumps.
    now = time.monotonic()
    with _TT_BUCKET_LOCK:
        # P15/D-M22 — bound the per-process bucket dict (drop empty entries).
        for k in list(_TT_BUCKET.keys()):
            _TT_BUCKET[k] = [t for t in _TT_BUCKET[k] if now - t < 60.0]
            if not _TT_BUCKET[k]:
                _TT_BUCKET.pop(k, None)
        bucket = [t for t in _TT_BUCKET.get(key, []) if now - t < 60.0]
        if len(bucket) >= cap:
            raise HTTPException(429, "rate limited")
        bucket.append(now)
        _TT_BUCKET[key] = bucket


# P17/C-M9 — Per-session registry for the trading terminal.
#
# Problem: ``api_tt_submit`` was previously protected only by (a) the per-IP/
# session rate limit (60 req/min default) and (b) request-level idempotency
# (Idempotency-Key header). Neither prevents the following replay-attack
# pattern:
#
#   1. Operator opens the trading-terminal at t=0, browser mints
#      X-Terminal-Session=S1.
#   2. Operator closes the tab, walks away. S1 is never explicitly torn down.
#   3. Attacker (or stale tab in another window) replays a captured /submit
#      payload using S1 days later. The Idempotency-Key collides with no
#      existing row (fresh key), the rate-limit bucket is empty (no traffic
#      under S1 for days), and the request goes through.
#
# Fix: the server tracks which X-Terminal-Session ids it has SEEN going through
# the /preview endpoint within the last TTL window. /submit is only accepted
# for sessions registered via a recent /preview (the operator-facing flow
# always previews first). Stale sessions (TTL exceeded) are rejected with 410.
#
# Registry is per-process in-memory:
#   - LRU-capped at _TT_SESSIONS_MAX so a runaway client can't OOM the box.
#   - TTL of _TT_SESSIONS_TTL_SEC enforces "must have previewed recently".
#   - register=True path is /preview; register=False is /submit.
#
# Ordering inside the endpoint MUST be:
#   rate_limit → session_check → idempotency
# Session-check before idempotency is the key invariant — rejecting a stale
# session BEFORE writing an idempotency row prevents the attacker from
# poisoning the idempotency store with a now-rejected key (which would then
# replay as 410 forever).
_TT_SESSIONS: "OrderedDict[str, float]" = OrderedDict()
_TT_SESSIONS_LOCK = threading.Lock()
_TT_SESSIONS_MAX = 2048
_TT_SESSIONS_TTL_SEC = 3600.0  # 1 hour — operator's typical work window


def _trading_terminal_session_check(request: Request, *, register: bool) -> None:
    """Validate / register the X-Terminal-Session.

    ``register=True`` (used by /preview): mint or refresh the session entry,
    enforcing the LRU cap.

    ``register=False`` (used by /submit): require the session to exist AND be
    fresher than the TTL. Stale or unknown sessions raise 410 Gone — the
    frontend then forces a fresh /preview to mint a new session.
    """
    session_key = (request.headers.get("X-Terminal-Session") or "").strip()
    if not session_key:
        # P17/C-M9 (replay-guard hardening): /preview (register=True) is
        # read-only and may be called before the browser mints a session, so we
        # stay lenient there. /submit (register=False) is the order-executing,
        # replay-sensitive endpoint: a missing/blank X-Terminal-Session means
        # the client never went through /preview (or a replay deliberately
        # dropped the header to bypass the registry). The idempotency layer does
        # NOT stop a replay that ships a fresh Idempotency-Key, so we must reject
        # here. 410 mirrors the stale-session response below, telling the
        # frontend to refresh and re-preview before submitting.
        if not register:
            raise HTTPException(
                410,
                "trading-terminal session required; refresh the page and re-preview before submitting",
            )
        return
    now = time.time()
    with _TT_SESSIONS_LOCK:
        # GC stale entries opportunistically (bounded — we only walk
        # _TT_SESSIONS_MAX entries max, in insertion order).
        stale_cutoff = now - _TT_SESSIONS_TTL_SEC
        # OrderedDict iteration order is insertion order; we popitem(last=False)
        # to drop oldest while still stale. Bound the loop by len() so we never
        # spin if the clock jumps backwards.
        for _ in range(len(_TT_SESSIONS)):
            try:
                _k, _ts = next(iter(_TT_SESSIONS.items()))
            except StopIteration:
                break
            if _ts >= stale_cutoff:
                break
            _TT_SESSIONS.popitem(last=False)

        if register:
            # Refresh-or-insert. move_to_end is idempotent if the key is new
            # we add it; if it already exists we just update its timestamp
            # and reorder to mark it as most-recently-used.
            _TT_SESSIONS[session_key] = now
            _TT_SESSIONS.move_to_end(session_key, last=True)
            # LRU eviction if cap exceeded.
            while len(_TT_SESSIONS) > _TT_SESSIONS_MAX:
                _TT_SESSIONS.popitem(last=False)
            return

        # register=False: session MUST exist and MUST be within TTL.
        ts = _TT_SESSIONS.get(session_key)
        if ts is None or (now - ts) > _TT_SESSIONS_TTL_SEC:
            raise HTTPException(
                410,
                "trading-terminal session stale; refresh the page and re-preview before submitting",
            )
        # Touch the session so an active submitter doesn't expire mid-workflow.
        _TT_SESSIONS[session_key] = now
        _TT_SESSIONS.move_to_end(session_key, last=True)


@app.get("/api/trading-terminal/status")
def api_tt_status(principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import trading_terminal
    from backend._envloader import env_bool
    return {
        "enabled": trading_terminal.is_enabled(),
        "live_enabled": env_bool("LIVE_TRADE_ENABLED", False),
        "mode_default": "paper",
        "supported_symbols": [s["symbol"] for s in trading_terminal.supported_symbols()],
        "fee_bps": {"maker": trading_terminal.maker_bps(), "taker": trading_terminal.taker_bps()},
        "rate_limit_per_min": trading_terminal.rate_limit_per_min(),
        "fat_finger_pct": trading_terminal.fat_finger_pct(),
        "default_cash_usdt": trading_terminal.default_cash(),
    }


@app.get("/api/trading-terminal/symbols")
def api_tt_symbols(principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import trading_terminal
    if not trading_terminal.is_enabled():
        raise HTTPException(503, "TRADING_TERMINAL_ENABLED=0")
    return {"symbols": trading_terminal.supported_symbols()}


@app.get("/api/trading-terminal/symbols/{symbol}/market")
def api_tt_market(symbol: str, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import trading_terminal
    if not trading_terminal.is_enabled():
        raise HTTPException(503, "TRADING_TERMINAL_ENABLED=0")
    try:
        return trading_terminal.market_info(symbol.upper())
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/trading-terminal/positions")
def api_tt_positions(mode: str = "paper", db: Session = Depends(get_db), principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import trading_terminal
    if not trading_terminal.is_enabled():
        raise HTTPException(503, "TRADING_TERMINAL_ENABLED=0")
    return {"positions": trading_terminal.list_positions(db, mode=mode)}


class _TtOrderReq(_PydBaseModel):
    """Pydantic v2 ingress contract for /trading-terminal/{preview,submit}.

    P17/C-M11 — enum / numeric / required-field validation happens at the
    framework boundary so malformed payloads get a 422 with a clean per-field
    error map (FastAPI's default), rather than reaching ``validate_order``
    where they'd surface as a 200-with-errors-list (preview) or a 422 with a
    single concatenated string. Domain checks that need the live catalogue
    (fat-finger, qty step, min_qty) stay in
    ``backend.core.trading_terminal.validate_order`` — Pydantic can't see
    those.
    """

    symbol: str
    side: str
    order_type: str
    qty: float
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    tif: str = "gtc"
    mode: str = "paper"

    # ---- enums (lower-cased + whitelisted) ---------------------------------
    # Mirrors VALID_SIDES / VALID_TYPES / VALID_TIFS in trading_terminal.py.
    # Kept inline (rather than re-imported) so the ingress layer doesn't gain
    # a hard import dependency on the domain module at request-validation
    # time (helps startup speed + keeps mock-tests of the schema independent).

    @field_validator("side", mode="before")
    @classmethod
    def _v_side(cls, v: Any) -> str:
        s = str(v or "").strip().lower()
        if s not in {"buy", "sell"}:
            raise ValueError("side must be one of ['buy', 'sell']")
        return s

    @field_validator("order_type", mode="before")
    @classmethod
    def _v_order_type(cls, v: Any) -> str:
        s = str(v or "").strip().lower()
        if s not in {"market", "limit", "stop"}:
            raise ValueError("order_type must be one of ['limit', 'market', 'stop']")
        return s

    @field_validator("tif", mode="before")
    @classmethod
    def _v_tif(cls, v: Any) -> str:
        s = str(v or "gtc").strip().lower()
        if s not in {"gtc", "ioc", "fok"}:
            raise ValueError("tif must be one of ['fok', 'gtc', 'ioc']")
        return s

    @field_validator("mode", mode="before")
    @classmethod
    def _v_mode(cls, v: Any) -> str:
        s = str(v or "paper").strip().lower()
        if s not in {"paper", "live"}:
            raise ValueError("mode must be one of ['live', 'paper']")
        return s

    @field_validator("symbol", mode="before")
    @classmethod
    def _v_symbol(cls, v: Any) -> str:
        s = str(v or "").strip().upper()
        if not s:
            raise ValueError("symbol is required")
        # Catalogue membership is checked downstream in validate_order so we
        # don't import _SYMBOL_INDEX at schema time. Here we only enforce
        # shape (non-empty, upper-cased).
        return s

    # ---- numerics ----------------------------------------------------------

    @field_validator("qty", mode="before")
    @classmethod
    def _v_qty(cls, v: Any) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            raise ValueError("qty must be numeric") from exc
        if not (f > 1e-14):  # rejects 0, negatives, NaN, and IEEE 754 subnormals
            raise ValueError("qty must be a finite number > 1e-14")
        # IEEE 754 +inf passes the > 1e-14 check; explicitly forbid.
        if f == float("inf"):
            raise ValueError("qty must be a finite positive number")
        return f

    @field_validator("limit_price", mode="before")
    @classmethod
    def _v_limit_price(cls, v: Any) -> Optional[float]:
        if v is None or v == "":
            return None
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit_price must be numeric") from exc
        if not (f > 1e-14):
            raise ValueError("limit_price must be a finite number > 1e-14")
        if f == float("inf"):
            raise ValueError("limit_price must be a finite positive number")
        return f

    @field_validator("stop_price", mode="before")
    @classmethod
    def _v_stop_price(cls, v: Any) -> Optional[float]:
        if v is None or v == "":
            return None
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            raise ValueError("stop_price must be numeric") from exc
        if not (f > 1e-14):
            raise ValueError("stop_price must be a finite number > 1e-14")
        if f == float("inf"):
            raise ValueError("stop_price must be a finite positive number")
        return f

    # ---- cross-field rules -------------------------------------------------

    @model_validator(mode="after")
    def _v_cross_fields(self) -> "_TtOrderReq":
        # limit order requires limit_price; stop order requires stop_price.
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit order requires limit_price")
        if self.order_type == "stop" and self.stop_price is None:
            raise ValueError("stop order requires stop_price")
        # market orders are necessarily immediate-or-cancel — no resting book.
        # Mirrors the same check in trading_terminal.validate_order.
        if self.order_type == "market" and self.tif != "ioc":
            # Auto-normalize rather than reject — the operator UI defaults to
            # tif='gtc' on the form; coercing here is friendlier than 422.
            # validate_order downstream is now redundant for this rule but
            # remains a belt-and-braces second line.
            object.__setattr__(self, "tif", "ioc")
        return self


@app.post("/api/trading-terminal/preview")
def api_tt_preview(req: _TtOrderReq, request: Request, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import trading_terminal
    if not trading_terminal.is_enabled():
        raise HTTPException(503, "TRADING_TERMINAL_ENABLED=0")
    _trading_terminal_rate_check(request)
    # P17/C-M9 — register the session BEFORE any business logic. /preview is
    # read-only so this is safe even if validation downstream fails.
    _trading_terminal_session_check(request, register=True)
    return trading_terminal.preview(req.dict())


@app.post("/api/trading-terminal/submit")
def api_tt_submit(req: _TtOrderReq, request: Request, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import trading_terminal
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )
    if not trading_terminal.is_enabled():
        raise HTTPException(503, "TRADING_TERMINAL_ENABLED=0")
    _trading_terminal_rate_check(request)
    # P17/C-M9 — reject stale sessions BEFORE the idempotency layer so we
    # don't pollute the idempotency store with rows tied to rejected sessions.
    # Order: rate-limit → session-check → idempotency. Do not reorder.
    _trading_terminal_session_check(request, register=False)
    key = require_idempotency_key(request)
    payload = req.dict()
    req_hash = canonical_request_hash(payload)
    terminal_session = request.headers.get("X-Terminal-Session")
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")

    # D-NEW1 — single-session pattern: _compute reuses outer `s`, no inner
    # session_scope. Atomic commit via session_scope context exit covers BOTH
    # the order persist + the idempotency row.
    with session_scope() as s:
        def _compute():
            try:
                result = trading_terminal.submit_paper(
                    s,
                    payload=payload,
                    terminal_session_id=terminal_session,
                    request_ip=ip,
                    user_agent=ua,
                    idempotency_key=key,
                )
                return (result, 200)
            except NotImplementedError as exc:
                # Live path deferred — return clear error without retry.
                return ({"error": "live_routing_deferred", "detail": str(exc)}, 501)
            except ValueError as exc:
                return ({"error": "validation", "detail": str(exc)}, 422)

        outcome = lookup_or_record(s, key=key, request_hash=req_hash, compute_fn=_compute)
    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.post("/api/trading-terminal/orders/{order_uid}/cancel")
def api_tt_cancel(order_uid: str, request: Request, principal: str = Depends(require_operator)) -> Dict[str, Any]:
    from backend.core import trading_terminal
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )
    if not trading_terminal.is_enabled():
        raise HTTPException(503, "TRADING_TERMINAL_ENABLED=0")
    key = require_idempotency_key(request)
    payload = {"action": "cancel", "order_uid": order_uid}
    req_hash = canonical_request_hash(payload)
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")

    # D-NEW1 — single-session pattern: _compute reuses outer `s`, no inner
    # session_scope. Atomic commit via session_scope context exit covers BOTH
    # the cancel + the idempotency row.
    with session_scope() as s:
        def _compute():
            try:
                result = trading_terminal.cancel_order(
                    s,
                    order_uid=order_uid,
                    idempotency_key=key,
                    request_ip=ip,
                    user_agent=ua,
                )
                return (result, 200)
            except ValueError as exc:
                # P31-T3 race: the reaper filled the order between our SELECT
                # and conditional UPDATE (trading_terminal.cancel_order). This
                # is a TRANSIENT, data-state condition, not a deterministic
                # client error, so it must NOT be cached under this
                # Idempotency-Key — otherwise a legitimate retry replays this
                # 409 forever instead of seeing the true terminal state.
                # Raising HTTPException here propagates OUT of lookup_or_record
                # before any idempotency row is persisted. All other ValueErrors
                # (not-found / terminal / non-pending) are deterministic and
                # remain cacheable as a 409 below.
                detail = str(exc)
                if "no longer pending" in detail:
                    raise HTTPException(
                        status_code=409,
                        detail={"error": "cancel_race", "detail": detail},
                    )
                # cancel_order raises ValueError both for a missing order
                # (trading_terminal.py: f"order {uid} not found") and for a
                # genuine terminal-state conflict. Disambiguate so a bogus /
                # mistyped order_uid returns 404 (not a misleading 409).
                if "not found" in detail:
                    return ({"error": "not_found", "detail": detail}, 404)
                return ({"error": "validation", "detail": detail}, 409)

        outcome = lookup_or_record(s, key=key, request_hash=req_hash, compute_fn=_compute)
    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.post("/api/trading-terminal/orders/cancel-all")
def api_tt_cancel_all(
    request: Request,
    mode: str = "paper",
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    """P31-KILL — bulk kill-switch: cancel ALL pending manual orders for `mode`.

    Operationally this is the panic button a trader needs when a fast adverse
    move makes one-by-one cancellation unacceptable. It mirrors api_tt_cancel's
    ordering (rate-limit -> idempotency) and the single-session atomic-commit
    pattern, and is idempotent at two layers:
      1. The Idempotency-Key replay cache returns the SAME summary for a retried
         request without re-scanning — a double-click cannot double-process.
      2. cancel_all_open()'s per-row conditional UPDATE (WHERE status='pending')
         means a concurrent reaper fill is counted as `skipped`, never
         clobbered back to 'cancelled'.
    Deliberately NO _trading_terminal_session_check: a kill-switch must work
    even when the terminal session has gone stale (that is exactly when you
    most need it). Returns {cancelled_count, cancelled_order_uids, skipped,
    idempotent_replay}.
    """
    from backend.core import trading_terminal
    from backend.core.idempotency import (
        canonical_request_hash,
        lookup_or_record,
        require_idempotency_key,
    )
    if not trading_terminal.is_enabled():
        raise HTTPException(503, "TRADING_TERMINAL_ENABLED=0")
    _trading_terminal_rate_check(request)
    mode_norm = (mode or "paper").strip().lower()
    if mode_norm not in ("paper", "live"):
        raise HTTPException(422, {"error": "validation", "detail": "mode must be paper|live"})
    key = require_idempotency_key(request)
    payload = {"action": "cancel_all", "mode": mode_norm}
    req_hash = canonical_request_hash(payload)
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")

    # D-NEW1 — single-session pattern: _compute reuses outer `s`, no inner
    # session_scope. Atomic commit via session_scope context exit covers BOTH
    # the bulk cancel + the idempotency row.
    with session_scope() as s:
        def _compute():
            try:
                result = trading_terminal.cancel_all_open(
                    s,
                    mode=mode_norm,
                    idempotency_key=key,
                    request_ip=ip,
                    user_agent=ua,
                )
                return (result, 200)
            except ValueError as exc:
                return ({"error": "validation", "detail": str(exc)}, 422)

        outcome = lookup_or_record(s, key=key, request_hash=req_hash, compute_fn=_compute)
    if outcome.status_code >= 400:
        raise HTTPException(outcome.status_code, outcome.response_payload)
    return {**(outcome.response_payload or {}), "idempotent_replay": bool(outcome.replay)}


@app.get("/api/trading-terminal/orders")
def api_tt_orders(
    limit: int = 50,
    status: Optional[str] = None,
    symbol: Optional[str] = None,
    mode: Optional[str] = None,
    db: Session = Depends(get_db),
    principal: str = Depends(require_operator),
) -> Dict[str, Any]:
    from backend.core import trading_terminal
    if not trading_terminal.is_enabled():
        raise HTTPException(503, "TRADING_TERMINAL_ENABLED=0")
    return {"orders": trading_terminal.list_orders(db, limit=limit, status=status, symbol=symbol, mode=mode)}


# Convenience runner so `python -m backend.app.main` boots the server.
if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    # P31-D3: env_int with valid-range clamp + non-raising fallback so a
    # malformed BACKEND_PORT doesn't hard-crash uvicorn at startup.
    from backend._envloader import env_int as _env_int_main
    uvicorn.run(
        "backend.app.main:app",
        host=os.environ.get("BACKEND_HOST", "127.0.0.1"),
        port=_env_int_main("BACKEND_PORT", 8000, minimum=1, maximum=65535),
        reload=False,
    )
