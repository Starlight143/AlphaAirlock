"""Discord outbound notifier (P-DISCORD-OUT).

Outbound mirror of :mod:`backend.core.telegram_notifier` for Discord. Posts
pipeline stage transitions, pipeline errors, and knowledge intakes to a Discord
channel via an incoming WEBHOOK — no gateway / bot connection is needed for
outbound (the inbound ``/intake`` bot lives separately in
``discord_inbound.py``; this module is outbound-only and never imports it).

Default-OFF. Requires BOTH:
  - ``DISCORD_OUTBOUND_ENABLED=1``
  - ``DISCORD_WEBHOOK_URL``  (Server Settings → Integrations → Webhooks → New
                              Webhook → Copy Webhook URL)

Optional:
  - ``DISCORD_OUTBOUND_USERNAME``  display-name override for webhook messages.

Safety:
  - The webhook URL is validated to be a genuine ``discord.com`` webhook
    endpoint before any POST, so a typo can never exfiltrate the message to an
    arbitrary host.
  - Rate-limited to ~1 message/sec globally (token bucket) so an
    orchestrator-driven burst never trips Discord's per-webhook 429s.
  - ``send_markdown`` NEVER raises — a Discord outage must not crash the
    pipeline. When the notifier is disabled it returns immediately (no network,
    no sleep), so the default configuration adds zero latency.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

import httpx

logger = logging.getLogger("alpha.discord_out")

_GLOBAL_LOCK = threading.Lock()
_LAST_SEND_TS = 0.0
_MIN_INTERVAL_SECONDS = 1.0  # Discord webhooks allow ~30/min; stay well under.
_MAX_CONTENT = 1900          # Discord hard-caps webhook `content` at 2000 chars.

# Canonical strict-whitelist env helpers (typo'd bools collapse to default;
# placeholder secrets like ``your_*`` / ``xxxx*`` are rejected).
from backend._envloader import env_bool as _env_bool, env_secret_or_none

_ALLOWED_WEBHOOK_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
    "https://canary.discord.com/api/webhooks/",
    "https://ptb.discord.com/api/webhooks/",
)


def _webhook_url() -> Optional[str]:
    """Resolve + validate the outbound Discord webhook URL.

    Returns None when unset, a placeholder, or not a real discord.com webhook
    endpoint — defensive so a misconfiguration never POSTs to an arbitrary host.
    """
    url = env_secret_or_none("DISCORD_WEBHOOK_URL")
    if not url:
        return None
    u = url.strip()
    if not u.startswith(_ALLOWED_WEBHOOK_PREFIXES):
        logger.warning(
            "DISCORD_WEBHOOK_URL is not a discord.com webhook URL; refusing to use it."
        )
        return None
    return u


def _username() -> Optional[str]:
    name = (os.environ.get("DISCORD_OUTBOUND_USERNAME") or "").strip()
    return name or None


def is_enabled() -> bool:
    """Discord outbound is opt-in (off by default, no webhook by default)."""
    if not _env_bool("DISCORD_OUTBOUND_ENABLED", False):
        return False
    return bool(_webhook_url())


def _rate_limit() -> None:
    global _LAST_SEND_TS
    with _GLOBAL_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_SEND_TS
        wait = max(0.0, _MIN_INTERVAL_SECONDS - elapsed)
        # Reserve the next slot under the lock so a second thread entering while
        # we sleep computes its own wait against the already-reserved time.
        _LAST_SEND_TS = now + wait
    if wait > 0.0:
        time.sleep(wait)


def send_markdown(text: str) -> bool:
    """POST a message to the configured Discord webhook.

    Returns True on success, False on any failure / when disabled. NEVER raises.
    """
    if not is_enabled():
        return False
    url = _webhook_url()
    if not url:
        return False
    payload = {
        "content": (text or "")[:_MAX_CONTENT],
        # Never ping @everyone/@here/roles/users from automated posts.
        "allowed_mentions": {"parse": []},
    }
    name = _username()
    if name:
        payload["username"] = name[:80]
    try:
        _rate_limit()
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0)) as cli:
            resp = cli.post(url, json=payload)
        if resp.status_code >= 400:
            logger.warning(
                "Discord send failed: %s %s", resp.status_code, resp.text[:200]
            )
            return False
        return True
    except httpx.RequestError as exc:
        logger.warning("Discord network error: %s", exc)
        return False
    except Exception:
        logger.exception("Discord send crashed")
        return False


# ---------------------------------------------------------------------------
# Higher-level templates — mirror telegram_notifier so call sites stay parallel
# ---------------------------------------------------------------------------


def notify_strategy_transition(
    strategy_id: int,
    *,
    name: str,
    from_status: str,
    to_status: str,
    metrics: Optional[dict] = None,
) -> bool:
    """Posted on every pipeline state transition (when enabled)."""
    bits = [
        f"**S#{strategy_id}** `{(name or '')[:64]}`",
        f"`{from_status}` → `{to_status}`",
    ]
    if metrics:
        s = metrics.get("annualized_sharpe")
        d = metrics.get("max_drawdown")
        if s is not None:
            bits.append(f"Sharpe `{float(s):.2f}`")
        if d is not None:
            bits.append(f"MaxDD `{float(d) * 100:.1f}%`")
    return send_markdown("🛠️ " + " · ".join(bits))


def notify_pipeline_error(strategy_id: int, stage: str, error: str) -> bool:
    safe_stage = str(stage)[:120].replace("`", "'")
    msg = (
        f"⚠️ **Pipeline error** — S#{strategy_id} at `{safe_stage}`\n"
        f"`{str(error)[:480]}`"
    )
    return send_markdown(msg)


def notify_intake(
    *,
    node_id: Optional[int],
    title: str,
    entry_point: str = "http",
    source_url: Optional[str] = None,
) -> bool:
    """Posted when a knowledge node is created via intake (when enabled)."""
    bits = [f"📥 **New intake** `K#{node_id}`", f"{str(title)[:160]}"]
    if entry_point and entry_point != "http":
        bits.append(f"via `{entry_point}`")
    if source_url:
        bits.append(f"<{str(source_url)[:300]}>")
    return send_markdown(" · ".join(bits))


__all__ = [
    "is_enabled",
    "send_markdown",
    "notify_strategy_transition",
    "notify_pipeline_error",
    "notify_intake",
]
