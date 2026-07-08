"""Discord inbound /intake bot (P8-FIX/C-5).

Mirrors :mod:`backend.core.telegram_inbound` for Discord — operators type
``/intake <text>`` (or reply to a message with ``/intake``) in any allow-listed
channel and a new ``KnowledgeNode`` is created with ``source_type =
'discord_intake'``. Same cost / safety guards as Telegram:

- Default-OFF via ``DISCORD_INBOUND_ENABLED`` env flag.
- ``DISCORD_BOT_TOKEN`` AND ``DISCORD_ALLOWED_CHANNEL_IDS`` (comma-separated
  int channel IDs) BOTH required; missing either disables the bot.
- Per-channel rate limit (1 intake per 10 s).
- File lock at ``storage/.discord_inbound.lock`` to dedup uvicorn --reload
  double-fires.
- ``discord.py`` is imported lazily so this module is safe to import in
  environments where the dep isn't installed (tests, smoke scripts).

The bot runs as an asyncio task on the FastAPI lifespan. It is *long-running*
(maintains a Discord gateway WebSocket connection) — that's why the lock is
strict. discord.py handles reconnect/backoff internally; we only restart the
process via ``stop_inbound()`` on lifespan shutdown.
"""

from __future__ import annotations

import asyncio
import hashlib
import json as _json
import logging
import os
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Set

from backend.agents.intake import process_text_to_node
from backend.core.database import PROJECT_ROOT

logger = logging.getLogger("alpha.discord_inbound")

_LOCK_PATH: Path = PROJECT_ROOT / "storage" / ".discord_inbound.lock"

_TASK: Optional[asyncio.Task] = None
_CLIENT: Optional[Any] = None  # typed as discord.Client at runtime
_STOP = asyncio.Event()

_RATE_BUCKET: Dict[int, float] = {}
# P13/D-L4 — Lock makes the read+modify+write of the bucket atomic so two
# rapid /intake commands on the same channel can't both pass the check.
_RATE_BUCKET_LOCK = threading.Lock()
_RATE_INTERVAL_SECONDS: float = 10.0

# Message-level replay/idempotency guard. discord.py transparently RESUMEs the
# gateway on transient disconnects (see module docstring); on a RESUME the
# gateway REPLAYS buffered MESSAGE_CREATE events to on_message. Without a
# message-id dedup, a redelivered message re-runs process_text_to_node, whose
# billable LLM call (call_messages) fires BEFORE the content_hash dedup — i.e.
# duplicate paid spend on every redelivery. We keep a bounded set of recently
# seen message.id values (checked + stamped under _RATE_BUCKET_LOCK, the same
# lock used by the rate-limit bucket) and silently drop duplicates. This is the
# in-process equivalent of Telegram's persisted update-offset dedup. NOTE: it
# does NOT survive a process restart (no source_ref/idempotency column exists on
# KnowledgeNode to key on); that residual gap is acceptable for this risk class.
_STATE_PATH: Path = PROJECT_ROOT / "storage" / "discord_inbound_state.json"
_SEEN_MESSAGE_IDS: "OrderedDict[int, None]" = OrderedDict()
_SEEN_MESSAGE_MAX: int = 512


def _load_seen_ids() -> None:
    """Seed _SEEN_MESSAGE_IDS from persisted state on startup."""
    if not _STATE_PATH.exists():
        return
    try:
        data = _json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        ids = data.get("seen_message_ids", [])
        with _RATE_BUCKET_LOCK:
            for mid in ids[-_SEEN_MESSAGE_MAX:]:
                _SEEN_MESSAGE_IDS[int(mid)] = None
    except Exception:  # noqa: BLE001
        logger.exception("Failed to load discord_inbound_state.json — starting fresh")


def _persist_seen_ids() -> None:
    """Persist current _SEEN_MESSAGE_IDS keys to disk (best-effort, non-blocking)."""
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Snapshot under lock to avoid holding lock during I/O
        with _RATE_BUCKET_LOCK:
            ids = list(_SEEN_MESSAGE_IDS.keys())
        _STATE_PATH.write_text(
            _json.dumps({"seen_message_ids": ids}, indent=2),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to persist discord_inbound_state.json")


def _seen_and_stamp(message_id: int) -> bool:
    """Return True if ``message_id`` was already processed (caller must drop it).

    Atomically records new ids under ``_RATE_BUCKET_LOCK``. id<=0 is treated as
    "never seen" (we can't dedup an unknown id, so we let it through rather than
    collapse all unknown-id messages into one). Persists state to disk on each
    new stamp so dedup survives a process restart.
    """
    if message_id <= 0:
        return False
    with _RATE_BUCKET_LOCK:
        if message_id in _SEEN_MESSAGE_IDS:
            _SEEN_MESSAGE_IDS.move_to_end(message_id)
            return True
        _SEEN_MESSAGE_IDS[message_id] = None
        while len(_SEEN_MESSAGE_IDS) > _SEEN_MESSAGE_MAX:
            _SEEN_MESSAGE_IDS.popitem(last=False)
    _persist_seen_ids()
    return False

# Recent inbound events for /api/discord/status. Bounded ring buffer.
_RECENT_MAX: int = 50
_RECENT_EVENTS: Deque[Dict[str, Any]] = deque(maxlen=_RECENT_MAX)
_LAST_EVENT_AT: Optional[str] = None


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


# D-M6 — delegate to the canonical strict-whitelist env_bool from _envloader.
# P15/D-H1 — placeholder filter now lives in _envloader.env_secret_or_none.
from backend._envloader import env_bool as _env_bool, env_secret_or_none


def _token() -> Optional[str]:
    return env_secret_or_none("DISCORD_BOT_TOKEN")


def _allowed_channel_ids() -> Set[int]:
    raw = (os.environ.get("DISCORD_ALLOWED_CHANNEL_IDS") or "").strip()
    out: Set[int] = set()
    for part in raw.replace(";", ",").split(","):
        s = part.strip()
        if not s:
            continue
        try:
            out.add(int(s))
        except ValueError:
            logger.warning("Skipping invalid Discord channel id: %r", s)
    return out


def is_inbound_enabled() -> bool:
    """Master switch for the inbound Discord bot."""
    if not _env_bool("DISCORD_INBOUND_ENABLED", False):
        return False
    if not _token():
        return False
    if not _allowed_channel_ids():
        logger.warning(
            "DISCORD_INBOUND_ENABLED=1 but no DISCORD_ALLOWED_CHANNEL_IDS allowlist set. "
            "Inbound disabled."
        )
        return False
    return True


def allowed_channel_count() -> int:
    return len(_allowed_channel_ids())


# ---------------------------------------------------------------------------
# File lock — dedup uvicorn --reload double-fire
# ---------------------------------------------------------------------------


def _acquire_lock() -> bool:
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    # P15/D-M14 — bounded retry instead of recursion: at most 2 steal attempts
    # so a pathological "two processes race-stealing the same stale lock"
    # scenario can't blow the stack.
    for attempt in range(2):
        try:
            fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - _LOCK_PATH.stat().st_mtime
                if age > 300 and attempt == 0:
                    _LOCK_PATH.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            return False
        except OSError as exc:
            logger.warning("Could not acquire discord lock: %s", exc)
            return True
    return False


def _release_lock() -> None:
    try:
        _LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def is_running() -> bool:
    return bool(_TASK and not _TASK.done())


def last_event_at() -> Optional[str]:
    return _LAST_EVENT_AT


def recent_events(*, limit: int = 20) -> list:
    capped = max(1, min(_RECENT_MAX, int(limit or 20)))
    return list(_RECENT_EVENTS)[-capped:][::-1]


def _redact_channel(channel_id: Any) -> str:
    """Non-reversible 8-hex token for a Discord channel id.

    The raw id is an operational identifier (mirrors the Telegram
    'never leak chat IDs' invariant) — never put it in a payload that
    /api/discord/status returns to unauthenticated callers.
    """
    try:
        cid = int(channel_id or 0)
    except (TypeError, ValueError):
        cid = 0
    return hashlib.sha256(str(cid).encode("ascii")).hexdigest()[:8]


def _record_event(kind: str, **payload: Any) -> None:
    global _LAST_EVENT_AT
    _LAST_EVENT_AT = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # P-SEC: redact operational identifiers before they reach the
    # unauthenticated /api/discord/status surface. Raw channel ids become a
    # non-reversible token; the bot account string ('user') is dropped.
    safe: Dict[str, Any] = {}
    for key, value in payload.items():
        if key == "channel":
            safe["channel"] = _redact_channel(value)
        elif key == "user":
            continue  # drop bot account name entirely
        else:
            safe[key] = value
    _RECENT_EVENTS.append({"ts": _LAST_EVENT_AT, "kind": kind, **safe})


async def start_inbound() -> None:
    """Idempotent — safe to call from lifespan startup."""
    global _TASK, _CLIENT
    if not is_inbound_enabled():
        logger.info(
            "Discord inbound not started (DISCORD_INBOUND_ENABLED is off or required env vars missing)"
        )
        return
    if _TASK and not _TASK.done():
        return
    if not _acquire_lock():
        logger.info("Discord inbound already running in another process (lock present)")
        return

    # Lazy import so the absence of discord.py never crashes the FastAPI boot.
    try:
        import discord  # type: ignore
    except Exception as exc:  # noqa: BLE001
        logger.warning("discord.py import failed (%s) — bot will not start", exc)
        _release_lock()
        return

    intents = discord.Intents.default()
    # Message Content is a *privileged* intent — operator must toggle it on
    # in the Discord Developer Portal for the bot's application. Without it
    # message.content is always an empty string.
    intents.message_content = True
    intents.messages = True

    client = discord.Client(intents=intents)
    _CLIENT = client
    _STOP.clear()
    allowed_ids = _allowed_channel_ids()

    @client.event
    async def on_ready() -> None:  # noqa: D401
        logger.info(
            "Discord inbound connected as %s (allowed channels=%s)",
            getattr(client.user, "name", "?"),
            sorted(allowed_ids),
        )
        _record_event("connected", user=str(client.user))

    @client.event
    async def on_message(message: Any) -> None:  # noqa: D401
        try:
            if message.author and getattr(message.author, "bot", False):
                return  # ignore bot echoes (including our own)
            # Replay/idempotency guard — drop gateway RESUME redeliveries and any
            # duplicate MESSAGE_CREATE BEFORE any billable intake work.
            if _seen_and_stamp(int(getattr(message, "id", 0) or 0)):
                return
            channel_id = int(getattr(message.channel, "id", 0) or 0)
            if channel_id == 0 or channel_id not in allowed_ids:
                return
            text = str(getattr(message, "content", "") or "").strip()
            if not text:
                return
            if text.startswith("/help"):
                # P34: rate-limit /help too (parity with the /intake path and the
                # Telegram /help handler) so a flood can't spam the channel.
                if not _rate_limit_check(channel_id):
                    return
                await message.channel.send(
                    "**Agentic Alpha Inbound Bot**\n"
                    "Send `/intake <your market commentary>` to create a new "
                    "KnowledgeNode. Reply to one of your own messages with "
                    "`/intake` to use that message's text."
                )
                _record_event("help", channel=channel_id)
                return
            if not text.startswith("/intake"):
                return
            raw_text = text[len("/intake"):].strip()
            if not raw_text and getattr(message, "reference", None) is not None:
                try:
                    referenced = await message.channel.fetch_message(
                        message.reference.message_id
                    )
                    raw_text = str(getattr(referenced, "content", "") or "").strip()
                except Exception:  # noqa: BLE001
                    raw_text = ""
            if not raw_text:
                # P34 parity: the usage reply is an outbound channel.send, so
                # gate it on the same per-channel bucket as /help and silently
                # drop when over the limit (replying would itself be the flood).
                if not _rate_limit_check(channel_id):
                    return
                await message.channel.send(
                    "Usage: `/intake <text>` or reply to a message with `/intake`."
                )
                return
            if not _rate_limit_check(channel_id):
                await message.channel.send(
                    "Rate limited. Please wait before sending another /intake."
                )
                return
            try:
                node = await asyncio.to_thread(
                    lambda: process_text_to_node(raw_text, entry_point="discord")
                )
            except ValueError as exc:
                await message.channel.send(f"Error: {exc}")
                _record_event("error", channel=channel_id, error=str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("discord intake failed for channel %s", channel_id)
                await message.channel.send(
                    f"Error: {type(exc).__name__}: {str(exc)[:200]}"
                )
                _record_event(
                    "error", channel=channel_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return
            title = str(node.get("title") or "Untitled")[:120]
            nid = node.get("id")
            await message.channel.send(f"Created **KnowledgeNode #{nid}**: `{title}`")
            _record_event("intake", channel=channel_id, node_id=int(nid or 0), title=title)
            # P11-B-14: optional auto-pipeline trigger from discord /intake.
            # Default OFF so existing deployments aren't surprised by extra
            # LLM spend; opt in via AUTO_PIPELINE_FROM_INTAKE_BOTS=1.
            if _env_bool("AUTO_PIPELINE_FROM_INTAKE_BOTS", False) and nid:
                try:
                    from backend.core.auto_pipeline import maybe_trigger_pipeline_for_nodes
                    await asyncio.to_thread(
                        lambda: maybe_trigger_pipeline_for_nodes(0, [int(nid)]),
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("auto-pipeline trigger from discord bot failed (non-fatal)")
        except Exception:  # noqa: BLE001
            logger.exception("Discord on_message handler crashed")

    tok = _token()
    if not tok:
        _release_lock()
        return

    async def _runner() -> None:
        try:
            await client.start(tok)
        except asyncio.CancelledError:
            await client.close()
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Discord client crashed")
            _record_event("crashed")
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    _load_seen_ids()
    _TASK = asyncio.create_task(_runner(), name="alpha-discord-inbound")


async def stop_inbound() -> None:
    global _TASK, _CLIENT
    _STOP.set()
    if _CLIENT is not None:
        try:
            await _CLIENT.close()
        except Exception:  # noqa: BLE001
            pass
    if _TASK and not _TASK.done():
        _TASK.cancel()
        try:
            await asyncio.wait_for(_TASK, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    _TASK = None
    _CLIENT = None
    _release_lock()


def _rate_limit_check(channel_id: int) -> bool:
    # P13/D-L4 — full check-and-stamp under the lock so concurrent inbound
    # /intake commands cannot both observe an old `last` and bypass the limit.
    with _RATE_BUCKET_LOCK:
            # P31-D7: opportunistic GC — drop entries older than 1h so a
            # large bot in a many-chat deployment can't grow the dict
            # unbounded.
        now = time.monotonic()
        cutoff_ts = now - 3600.0
        for stale_id in [k for k, v in _RATE_BUCKET.items() if v < cutoff_ts]:
            _RATE_BUCKET.pop(stale_id, None)
        last = _RATE_BUCKET.get(channel_id, 0.0)
        if (now - last) < _RATE_INTERVAL_SECONDS:
            return False
        _RATE_BUCKET[channel_id] = now
        return True


__all__ = [
    "start_inbound",
    "stop_inbound",
    "is_inbound_enabled",
    "is_running",
    "allowed_channel_count",
    "last_event_at",
    "recent_events",
]
