"""Exchange adapter stub (P7 — live-trade + trading-terminal).

**Intentionally not wired to a real exchange.**

The M14 task ("Live exchange SDK integration") was explicitly deferred — see
the project's :doc:`CLAUDE.md` ``高風險系統加強`` block. Sending live orders
requires:

* Real exchange API keys + signing
* Per-request idempotent ``client_order_id`` per CLAUDE.md replay rule
* DB-level ledger of every order intent / fill / cancel
* Rate-limit + back-off respecting exchange-specific weight budgets
* Signed-webhook handling for fill notifications
* Reconciliation loop between local position and exchange position

All of the above are out of scope for the current release. This module exists
only so the ``/live-trade`` and ``/trading-terminal`` pages can render a
"PAPER mode — adapter not configured" state without import errors.

When M14 is unblocked, swap the body of each method for a real CCXT call (or
direct REST per exchange). Until then every method raises
``NotImplementedError`` with a pointer to this docstring, and the routes
short-circuit on ``LIVE_TRADE_ENABLED=0`` before reaching the adapter.

Required env vars when the real adapter is finally wired:

* ``EXCHANGE_NAME``   — ``binance`` / ``bybit`` / ``okx`` / etc.
* ``EXCHANGE_API_KEY``
* ``EXCHANGE_API_SECRET``
* ``EXCHANGE_PASSPHRASE`` (OKX-style three-factor)
* ``EXCHANGE_TESTNET`` — ``1`` to route to testnet endpoints
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend._envloader import env_bool, env_str


@dataclass
class ExchangePing:
    status: str          # "paper_mode" | "connected" | "disconnected" | "degraded"
    latency_ms: Optional[float]
    venue: Optional[str]
    note: Optional[str] = None


def is_live_enabled() -> bool:
    return env_bool("LIVE_TRADE_ENABLED", False)


def configured_venue() -> str:
    return env_str("EXCHANGE_NAME", "")


class ExchangeAdapter:
    """Stub. Real implementation is M14 (deferred)."""

    DEFER_MSG = (
        "CCXT/live exchange integration deferred — see "
        "backend.core.exchange_adapter module docstring (M14)."
    )

    def __init__(self) -> None:
        if not is_live_enabled():
            self.mode = "paper"
        else:
            # Even when LIVE_TRADE_ENABLED=1, until M14 lands we still raise on
            # any state-changing method. ping() returns degraded.
            self.mode = "live_unconfigured"

    # ------------------------------------------------------------------
    # Read-only methods (safe-ish — return mock data so the page renders)
    # ------------------------------------------------------------------

    def ping(self) -> ExchangePing:
        """Return adapter health snapshot consumed by the live-trade dashboard.

        P15/D-L11 NOTE: ``venue`` is ``None`` whenever the operator has not yet
        configured EXCHANGE_VENUE (or set it to the empty string). The frontend
        is expected to render that case as "no venue configured" rather than
        a missing field — never silently default to a real venue name here, so
        operators can't accidentally route to the wrong exchange.
        """
        venue = configured_venue() or None
        if self.mode == "paper":
            return ExchangePing(
                status="paper_mode",
                latency_ms=None,
                venue=venue,
                note="LIVE_TRADE_ENABLED=0 — orders go to paper trade engine",
            )
        return ExchangePing(
            status="degraded",
            latency_ms=None,
            venue=venue,
            note=self.DEFER_MSG,
        )

    def fetch_positions(self) -> List[Dict[str, Any]]:
        if self.mode == "paper":
            return []
        raise NotImplementedError(self.DEFER_MSG)

    # ------------------------------------------------------------------
    # State-changing methods (always raise — paper engine handles paper orders)
    # ------------------------------------------------------------------

    def place_order(self, **_kwargs: Any) -> Dict[str, Any]:
        raise NotImplementedError(self.DEFER_MSG)

    def cancel_order(self, _order_id: str) -> Dict[str, Any]:
        raise NotImplementedError(self.DEFER_MSG)


__all__ = [
    "ExchangeAdapter",
    "ExchangePing",
    "is_live_enabled",
    "configured_venue",
]
