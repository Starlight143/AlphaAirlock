"""R6/PERF-17 regression coverage for pipeline_analytics.occupancy().

occupancy() was refactored from a full StageTransition table scan + Python
dedup to a MAX(transitioned_at)-per-strategy GROUP BY subquery + join. These
tests lock in the behavior the refactor MUST preserve (there was previously no
test exercising occupancy() at all):

  1. The LATEST non-excluded transition per strategy drives entered_at/age.
  2. Excluded actors (backfill/system/operator) are ignored by the subquery.
  3. A strategy with no non-excluded transition falls back to updated_at.

A regression in the subquery (missing actor filter, wrong join, picking the
oldest instead of newest) would flip one of these assertions.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core.database import AlphaStrategy, Base, StageTransition
from backend.core import pipeline_analytics as pa


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    Base.metadata.drop_all(engine)


def _median_age(out, status):
    for row in out["per_status"]:
        if row["status"] == status:
            return row["median_age_minutes"]
    return None


class TestOccupancyLatestTransition:
    def test_latest_nonexcluded_transition_drives_age(self, db_session, monkeypatch):
        """entered_at = newest non-excluded transition (10m), not the older
        pipeline row (100m) and not the newer operator row (1m, excluded)."""
        monkeypatch.setenv("PIPELINE_STUCK_MINUTES", "1440")
        now = datetime.now(timezone.utc)
        s1 = AlphaStrategy(
            name="s1", status="BACKTESTING", updated_at=now - timedelta(minutes=200)
        )
        db_session.add(s1)
        db_session.flush()
        db_session.add_all([
            StageTransition(strategy_id=s1.id, to_status="BACKTESTING",
                            transitioned_at=now - timedelta(minutes=100), actor="orchestrator"),
            StageTransition(strategy_id=s1.id, to_status="BACKTESTING",
                            transitioned_at=now - timedelta(minutes=10), actor="orchestrator"),
            StageTransition(strategy_id=s1.id, to_status="BACKTESTING",
                            transitioned_at=now - timedelta(minutes=1), actor="operator"),
        ])
        db_session.commit()

        pa._CACHE.clear()
        out = pa.occupancy(db_session)
        age = _median_age(out, "BACKTESTING")
        assert age is not None, f"BACKTESTING missing from per_status: {out['per_status']}"
        assert 9.5 <= age <= 11.5, (
            f"expected ~10m from the latest non-excluded transition, got {age} "
            "(100m => oldest picked; ~1m => excluded operator row leaked into the subquery)"
        )

    def test_excluded_only_falls_back_to_updated_at(self, db_session, monkeypatch):
        """A strategy whose only transition is an excluded actor must fall back
        to updated_at (50m), NOT use the excluded operator row (2m)."""
        monkeypatch.setenv("PIPELINE_STUCK_MINUTES", "1440")
        now = datetime.now(timezone.utc)
        s2 = AlphaStrategy(
            name="s2", status="CODE_GEN", updated_at=now - timedelta(minutes=50)
        )
        db_session.add(s2)
        db_session.flush()
        db_session.add(
            StageTransition(strategy_id=s2.id, to_status="CODE_GEN",
                            transitioned_at=now - timedelta(minutes=2), actor="operator")
        )
        db_session.commit()

        pa._CACHE.clear()
        out = pa.occupancy(db_session)
        age = _median_age(out, "CODE_GEN")
        assert age is not None, f"CODE_GEN missing from per_status: {out['per_status']}"
        assert 49.0 <= age <= 52.0, (
            f"expected ~50m fallback to updated_at, got {age} "
            "(~2m => excluded operator row was wrongly used as entered_at)"
        )
