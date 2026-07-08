"""Telegram outbound notifier (P3c).

Implements the Conor-style 6h report / Morning Briefing / Paper Trade Health
push messages from the reference demo. Outbound-only — no bot framework is
needed; we just POST to the Bot API via httpx.

Default-OFF. Requires both:
  - TELEGRAM_BOT_TOKEN   (from @BotFather)
  - TELEGRAM_CHAT_ID     (e.g. -100123456789 for a channel)

Rate-limited at 1 message per ~1.2 seconds globally (token-bucket) so this
module can be called from the orchestrator without DoSing the Telegram API.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

import httpx

logger = logging.getLogger("alpha.telegram")

_TG_API_BASE = "https://api.telegram.org"
_GLOBAL_LOCK = threading.Lock()
_LAST_SEND_TS = 0.0
_MIN_INTERVAL_SECONDS = 1.2  # Telegram global limit is 30 msg/sec; we play safe


# D-M7 — delegate to the canonical strict-whitelist env_bool from _envloader
# so typo'd values ("treu", "Y") collapse to default (False) instead of
# accidentally evaluating True via "any non-empty string".
# P15/D-H1 — placeholder filter also lives in _envloader (env_secret_or_none).
from backend._envloader import env_bool as _env_bool, env_secret_or_none


def _token() -> Optional[str]:
    """Resolve outbound Telegram bot token, rejecting placeholder values."""
    return env_secret_or_none("TELEGRAM_BOT_TOKEN")


def _chat_id() -> Optional[str]:
    """Resolve outbound Telegram chat id, rejecting placeholder values."""
    return env_secret_or_none("TELEGRAM_CHAT_ID")


def is_enabled() -> bool:
    """Telegram notifier is opt-in (off by default, no tokens by default)."""
    if not _env_bool("TELEGRAM_ENABLED", False):
        return False
    return bool(_token() and _chat_id())


def _rate_limit() -> None:
    global _LAST_SEND_TS
    with _GLOBAL_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_SEND_TS
        wait = max(0.0, _MIN_INTERVAL_SECONDS - elapsed)
        # Stamp the intended next-send time under the lock so a second thread
        # that enters while we are sleeping outside the lock will see the
        # already-reserved slot and compute its own wait correctly.
        _LAST_SEND_TS = now + wait
    # Sleep OUTSIDE the lock so other threads can enter and compute their own
    # wait without blocking on our sleep.
    if wait > 0.0:
        time.sleep(wait)


def send_markdown(text: str, *, disable_notification: bool = False) -> bool:
    """Send a markdown message. Returns True on success, False on any failure.

    NEVER raises — Telegram outages must not crash the pipeline.
    """
    if not is_enabled():
        return False
    tok = _token()
    cid = _chat_id()
    if not tok or not cid:
        return False
    try:
        _rate_limit()
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0)) as cli:
            resp = cli.post(
                f"{_TG_API_BASE}/bot{tok}/sendMessage",
                json={
                    "chat_id": cid,
                    "text": text[:4000],  # Telegram caps at 4096; leave headroom.
                    "parse_mode": "Markdown",
                    "disable_notification": bool(disable_notification),
                    "disable_web_page_preview": True,
                },
            )
        if resp.status_code >= 400:
            logger.warning("Telegram send failed: %s %s", resp.status_code, resp.text[:200])
            return False
        return True
    except httpx.RequestError as exc:
        logger.warning("Telegram network error: %s", exc)
        return False
    except Exception:
        logger.exception("Telegram send crashed")
        return False


# ---------------------------------------------------------------------------
# Higher-level message templates — match the reference demo's report style
# ---------------------------------------------------------------------------


def notify_strategy_transition(
    strategy_id: int,
    *,
    name: str,
    from_status: str,
    to_status: str,
    metrics: Optional[dict] = None,
) -> bool:
    """Posted on every orchestrator state transition (when enabled)."""
    bits = [
        f"*S#{strategy_id}* `{name[:64]}`",
        f"`{from_status}` → `{to_status}`",
    ]
    if metrics:
        s = metrics.get("annualized_sharpe")
        d = metrics.get("max_drawdown")
        if s is not None:
            bits.append(f"Sharpe: `{float(s):.2f}`")
        if d is not None:
            bits.append(f"MaxDD: `{float(d) * 100:.1f}%`")
    return send_markdown(" · ".join(bits), disable_notification=True)


def notify_pipeline_error(strategy_id: int, stage: str, error: str) -> bool:
    safe_stage = str(stage)[:120].replace("`", "'")
    msg = (
        f"⚠️ *Pipeline error* — S#{strategy_id} at `{safe_stage}`\n"
        f"`{error[:480]}`"
    )
    return send_markdown(msg)


__all__ = [
    "is_enabled",
    "send_markdown",
    "notify_strategy_transition",
    "notify_pipeline_error",
]
