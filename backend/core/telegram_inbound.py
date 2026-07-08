"""Telegram inbound /intake bot (P5-BE-06).

Implements the reference demo's "type ``/intake <text>`` in Telegram, watch a
KnowledgeNode appear" flow. Long-poll-based (no public HTTPS required for
webhooks), runs as a single asyncio task on the FastAPI lifespan, idempotent
across uvicorn --reload double-fires via a file lock.

Cost / safety guards (validator-mandated):
- Default-OFF via the SEPARATE env ``TELEGRAM_INBOUND_ENABLED``. Sharing the
  outbound ``TELEGRAM_ENABLED`` flag would let anyone-with-the-bot trigger
  LLM intake calls on a deployment that only wanted notifications.
- A bearer token (``TELEGRAM_BOT_TOKEN``) AND a chat-id allowlist
  (``TELEGRAM_ALLOWED_CHAT_IDS`` comma-separated; falls back to
  ``TELEGRAM_CHAT_ID`` if unset) are both required. Messages from any other
  chat are dropped with no reply.
- Per-chat-id rate limit: at most one ``/intake`` per 10 s (in-memory).
- Telegram update IDs are persisted to disk so a restart never re-processes
  a message.
- File lock at ``storage/.telegram_inbound.lock`` prevents the second uvicorn
  worker from starting a duplicate poll loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional, Set

import httpx

from backend.agents.intake import process_text_to_node
from backend.core.database import PROJECT_ROOT

logger = logging.getLogger("alpha.telegram_inbound")

_TG_API_BASE = "https://api.telegram.org"
_STATE_PATH: Path = PROJECT_ROOT / "storage" / "telegram_inbound_state.json"
_LOCK_PATH: Path = PROJECT_ROOT / "storage" / ".telegram_inbound.lock"

_TASK: Optional[asyncio.Task] = None
_STOP = asyncio.Event()

# Per-chat-id rate limit. {chat_id: last_intake_monotonic}
_RATE_BUCKET: dict[int, float] = {}
# P13/D-L4 — Lock makes the read+modify+write of the bucket atomic so two
# rapid /intake commands in the same chat can't both pass the check.
_RATE_BUCKET_LOCK = threading.Lock()
_RATE_INTERVAL_SECONDS: float = 10.0

# Long-poll timeout — Telegram allows up to 50 s; 30 is the safe sweet spot.
_LONG_POLL_TIMEOUT: int = 30


# ---------------------------------------------------------------------------
# Env helpers (mirror scheduler.py for consistency)
# ---------------------------------------------------------------------------


# D-M6 — delegate to the canonical strict-whitelist env_bool from _envloader.
# P15/D-H1 — placeholder filter now lives in _envloader.env_secret_or_none.
from backend._envloader import env_bool as _env_bool, env_secret_or_none, env_str_set


def _token() -> Optional[str]:
    # P12-B-L3 — placeholder filter mirrors Discord's _token() so that obvious
    # template values ("your_xxx", "xxxx", "placeholder", "changeme") never look
    # like a real bot token to the Telegram poller. Prevents 401-loops against
    # api.telegram.org when the operator only filled in the .env.example.
    return env_secret_or_none("TELEGRAM_BOT_TOKEN")


def _allowed_chat_ids() -> Set[int]:
    raw = (os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip()
    fallback = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not raw and fallback:
        raw = fallback
    out: Set[int] = set()
    for part in raw.replace(";", ",").split(","):
        s = part.strip()
        if not s:
            continue
        try:
            out.add(int(s))
        except ValueError:
            logger.warning("Skipping invalid chat id in allowlist: %r", s)
    return out


def _allowed_user_ids() -> Set[int]:
    # Optional per-sender allowlist. Unset -> empty set -> no sender gate
    # (back-compat: authorization stays chat-id-only). When set, the
    # message sender's numeric Telegram id (msg["from"]["id"]) MUST be a
    # member. Recommended whenever TELEGRAM_ALLOWED_CHAT_IDS points at a
    # GROUP/SUPERGROUP/CHANNEL, since otherwise every group member can
    # trigger billable /intake calls and quote arbitrary messages.
    out: Set[int] = set()
    for s in env_str_set("TELEGRAM_ALLOWED_USER_IDS"):
        try:
            out.add(int(s))
        except ValueError:
            logger.warning("Skipping invalid user id in allowlist: %r", s)
    return out


def is_inbound_enabled() -> bool:
    """Master switch for the inbound bot."""
    if not _env_bool("TELEGRAM_INBOUND_ENABLED", False):
        return False
    if not _token():
        return False
    if not _allowed_chat_ids():
        logger.warning(
            "TELEGRAM_INBOUND_ENABLED=1 but no allowlist set "
            "(TELEGRAM_ALLOWED_CHAT_IDS or TELEGRAM_CHAT_ID). Inbound disabled."
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Persistent state (last update id) + file lock
# ---------------------------------------------------------------------------


def _read_last_update_id() -> int:
    if not _STATE_PATH.exists():
        return 0
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return int(data.get("last_update_id", 0))
    except Exception:  # noqa: BLE001
        return 0


def _write_last_update_id(uid: int) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        _STATE_PATH.write_text(
            json.dumps({"last_update_id": int(uid)}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("Failed to persist last_update_id")


def _acquire_lock() -> bool:
    """Best-effort file lock to dedup uvicorn --reload double-fires.

    Returns True if we acquired the lock (or the lock dir is missing and we
    can't enforce it). The lock is reset on process exit via
    ``_release_lock``; stale locks from crashed processes are tolerated
    because Telegram offset-based dedup also prevents duplicate processing.
    """
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    # P15/D-M14 — bounded retry instead of recursion: at most 2 steal attempts
    # so a pathological "two processes race-stealing the same stale lock"
    # scenario can't blow the stack.
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    for attempt in range(2):
        try:
            # O_CREAT | O_EXCL — atomic create-or-fail.
            fd = os.open(str(_LOCK_PATH), flags)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return True
        except FileExistsError:
            # Stale lock from a crashed previous run? Check age — if it's older
            # than 5 minutes we steal it on the first attempt only.
            try:
                age = time.time() - _LOCK_PATH.stat().st_mtime
                if age > 300 and attempt == 0:
                    _LOCK_PATH.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            return False
        except OSError as exc:
            logger.warning("Could not acquire telegram lock: %s", exc)
            return True  # don't block startup over a filesystem hiccup
    return False


def _release_lock() -> None:
    try:
        _LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def start_inbound() -> None:
    """Idempotent — safe to call multiple times in lifespan setup."""
    global _TASK
    if not is_inbound_enabled():
        logger.info(
            "Telegram inbound not started (TELEGRAM_INBOUND_ENABLED is off "
            "or required env vars missing)"
        )
        return
    if _TASK and not _TASK.done():
        return
    if not _acquire_lock():
        logger.info(
            "Telegram inbound already running in another process (lock present)"
        )
        return
    _STOP.clear()
    _TASK = asyncio.create_task(_poll_loop(), name="alpha-telegram-inbound")
    allowed = sorted(_allowed_chat_ids())
    logger.info(
        "Telegram inbound bot started (allowed chats=%s, last_update_id=%s)",
        allowed,
        _read_last_update_id(),
    )


async def stop_inbound() -> None:
    """Cooperative shutdown of the long-poll loop.

    P15/D-M18 NOTE: A long-poll cycle held by ``getUpdates`` (timeout up to
    30s) may swallow the cancel and finish naturally before re-checking
    ``_STOP``. We wait up to 5s for clean exit, then ``.cancel()`` the task
    hard. Any unread messages stay queued on Telegram's server until the next
    ``start_inbound()`` call thanks to the offset persistence in
    ``_write_last_update_id``.
    """
    _STOP.set()
    if _TASK and not _TASK.done():
        try:
            await asyncio.wait_for(_TASK, timeout=5.0)
        except asyncio.TimeoutError:
            _TASK.cancel()
    _release_lock()


def is_running() -> bool:
    return bool(_TASK and not _TASK.done())


# ---------------------------------------------------------------------------
# Core poll loop
# ---------------------------------------------------------------------------


async def _poll_loop() -> None:
    last = _read_last_update_id()
    backoff = 1.0
    allowed_chat_ids = _allowed_chat_ids()
    if not allowed_chat_ids:
        logger.warning(
            "TELEGRAM_ALLOWED_CHAT_IDS resolved to empty set at poll loop start; "
            "all messages will be dropped. Set TELEGRAM_ALLOWED_CHAT_IDS or "
            "TELEGRAM_CHAT_ID to enable inbound."
        )
    while not _STOP.is_set():
        # Re-read token each iteration so a token rotation takes effect without
        # requiring a process restart. Overhead is negligible (~one os.environ.get
        # per 30-second long-poll cycle).
        tok = _token()
        if not tok:
            logger.warning("TELEGRAM_BOT_TOKEN cleared; stopping inbound loop")
            return
        try:
            updates = await _get_updates(tok, offset=last + 1)
            backoff = 1.0
        except Exception:  # noqa: BLE001
            logger.exception("getUpdates crashed; backing off %.1fs", backoff)
            try:
                await asyncio.wait_for(_STOP.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(60.0, backoff * 2.0)
            continue

        for upd in updates:
            # P-TG-IDEMPOTENCY: advance + persist the offset BEFORE the
            # side-effecting handler. Telegram getUpdates offset semantics treat
            # offset=last+1 as an acknowledgement of every update_id <= last, so
            # persisting first means a crash/redeploy drops at most the single
            # in-flight message rather than RE-running the billable intake LLM
            # call and creating a duplicate KnowledgeNode (the temperature=0.2
            # intake regenerates different text -> different content_hash -> the
            # content-hash dedup does NOT catch the replay). On handler failure
            # the operator already receives an error reply from _handle_update
            # and can re-send /intake.
            uid = int(upd.get("update_id") or 0)
            if uid > last:
                last = uid
                _write_last_update_id(last)
            try:
                await _handle_update(tok, upd)
            except Exception:
                logger.exception("Failed to handle update %s", upd.get("update_id"))

        # Cooperative cancel between long-poll rounds.
        if _STOP.is_set():
            return


async def _get_updates(token: str, *, offset: int) -> list:
    url = f"{_TG_API_BASE}/bot{token}/getUpdates"
    params = {
        "offset": offset,
        "timeout": _LONG_POLL_TIMEOUT,
        "allowed_updates": json.dumps(["message"]),
    }
    timeout = httpx.Timeout(_LONG_POLL_TIMEOUT + 10, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as cli:
        resp = await cli.get(url, params=params)
    if resp.status_code >= 400:
        raise RuntimeError(f"getUpdates HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates not ok: {data.get('description')}")
    return list(data.get("result") or [])


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------


async def _send_reply(token: str, chat_id: int, text: str) -> None:
    url = f"{_TG_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as cli:
            resp = await cli.post(url, json=payload)
        if resp.status_code >= 400:
            logger.warning(
                "sendMessage HTTP %s for chat %s: %s",
                resp.status_code,
                chat_id,
                resp.text[:200],
            )
    except Exception:
        logger.exception("Failed to send reply to chat %s", chat_id)


def _rate_limit_check(chat_id: int) -> bool:
    """Return True if the chat is allowed to send another /intake right now."""
    # P13/D-L4 — full check-and-stamp under the lock.
    with _RATE_BUCKET_LOCK:
            # P31-D7: opportunistic GC — drop entries older than 1h so a
            # large bot in a many-chat deployment can't grow the dict
            # unbounded.
        now = time.monotonic()
        cutoff_ts = now - 3600.0
        for stale_id in [k for k, v in _RATE_BUCKET.items() if v < cutoff_ts]:
            _RATE_BUCKET.pop(stale_id, None)
        last = _RATE_BUCKET.get(chat_id, 0.0)
        if (now - last) < _RATE_INTERVAL_SECONDS:
            return False
        _RATE_BUCKET[chat_id] = now
        return True


async def _handle_update(token: str, update: dict) -> None:
    msg = update.get("message") or {}
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = int(chat.get("id") or 0)
    if chat_id == 0 or chat_id not in _allowed_chat_ids():
        # Silent drop — don't expose our existence to random users.
        return
    # P-TG-AUTH — optional per-sender allowlist. When TELEGRAM_ALLOWED_USER_IDS
    # is set (recommended for GROUP chat ids), the sender's numeric id must be
    # a member; otherwise silent drop. Unset -> no sender gate (back-compat).
    allowed_users = _allowed_user_ids()
    if allowed_users:
        sender_id = int((msg.get("from") or {}).get("id") or 0)
        if sender_id == 0 or sender_id not in allowed_users:
            return

    text = str(msg.get("text") or "").strip()
    if not text:
        return

    if text.startswith("/help"):
        # P-TG-RL: throttle /help on the same per-chat bucket as /intake so an
        # allow-listed-but-compromised chat can't spam /help into unbounded
        # outbound sendMessage calls. Silently drop when over the limit (no
        # reply at all — replying would itself be an outbound message).
        if not _rate_limit_check(chat_id):
            return
        await _send_reply(
            token,
            chat_id,
            "*Agentic Alpha Inbound Bot*\n\n"
            "Send `/intake <your market commentary>` to create a new "
            "KnowledgeNode. The reply will include its assigned ID."
            "\n\nReplying to one of your own messages with `/intake` will use "
            "that message's text as the input.",
        )
        return

    if not text.startswith("/intake"):
        # Unknown command — ignore (no reply) per cost-guard guidance.
        return

    raw_text = text[len("/intake"):].strip()
    if not raw_text:
        # Try the message-being-replied-to as the source of input.
        reply = msg.get("reply_to_message") or {}
        raw_text = str(reply.get("text") or "").strip()
    if not raw_text:
        # P-TG-RL parity: the usage reply is itself an outbound sendMessage, so
        # gate it on the same per-chat bucket as /help and silently drop when
        # over the limit (no reply at all — replying would be the flood).
        if not _rate_limit_check(chat_id):
            return
        await _send_reply(
            token,
            chat_id,
            "Usage: `/intake <text>` or reply to a message with `/intake`.",
        )
        return

    if not _rate_limit_check(chat_id):
        await _send_reply(
            token,
            chat_id,
            f"Rate-limited (1 intake / {int(_RATE_INTERVAL_SECONDS)}s). Try again in a moment.",
        )
        return

    # process_text_to_node opens its own DB session.
    # P8-FIX/H-17: stamp provenance so /api/telegram/recent-intakes can find it.
    # R5/SRE-005: bound the LLM-backed intake with a timeout so a provider outage
    # cannot wedge the single telegram poll task forever (no reply, no log).
    _intake_timeout = float(os.environ.get("TELEGRAM_INTAKE_TIMEOUT_S", "120") or "120")
    try:
        node = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: process_text_to_node(raw_text, entry_point="telegram"),
            ),
            timeout=_intake_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Telegram intake timed out after %.0fs for chat %s", _intake_timeout, chat_id
        )
        await _send_reply(token, chat_id, "Request timed out; please try again.")
        return
    except ValueError as exc:
        logger.warning("intake ValueError via telegram for chat %s: %s", chat_id, exc)
        await _send_reply(token, chat_id, f"Error: {type(exc).__name__}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("intake failed via telegram for chat %s", chat_id)
        await _send_reply(token, chat_id, f"Error: {type(exc).__name__}: {str(exc)[:200]}")
        return

    title = str(node.get("title") or "Untitled")[:120]
    nid = node.get("id")
    await _send_reply(
        token,
        chat_id,
        f"Created KnowledgeNode *#{nid}*: `{title}`",
    )

    # P11-B-14: optional auto-pipeline trigger from telegram /intake. Default OFF
    # so existing deployments aren't surprised by extra LLM spend; opt in via
    # AUTO_PIPELINE_FROM_INTAKE_BOTS=1. source_id=0 means "bot intake" — the
    # auto_pipeline module handles that case without an IngestSource row.
    if _env_bool("AUTO_PIPELINE_FROM_INTAKE_BOTS", False) and nid:
        try:
            from backend.core.auto_pipeline import maybe_trigger_pipeline_for_nodes
            await asyncio.to_thread(
                lambda: maybe_trigger_pipeline_for_nodes(0, [int(nid)]),
            )
        except Exception:  # noqa: BLE001
            logger.exception("auto-pipeline trigger from telegram bot failed (non-fatal)")


__all__ = [
    "start_inbound",
    "stop_inbound",
    "is_running",
    "is_inbound_enabled",
]
