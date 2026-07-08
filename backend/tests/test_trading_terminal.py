"""Unit + integration tests for backend/core/trading_terminal.py.

Covers audit finding #33 (validate_order fat-finger, notional cap, Decimal
step-check) and finding #35 (idempotency_key uniqueness enforcement in
submit_paper — same key must not produce two orders).

Strategy:
  - Patch _btc_market_snapshot to return a deterministic non-stale quote so
    market_info() returns predictable values without touching the filesystem.
  - Use TRADING_REPLAY_MODE=1 + TRADING_MAX_QUOTE_AGE_SEC=0 to disable the
    stale-quote TTL guard, which is driven by file mtime and is irreproducible
    in unit tests. Patching _btc_market_snapshot directly is cleaner and is
    the approach used throughout this suite.
  - All DB-touching tests use an in-memory SQLite session.
  - No network calls, no LLM calls, no live exchange.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any, Dict
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# Path bootstrap (conftest.py already handles sys.path; this import is a
# belt-and-suspenders guard for direct pytest runs from non-repo root dirs).
# ---------------------------------------------------------------------------
from backend.core.database import Base, IdempotencyKey, ManualFill, ManualOrder
from backend.core.trading_terminal import (
    fat_finger_pct,
    max_order_notional_usdt,
    validate_order,
)


# ---------------------------------------------------------------------------
# Module-level mock: a stable, non-stale BTC snapshot at $50 000.
# This is returned by _btc_market_snapshot() so all market_info() calls in the
# tests below see a predictable, fresh quote without reading the CSV.
# Signature: (last_btc: float, vol_24h: float, change_24h: float, stale: bool)
# ---------------------------------------------------------------------------
_MOCK_SNAPSHOT = (50_000.0, 1_000.0, 0.0, False)

# BTC-USDT is the only symbol with ratio=1.0, so:
#   last   = 50 000.0
#   bid    = 50 000.0 * 0.9995 = 49 975.0
#   ask    = 50 000.0 * 1.0005 = 50 025.0


def _snapshot_patch():
    """Return a context manager that patches _btc_market_snapshot."""
    return patch(
        "backend.core.trading_terminal._btc_market_snapshot",
        return_value=_MOCK_SNAPSHOT,
    )


# ---------------------------------------------------------------------------
# Shared valid payload for BTC-USDT limit buy within all safety bounds.
# fat_finger_pct() default = 0.03 (3%).  50 000 * 1.03 = 51 500 (excluded).
# A price of exactly 50 000.0 is at drift=0.0, well within the cap.
# notional = 0.0001 BTC * 50 000 = 5 USDT, far below the 50 000 USDT cap.
# qty_step for BTC-USDT = 0.0001; qty=0.0001 is exactly aligned.
# ---------------------------------------------------------------------------
_VALID_BUY_PAYLOAD: Dict[str, Any] = {
    "symbol": "BTC-USDT",
    "side": "buy",
    "order_type": "limit",
    "qty": "0.0001",
    "limit_price": "50000.0",
    "mode": "paper",
}


# ============================================================================
# Finding #33 — validate_order guard paths
# ============================================================================


class TestFatFinger:
    """Validate the fat-finger price rejection path."""

    def test_buy_limit_above_cap_rejected(self):
        """A buy limit_price > last * (1 + fat_finger_pct()) must be rejected."""
        pct = fat_finger_pct()
        dangerous_price = 50_000.0 * (1.0 + pct + 0.01)  # 1 pp above the threshold
        payload = {**_VALID_BUY_PAYLOAD, "limit_price": str(dangerous_price)}
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is False, f"Expected rejection but got ok=True; result={result}"
        combined = " ".join(result.get("errors", [])).lower()
        # The error message produced by production code at line ~395:
        # "price {ref} > {fat_finger_pct()*100:.2f}% from last {last}"
        assert "%" in combined or "fat" in combined or "last" in combined, (
            f"Expected a fat-finger-style error, got: {result['errors']}"
        )

    def test_sell_limit_below_cap_rejected(self):
        """A sell limit_price well below last must also be rejected."""
        pct = fat_finger_pct()
        dangerous_price = 50_000.0 * (1.0 - pct - 0.01)  # 1 pp below the threshold
        payload = {
            **_VALID_BUY_PAYLOAD,
            "side": "sell",
            "limit_price": str(dangerous_price),
        }
        with _snapshot_patch():
            result = validate_order(payload)
        # Production code applies the fat-finger check symmetrically via
        # abs(ref / last - 1.0) > fat_finger_pct(); a price 4% below last
        # triggers it in both buy and sell directions.
        assert result["ok"] is False, (
            f"Sell at {dangerous_price} with pct={pct} should be rejected; got ok=True"
        )

    def test_price_within_cap_passes(self):
        """A price at the mark (drift=0) must NOT trigger fat-finger rejection."""
        payload = {**_VALID_BUY_PAYLOAD, "limit_price": "50000.0"}
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is True, f"Expected ok=True, got errors: {result.get('errors')}"

    def test_fat_finger_pct_returns_valid_float(self):
        """fat_finger_pct() must return a finite positive float in (0, 1]."""
        pct = fat_finger_pct()
        assert isinstance(pct, float)
        assert 0.0 < pct <= 1.0

    def test_price_just_inside_threshold_accepted(self):
        """A price slightly inside the fat-finger cap must be accepted.

        The guard is `drift > fat_finger_pct()` (strict >).  A price at
        (1 + pct * 0.99) * last has drift = pct * 0.99, which is strictly
        less than pct, so no error should be generated.

        Note: the exact boundary (drift == pct) is unreliable due to IEEE 754
        float representation — 50000 * 1.03 / 50000 - 1 may round to a value
        epsilon above 0.03, so we use 99% of the cap as a safe interior point.
        """
        pct = fat_finger_pct()
        interior_price = 50_000.0 * (1.0 + pct * 0.99)  # 1% inside the cap
        payload = {**_VALID_BUY_PAYLOAD, "limit_price": str(interior_price)}
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is True, (
            f"Price {interior_price} is within the fat-finger cap; "
            f"got errors: {result.get('errors')}"
        )


class TestNotionalCap:
    """Validate the order notional cap rejection path."""

    def test_high_qty_rejected_by_cap(self):
        """qty=2 BTC at 50 000 => notional=100 000; default cap=50 000 -> reject."""
        payload = {
            **_VALID_BUY_PAYLOAD,
            "qty": "2.0",          # 2 BTC * 50 000 = 100 000 USDT
            "limit_price": "50000.0",
        }
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is False
        combined = " ".join(result.get("errors", [])).lower()
        assert "notional" in combined, (
            f"Expected 'notional' in error message, got: {result.get('errors')}"
        )

    def test_notional_within_cap_accepted(self):
        """qty=0.0001 BTC at 50 000 => notional=5 USDT; well within cap -> accept."""
        payload = {**_VALID_BUY_PAYLOAD, "qty": "0.0001", "limit_price": "50000.0"}
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is True, f"Expected ok=True, got: {result.get('errors')}"

    def test_max_order_notional_returns_valid_float(self):
        """max_order_notional_usdt() must return a finite positive float."""
        cap = max_order_notional_usdt()
        assert isinstance(cap, float)
        assert cap > 0.0

    def test_custom_cap_via_env(self, monkeypatch):
        """With a very small cap (1 000 USDT), any 1 BTC order is over-cap."""
        monkeypatch.setenv("TRADING_MAX_ORDER_NOTIONAL_USDT", "1000.0")
        payload = {
            **_VALID_BUY_PAYLOAD,
            "qty": "1.0",
            "limit_price": "50000.0",
        }
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is False
        assert any("notional" in e.lower() for e in result.get("errors", []))


class TestDecimalStepCheck:
    """Validate the Decimal qty step-alignment path (P30-T7 tolerance band)."""

    def test_unaligned_qty_rejected(self):
        """qty=0.00015 with BTC-USDT step=0.0001: residue=0.5*step -> reject."""
        # step=0.0001, qty=0.00015 -> residue=0.00005=0.5*step > 0.001*step tol
        payload = {**_VALID_BUY_PAYLOAD, "qty": "0.00015"}
        with _snapshot_patch():
            result = validate_order(payload)
        # Either rejected outright, or the test documents that the tolerance
        # band accepted it (which would be a step-check regression).
        assert result["ok"] is False, (
            "validate_order must reject qty=0.00015 with step=0.0001 "
            "(residue=0.00005 is 500x the 0.001*step tolerance; P30-T7 regression)"
        )
        combined = " ".join(result.get("errors", [])).lower()
        assert "step" in combined or "align" in combined, (
            f"Expected step-alignment error, got: {result.get('errors')}"
        )

    def test_aligned_qty_accepted(self):
        """qty=0.0002 with step=0.0001: exact alignment must pass."""
        payload = {**_VALID_BUY_PAYLOAD, "qty": "0.0002"}
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is True, f"Expected ok=True for aligned qty, got: {result}"

    def test_qty_below_min_rejected(self):
        """qty < min_qty must produce a specific error."""
        # BTC-USDT min_qty=0.0001; qty=0.00001 is below min
        payload = {**_VALID_BUY_PAYLOAD, "qty": "0.00001"}
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is False
        combined = " ".join(result.get("errors", [])).lower()
        assert "min" in combined or "qty" in combined

    def test_zero_qty_rejected(self):
        """qty=0 must always be rejected."""
        payload = {**_VALID_BUY_PAYLOAD, "qty": "0"}
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is False
        assert any("qty" in e.lower() for e in result.get("errors", []))

    def test_nan_qty_rejected(self):
        """qty=NaN must be sanitized (P31-NUM1) and rejected, not cause a crash."""
        payload = {**_VALID_BUY_PAYLOAD, "qty": "nan"}
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is False


class TestValidateOrderMisc:
    """Miscellaneous validate_order guard paths."""

    def test_invalid_symbol_rejected(self):
        """An unlisted symbol must be rejected with a clear error."""
        payload = {**_VALID_BUY_PAYLOAD, "symbol": "FAKECOIN-USDT"}
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is False
        assert any("not supported" in e.lower() for e in result.get("errors", []))

    def test_invalid_side_rejected(self):
        payload = {**_VALID_BUY_PAYLOAD, "side": "long"}
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is False
        assert any("side" in e.lower() for e in result.get("errors", []))

    def test_invalid_order_type_rejected(self):
        payload = {**_VALID_BUY_PAYLOAD, "order_type": "stop_limit"}
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is False

    def test_limit_order_missing_limit_price_rejected(self):
        payload = {k: v for k, v in _VALID_BUY_PAYLOAD.items() if k != "limit_price"}
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is False
        assert any("limit_price" in e.lower() for e in result.get("errors", []))

    def test_market_order_must_use_ioc(self):
        """Market orders with tif=gtc must be rejected."""
        payload = {
            **_VALID_BUY_PAYLOAD,
            "order_type": "market",
            "tif": "gtc",
        }
        payload.pop("limit_price", None)
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is False
        assert any("ioc" in e.lower() for e in result.get("errors", []))

    def test_live_mode_blocked_by_default(self):
        """Live mode requires LIVE_TRADE_ENABLED=1; default is off -> reject."""
        payload = {**_VALID_BUY_PAYLOAD, "mode": "live"}
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is False
        # Expect both LIVE_TRADE_ENABLED and LIVE_ROUTING_NOT_IMPLEMENTED errors
        combined = " ".join(result.get("errors", [])).lower()
        assert "live" in combined

    def test_inf_price_rejected(self):
        """A limit_price of Infinity must be sanitized and rejected (P31-NUM7)."""
        payload = {**_VALID_BUY_PAYLOAD, "limit_price": "inf"}
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is False

    def test_validate_order_returns_mark_price(self):
        """validate_order must return mark_price=50000.0 from the mocked snapshot."""
        payload = {**_VALID_BUY_PAYLOAD}
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["mark_price"] == 50_000.0

    def test_validate_order_returns_symbol_spec(self):
        """validate_order must populate symbol_spec for a known symbol."""
        payload = {**_VALID_BUY_PAYLOAD}
        with _snapshot_patch():
            result = validate_order(payload)
        assert result["symbol_spec"] is not None
        assert result["symbol_spec"]["symbol"] == "BTC-USDT"

    def test_market_halted_symbol_rejected(self, monkeypatch):
        """P18/D2 circuit-breaker: when ALPHA_MARKET_HALTED_SYMBOLS lists the
        symbol, validate_order must reject with a MARKET_HALTED:<sym> error.

        This guards against regressions in the env-var check itself (wrong var
        name, case-sensitivity bug, missing strip, etc.) which would silently
        disable the operational halt guard during flash crashes or exchange
        outages.
        """
        # BTC-USDT is the symbol used in _VALID_BUY_PAYLOAD; set it as halted.
        monkeypatch.setenv("ALPHA_MARKET_HALTED_SYMBOLS", "BTC-USDT")
        with _snapshot_patch():
            result = validate_order({**_VALID_BUY_PAYLOAD})
        assert result["ok"] is False, (
            "validate_order must reject orders for a halted symbol "
            "(ALPHA_MARKET_HALTED_SYMBOLS=BTC-USDT should activate the circuit-breaker)"
        )
        assert any("MARKET_HALTED" in e for e in result.get("errors", [])), (
            f"Expected a 'MARKET_HALTED:BTC-USDT' error entry; got: {result.get('errors')}"
        )

    def test_market_halted_check_is_case_insensitive(self, monkeypatch):
        """The halt check must be case-insensitive: lower-case entry still halts."""
        # env_str_set + _is_market_halted both upper-case before comparison.
        monkeypatch.setenv("ALPHA_MARKET_HALTED_SYMBOLS", "btc-usdt")
        with _snapshot_patch():
            result = validate_order({**_VALID_BUY_PAYLOAD})
        assert result["ok"] is False, (
            "Case-insensitive halt check failed: 'btc-usdt' in env var must "
            "still halt 'BTC-USDT' orders"
        )
        assert any("MARKET_HALTED" in e for e in result.get("errors", [])), (
            f"Expected MARKET_HALTED error; got: {result.get('errors')}"
        )

    def test_unhalted_symbol_not_blocked_by_halt_env(self, monkeypatch):
        """An env var that halts ETH-USDT must NOT block BTC-USDT orders."""
        monkeypatch.setenv("ALPHA_MARKET_HALTED_SYMBOLS", "ETH-USDT")
        with _snapshot_patch():
            result = validate_order({**_VALID_BUY_PAYLOAD})
        assert result["ok"] is True, (
            "BTC-USDT must not be blocked when only ETH-USDT is halted; "
            f"got errors: {result.get('errors')}"
        )

    def test_empty_halted_env_does_not_block(self, monkeypatch):
        """An empty ALPHA_MARKET_HALTED_SYMBOLS must not block any order."""
        monkeypatch.setenv("ALPHA_MARKET_HALTED_SYMBOLS", "")
        with _snapshot_patch():
            result = validate_order({**_VALID_BUY_PAYLOAD})
        assert result["ok"] is True, (
            "Empty halt list must not block orders; "
            f"got errors: {result.get('errors')}"
        )


# ============================================================================
# Finding #35 — idempotency_key uniqueness: same key must not produce two orders
# ============================================================================


@pytest.fixture()
def db_session():
    """Ephemeral in-memory SQLite session for integration tests.

    Uses the production Base so all schema (ManualOrder, ManualFill,
    ManualPosition, IdempotencyKey, AuditLog, …) is exactly as defined.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


def _make_submit_fn(session, idem_key: str, payload: Dict[str, Any]):
    """Return a compute_fn compatible with lookup_or_record.

    The compute_fn calls submit_paper (which does the real DB work) and
    returns (response_dict, 200) as the idempotency layer expects.
    """
    from backend.core.trading_terminal import submit_paper

    def _fn():
        result = submit_paper(
            session,
            payload=payload,
            terminal_session_id="test-session",
            request_ip="127.0.0.1",
            user_agent="pytest",
            idempotency_key=idem_key,
        )
        return result, 200

    return _fn


class TestSubmitPaperIdempotency:
    """Finding #35: same Idempotency-Key must not produce two ManualOrder rows."""

    def test_same_key_second_call_is_replay(self, db_session, monkeypatch):
        """Calling submit_paper twice via the idempotency layer with the same key
        must return replay=True on the second call and create exactly one row.
        """
        # Disable balance enforcement so we don't need pre-seeded cash state.
        monkeypatch.setenv("TRADING_TERMINAL_ENFORCE_BALANCE", "0")

        from backend.core.idempotency import canonical_request_hash, lookup_or_record

        idem_key = "test-idem-submit-001"
        payload = {**_VALID_BUY_PAYLOAD}
        req_hash = canonical_request_hash(
            {k: v for k, v in payload.items() if k != "idempotency_key"}
        )

        with _snapshot_patch():
            # First call: miss path — should create the order.
            outcome1 = lookup_or_record(
                db_session,
                key=idem_key,
                request_hash=req_hash,
                compute_fn=_make_submit_fn(db_session, idem_key, payload),
            )
            db_session.commit()

        assert outcome1.replay is False
        assert outcome1.status_code == 200
        assert isinstance(outcome1.response_payload, dict)

        with _snapshot_patch():
            # Second call: same key — must replay without calling compute_fn.
            outcome2 = lookup_or_record(
                db_session,
                key=idem_key,
                request_hash=req_hash,
                compute_fn=_make_submit_fn(db_session, idem_key, payload),
            )
            db_session.commit()

        assert outcome2.replay is True, (
            "Second call with identical Idempotency-Key must be a replay — "
            "double-submit risk if this fails!"
        )

        # Critical: exactly ONE ManualOrder row for this key.
        count = (
            db_session.query(ManualOrder)
            .filter(ManualOrder.idempotency_key == idem_key)
            .count()
        )
        assert count == 1, (
            f"Expected 1 ManualOrder row for idempotency_key={idem_key!r}, "
            f"found {count} — double-order insertion bug!"
        )

    def test_different_keys_produce_distinct_orders(self, db_session, monkeypatch):
        """Two different idempotency keys with the same payload produce two separate orders."""
        monkeypatch.setenv("TRADING_TERMINAL_ENFORCE_BALANCE", "0")

        from backend.core.idempotency import canonical_request_hash, lookup_or_record

        payload = {**_VALID_BUY_PAYLOAD}
        req_hash = canonical_request_hash(
            {k: v for k, v in payload.items() if k != "idempotency_key"}
        )

        with _snapshot_patch():
            for i in range(2):
                key = f"test-idem-distinct-{i:03d}"
                lookup_or_record(
                    db_session,
                    key=key,
                    request_hash=req_hash,
                    compute_fn=_make_submit_fn(db_session, key, payload),
                )
            db_session.commit()

        count = db_session.query(ManualOrder).count()
        assert count == 2, (
            f"Expected 2 ManualOrder rows (one per unique key), got {count}"
        )

    def test_same_key_different_body_raises_409(self, db_session, monkeypatch):
        """Same idempotency key with a different request body must raise HTTP 409."""
        monkeypatch.setenv("TRADING_TERMINAL_ENFORCE_BALANCE", "0")

        from fastapi import HTTPException
        from backend.core.idempotency import canonical_request_hash, lookup_or_record

        idem_key = "test-idem-conflict-001"
        payload_a = {**_VALID_BUY_PAYLOAD, "qty": "0.0001"}
        payload_b = {**_VALID_BUY_PAYLOAD, "qty": "0.0002"}
        hash_a = canonical_request_hash({k: v for k, v in payload_a.items()})
        hash_b = canonical_request_hash({k: v for k, v in payload_b.items()})

        with _snapshot_patch():
            # First call with payload_a
            lookup_or_record(
                db_session,
                key=idem_key,
                request_hash=hash_a,
                compute_fn=_make_submit_fn(db_session, idem_key, payload_a),
            )
            db_session.commit()

        with _snapshot_patch():
            # Second call with a different body hash — must raise 409
            with pytest.raises(HTTPException) as exc_info:
                lookup_or_record(
                    db_session,
                    key=idem_key,
                    request_hash=hash_b,
                    compute_fn=_make_submit_fn(db_session, idem_key, payload_b),
                )

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["error"] == "IDEMPOTENCY_KEY_REUSED"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "R5/QA-03 / Finding #35: ManualOrder.idempotency_key lacks a UNIQUE DB "
            "constraint. Expected to fail until a UNIQUE constraint is added to the "
            "manual_orders table, closing the double-submit bypass that exists when "
            "submit_paper is called outside the HTTP idempotency layer. Once added, "
            "this xfail becomes an xpass — remove the decorator at that point."
        ),
    )
    def test_no_unique_db_constraint_on_idempotency_key_column(self, db_session):
        """Assert that ManualOrder.idempotency_key HAS a UNIQUE DB constraint.

        Finding #35: the only double-submit guard lives in the HTTP route handler
        (via idempotency.py). The ManualOrder.idempotency_key column has no UNIQUE
        constraint at the DB schema level, so if the route handler is bypassed
        (internal task queue, worker calling submit_paper directly) double-insertion
        is possible. This test documents the DESIRED secure state; it is xfail until
        the constraint is added (then it auto-promotes to a passing test).
        """
        from sqlalchemy import inspect
        inspector = inspect(db_session.bind)
        unique_constraints = inspector.get_unique_constraints("manual_orders")
        uc_columns = [
            col
            for uc in unique_constraints
            for col in uc.get("column_names", [])
        ]
        assert "idempotency_key" in uc_columns, (
            "ManualOrder.idempotency_key must have a UNIQUE DB constraint for "
            "defence-in-depth against double-submit when the HTTP layer is bypassed."
        )


# ============================================================================
# Finding B14-2 — STALE_QUOTE rejection path in validate_order
# ============================================================================

# A stale snapshot: same prices as _MOCK_SNAPSHOT but with stale=True (index 3).
# This exercises the P30-T6 guard at trading_terminal.py lines 337-343 without
# touching the filesystem or relying on CSV mtime — identical mechanism to the
# existing _snapshot_patch() approach throughout this suite.
_STALE_SNAPSHOT = (50_000.0, 1_000.0, 0.0, True)


def _stale_snapshot_patch():
    """Return a context manager that patches _btc_market_snapshot with stale=True."""
    return patch(
        "backend.core.trading_terminal._btc_market_snapshot",
        return_value=_STALE_SNAPSHOT,
    )


class TestStaleQuote:
    """B14-2: validate_order must reject all orders when quote_stale=True.

    The P30-T6 guard at trading_terminal.py:337-343 must:
      (1) append STALE_QUOTE:<sym> to errors, and
      (2) set last=None so the downstream fat-finger block is skipped,
          preventing a misleading drift error computed against a stale price.
    """

    def test_stale_quote_buy_rejected(self):
        """validate_order with a stale snapshot must return ok=False with STALE_QUOTE in errors."""
        with _stale_snapshot_patch():
            result = validate_order(_VALID_BUY_PAYLOAD)
        assert result["ok"] is False, (
            "validate_order must reject an order when the quote is stale; "
            f"got ok=True with result={result}"
        )
        combined = " ".join(result.get("errors", []))
        assert "STALE_QUOTE" in combined, (
            f"Expected 'STALE_QUOTE' error token in error list; got: {result.get('errors')}"
        )

    def test_stale_quote_does_not_emit_fat_finger_error(self):
        """When quote_stale=True, errors must contain STALE_QUOTE but NOT a fat-finger drift error.

        The P32-T3 comment at trading_terminal.py:339-343 explains: clearing `last`
        to None prevents the downstream drift/fat-finger block from firing a
        misleading '% from last' error computed against an arbitrarily old price.
        Both errors firing simultaneously would confuse the operator; only the
        actionable STALE_QUOTE error should appear.
        """
        with _stale_snapshot_patch():
            result = validate_order(_VALID_BUY_PAYLOAD)
        errors = result.get("errors", [])
        # Must have the stale-quote error.
        assert any("STALE_QUOTE" in e for e in errors), (
            f"Expected STALE_QUOTE error, got: {errors}"
        )
        # Must NOT have a fat-finger / drift error (which would reference a
        # meaningless stale last price).
        fat_finger_fired = any(
            "%" in e and "last" in e.lower()
            for e in errors
        )
        assert not fat_finger_fired, (
            "Fat-finger drift error must not fire when quote is stale "
            f"(P32-T3 regression); errors={errors}"
        )

    def test_stale_quote_sell_rejected(self):
        """STALE_QUOTE guard applies symmetrically to sell orders."""
        payload = {**_VALID_BUY_PAYLOAD, "side": "sell"}
        with _stale_snapshot_patch():
            result = validate_order(payload)
        assert result["ok"] is False
        assert any("STALE_QUOTE" in e for e in result.get("errors", []))

    def test_non_stale_snapshot_passes_normally(self):
        """Regression guard: a non-stale snapshot must NOT produce STALE_QUOTE errors.

        Ensures the stale-snapshot fixture change does not accidentally break
        the baseline non-stale path already covered by TestFatFinger.
        """
        with _snapshot_patch():
            result = validate_order(_VALID_BUY_PAYLOAD)
        assert result["ok"] is True, (
            f"Non-stale baseline must still pass; got errors: {result.get('errors')}"
        )
        assert not any("STALE_QUOTE" in e for e in result.get("errors", []))


# ============================================================================
# Finding #21 — cancel_order: idempotency no-op, reaper race, terminal rejection
# ============================================================================


def _make_pending_order(session, order_uid: str = "test-uid-001") -> "ManualOrder":
    """Insert a minimal 'pending' ManualOrder row and flush (no commit needed)."""
    from datetime import datetime, timezone

    order = ManualOrder(
        order_uid=order_uid,
        symbol="BTC-USDT",
        side="buy",
        order_type="limit",
        qty=0.0001,
        limit_price=50000.0,
        tif="gtc",
        mode="paper",
        status="pending",
        placed_by="manual_terminal",
        requested_at=datetime.now(timezone.utc),
        idempotency_key=f"idem-{order_uid}",
    )
    session.add(order)
    session.flush()
    return order


class TestCancelOrder:
    """Finding #21: cancel_order safety-critical paths must all have coverage.

    Tests:
      (a) Happy path: cancel a pending order -> status becomes 'cancelled'.
      (b) Idempotency no-op: cancel an already-cancelled order returns the row
          without raising.
      (c) Terminal non-OK rejection: cancel a 'filled' order raises ValueError.
      (d) Reaper race: conditional UPDATE returns 0 rows -> ValueError with
          'concurrent reaper fill won'.
    """

    def test_cancel_pending_order_succeeds(self, db_session):
        """(a) A pending order can be cancelled; status must flip to 'cancelled'."""
        from backend.core.trading_terminal import cancel_order

        order = _make_pending_order(db_session, "cancel-happy-001")
        result = cancel_order(
            db_session,
            order_uid=order.order_uid,
            idempotency_key="idem-cancel-happy-001",
            request_ip="127.0.0.1",
            user_agent="pytest",
        )
        assert result["status"] == "cancelled", (
            f"Expected status='cancelled' after cancel, got {result['status']!r}"
        )
        db_session.commit()
        db_session.refresh(order)
        assert order.status == "cancelled"

    def test_cancel_already_cancelled_is_idempotent(self, db_session):
        """(b) Cancelling an order that is already 'cancelled' must return the row
        without raising — idempotent no-op per P31-STATE5.
        """
        from backend.core.trading_terminal import cancel_order

        order = _make_pending_order(db_session, "cancel-noop-001")
        # First cancel: transitions pending -> cancelled.
        cancel_order(
            db_session,
            order_uid=order.order_uid,
            idempotency_key="idem-noop-first",
            request_ip=None,
            user_agent=None,
        )
        db_session.commit()

        # Second cancel: must not raise, must return the row.
        result = cancel_order(
            db_session,
            order_uid=order.order_uid,
            idempotency_key="idem-noop-second",
            request_ip=None,
            user_agent=None,
        )
        assert result["status"] == "cancelled", (
            "Idempotent no-op must return the already-cancelled row; "
            f"got status={result['status']!r}"
        )

    def test_cancel_filled_order_raises(self, db_session):
        """(c) Cancelling a 'filled' order must raise ValueError (terminal non-OK).

        Regression guard: if broken, a client cancel could overwrite a fill and
        cause a position desync.
        """
        from backend.core.trading_terminal import cancel_order

        order = _make_pending_order(db_session, "cancel-filled-001")
        # Directly force status to 'filled' (simulates reaper completion).
        order.status = "filled"
        db_session.flush()

        with pytest.raises(ValueError, match="terminal"):
            cancel_order(
                db_session,
                order_uid=order.order_uid,
                idempotency_key="idem-cancel-filled",
                request_ip=None,
                user_agent=None,
            )

    def test_cancel_rejected_order_raises(self, db_session):
        """(c-variant) 'rejected' is also terminal non-OK — must raise ValueError."""
        from backend.core.trading_terminal import cancel_order

        order = _make_pending_order(db_session, "cancel-rejected-001")
        order.status = "rejected"
        db_session.flush()

        with pytest.raises(ValueError, match="terminal"):
            cancel_order(
                db_session,
                order_uid=order.order_uid,
                idempotency_key="idem-cancel-rejected",
                request_ip=None,
                user_agent=None,
            )

    def test_cancel_reaper_race_raises(self, db_session, monkeypatch):
        """(d) When the conditional UPDATE returns 0 rows (reaper already filled),
        cancel_order must raise ValueError mentioning 'reaper' or 'concurrent'.

        The mock makes the ORM UPDATE call return 0 even though the row is pending,
        simulating the cross-process race where the reaper flipped the row between
        our SELECT and our UPDATE.
        """
        from unittest.mock import patch as mock_patch
        from backend.core.trading_terminal import cancel_order

        order = _make_pending_order(db_session, "cancel-race-001")
        db_session.commit()

        # Patch the query().filter().filter().update() chain so it returns 0.
        # We intercept at the SQLAlchemy Query.update level.
        original_update = db_session.query(ManualOrder).__class__.update

        def _zero_update(self, values, synchronize_session=False, **kw):
            # Only intercept if this is the cancel UPDATE (status->cancelled).
            if isinstance(values, dict) and values.get("status") == "cancelled":
                return 0
            return original_update(self, values, synchronize_session=synchronize_session, **kw)

        with mock_patch.object(
            db_session.query(ManualOrder).__class__,
            "update",
            _zero_update,
        ):
            with pytest.raises(ValueError) as exc_info:
                cancel_order(
                    db_session,
                    order_uid=order.order_uid,
                    idempotency_key="idem-cancel-race",
                    request_ip=None,
                    user_agent=None,
                )

        msg = str(exc_info.value).lower()
        assert "reaper" in msg or "concurrent" in msg or "race" in msg, (
            f"Expected race/reaper/concurrent in error message, got: {exc_info.value!r}"
        )


# ============================================================================
# Finding #20 — negative-balance guard (CLAUDE.md no-negative-balance invariant)
# ============================================================================


class TestBalanceGuard:
    """Verify the INSUFFICIENT_BALANCE guard in submit_paper.

    The guard lives at trading_terminal.py lines 729-739: when
    TRADING_TERMINAL_ENFORCE_BALANCE=1 a paper buy that would drive the derived
    cash balance negative raises ValueError('INSUFFICIENT_BALANCE: ...').
    Default cash = TRADING_TERMINAL_DEFAULT_CASH_USDT (default 100 000 USDT).

    Strategy: seed the DB with a filled buy that consumes almost all cash by
    inserting a ManualOrder (status='filled') + ManualFill directly, then call
    submit_paper with enforce=1.  This mirrors what _paper_available_cash()
    reads: filled orders via ManualFill joined to ManualOrder.
    """

    # The mock BTC price is 50 000 USDT (see _MOCK_SNAPSHOT at top of file).
    # default_cash() = 100 000 USDT (env default).
    # We seed a prior filled buy that consumed 99 995 USDT of the budget.
    # Remaining available = 100 000 - 99 995 = 5 USDT.
    # The _VALID_BUY_PAYLOAD uses qty=0.0001 BTC at 50 000 USDT = 5 USDT notional
    # PLUS ~0.5 USDT fees (taker 10bps) — total required ≈ 5.05 USDT > 5 USDT.
    # So _VALID_BUY_PAYLOAD must be REJECTED when the budget is pre-consumed.
    _PRIOR_BUY_COST = 99_995.0  # USDT consumed by the seeded prior fill

    def _seed_prior_fill(self, session) -> None:
        """Insert a filled ManualOrder + ManualFill that consumed _PRIOR_BUY_COST USDT."""
        import uuid as _uuid
        order = ManualOrder(
            order_uid=str(_uuid.uuid4()),
            terminal_session_id="seed-session",
            symbol="BTC-USDT",
            side="buy",
            order_type="limit",
            qty=1.9999,                    # qty * price ≈ 99 995 USDT
            limit_price=50_000.0,
            mode="paper",
            status="filled",
            idempotency_key="seed-fill-" + str(_uuid.uuid4()),
        )
        session.add(order)
        session.flush()
        fill = ManualFill(
            order_id=order.id,
            filled_qty=1.9999,
            filled_price=50_000.0,
            fee_quote=0.0,   # keep math simple: prior fill cost = qty * price exactly
            fee_bps=0.0,
            is_maker=False,
            slippage_bps=0.0,
        )
        session.add(fill)
        session.flush()

    def test_enforce_balance_returns_true_when_env_set(self, monkeypatch):
        """enforce_balance() must return True when TRADING_TERMINAL_ENFORCE_BALANCE=1."""
        from backend.core.trading_terminal import enforce_balance
        monkeypatch.setenv("TRADING_TERMINAL_ENFORCE_BALANCE", "1")
        assert enforce_balance() is True, (
            "enforce_balance() must return True when env var = '1'"
        )

    def test_enforce_balance_returns_false_when_env_unset(self, monkeypatch):
        """enforce_balance() must return False when TRADING_TERMINAL_ENFORCE_BALANCE=0."""
        from backend.core.trading_terminal import enforce_balance
        monkeypatch.setenv("TRADING_TERMINAL_ENFORCE_BALANCE", "0")
        assert enforce_balance() is False, (
            "enforce_balance() must return False when env var = '0'"
        )

    def test_buy_exceeding_cash_raises_insufficient_balance(self, db_session, monkeypatch):
        """A buy whose cost exceeds available cash must raise ValueError(INSUFFICIENT_BALANCE).

        Seed nearly all of the 100 000 USDT default budget as a prior filled buy,
        leaving ~5 USDT.  The standard 0.0001 BTC buy at 50 000 USDT costs 5 USDT
        notional + fees (~0.05 USDT at 10 bps), so total required > remaining
        available => guard must fire.
        """
        monkeypatch.setenv("TRADING_TERMINAL_ENFORCE_BALANCE", "1")
        # Pin default cash so the test is not affected by env overrides.
        monkeypatch.setenv("TRADING_TERMINAL_DEFAULT_CASH_USDT", "100000")
        self._seed_prior_fill(db_session)
        db_session.commit()

        from backend.core.trading_terminal import submit_paper
        with _snapshot_patch():
            with pytest.raises(ValueError, match="INSUFFICIENT_BALANCE"):
                submit_paper(
                    db_session,
                    payload={**_VALID_BUY_PAYLOAD},
                    terminal_session_id="test-balance-guard",
                    request_ip="127.0.0.1",
                    user_agent="pytest",
                    idempotency_key="balance-guard-reject-001",
                )

    def test_buy_within_cash_succeeds(self, db_session, monkeypatch):
        """A buy whose cost fits within available cash must NOT raise ValueError.

        With an empty DB (no prior fills) the full 100 000 USDT budget is
        available; a 0.0001 BTC buy at 50 000 USDT costs ≈ 5.05 USDT — well
        within budget.  The guard must remain silent.
        """
        monkeypatch.setenv("TRADING_TERMINAL_ENFORCE_BALANCE", "1")
        monkeypatch.setenv("TRADING_TERMINAL_DEFAULT_CASH_USDT", "100000")

        from backend.core.trading_terminal import submit_paper
        with _snapshot_patch():
            result = submit_paper(
                db_session,
                payload={**_VALID_BUY_PAYLOAD},
                terminal_session_id="test-balance-guard",
                request_ip="127.0.0.1",
                user_agent="pytest",
                idempotency_key="balance-guard-accept-001",
            )
        assert result.get("order") is not None, (
            "submit_paper must return an order dict when balance is sufficient; "
            f"got: {result}"
        )

    def test_paper_available_cash_decreases_after_fill(self, db_session, monkeypatch):
        """_paper_available_cash() must decrease by the cost of each filled buy.

        Seed a filled buy that cost _PRIOR_BUY_COST USDT and verify the helper
        returns default_cash() - _PRIOR_BUY_COST (no fees in the seed fill).
        """
        from backend.core.trading_terminal import _paper_available_cash, default_cash
        monkeypatch.setenv("TRADING_TERMINAL_DEFAULT_CASH_USDT", "100000")
        self._seed_prior_fill(db_session)
        db_session.commit()

        available = _paper_available_cash(db_session, mode="paper")
        expected = default_cash() - self._PRIOR_BUY_COST
        assert abs(available - expected) < 0.01, (
            f"_paper_available_cash() returned {available:.4f} USDT; "
            f"expected ≈{expected:.4f} USDT after seeding a {self._PRIOR_BUY_COST} USDT fill"
        )
