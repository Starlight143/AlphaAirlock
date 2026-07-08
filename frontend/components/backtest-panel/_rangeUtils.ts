import type { EquityPoint } from '@/lib/api';
import type { DateRangeKey } from './ChartsToolbar';

/**
 * Map a DateRangeKey chip to a calendar-day count, or null for ALL/CUSTOM
 * (no client-side slice). CUSTOM is a no-op here until explicit start/end
 * pickers ship.
 *
 * Single source of truth — replaces the per-component duplicates that used
 * to live in MultiEquityOverlay / MultiDailyPnlOverlay / MultiPositionOverlay
 * / CombinedChart.
 */
export function rangeToCutoffDays(range: DateRangeKey | undefined): number | null {
  switch (range) {
    case '1W': return 7;
    case '1M': return 30;
    case '3M': return 90;
    case '6M': return 180;
    case '1Y': return 365;
    case '5Y': return 1825;
    default:   return null;
  }
}

/**
 * P13 — Compute an ISO cutoff anchored to the LAST timestamp of the curve,
 * NOT wall-clock `new Date()`. Backtests can be months or years old; using
 * the wall-clock cutoff would slice every selected strategy down to zero
 * bars and render an empty chart whenever the operator picks a short range.
 *
 * Returns null when `days` is null (caller should skip filtering) OR when
 * the curve is empty (no anchor available).
 */
export function cutoffFromCurve(
  curve: { timestamp: string }[] | undefined,
  days: number | null,
): string | null {
  if (days == null) return null;
  if (!curve || curve.length === 0) return null;
  let maxTs = curve[0].timestamp;
  for (let i = 1; i < curve.length; i++) {
    if (curve[i].timestamp > maxTs) maxTs = curve[i].timestamp;
  }
  const d = new Date(maxTs);
  if (Number.isNaN(d.getTime())) return null;
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString();
}

/**
 * P13 — same anchor logic as cutoffFromCurve, but returns the cutoff as
 * epoch milliseconds. Comparing epoch millis (rather than lexicographic ISO
 * strings) removes coupling to the exact backend timestamp rendering
 * ('...Z' vs '...000Z') so the date-range slice stays correct even if the
 * backend ever emits fractional seconds or a differently-padded format.
 * Returns null when days is null or the curve is empty/unparseable.
 */
export function cutoffEpochFromCurve(
  curve: { timestamp: string }[] | undefined,
  days: number | null,
): number | null {
  if (days == null) return null;
  if (!curve || curve.length === 0) return null;
  // F11-4 fix: parse each timestamp before using it in the max search so that
  // a malformed entry (whose string representation sorts lexicographically
  // above all valid ISO timestamps) cannot corrupt the anchor computation.
  let maxMs: number | null = null;
  for (let i = 0; i < curve.length; i++) {
    const ms = new Date(curve[i].timestamp).getTime();
    if (Number.isNaN(ms)) continue; // skip unparseable entries
    if (maxMs === null || ms > maxMs) maxMs = ms;
  }
  if (maxMs === null) return null; // no parseable timestamps at all
  const d = new Date(maxMs);
  d.setUTCDate(d.getUTCDate() - days);
  return d.getTime();
}

/**
 * P13 — per-series convenience that yields a cutoff anchored to the
 * supplied curve. Each overlay computes its own cutoff per series so a
 * stale strategy (e.g. backtest ran 6 months ago) doesn't silently vanish
 * when a fresher strategy is co-selected.
 */
export function sliceCurveByRange<T extends { timestamp: string }>(
  curve: T[] | undefined,
  range: DateRangeKey | undefined,
): T[] {
  const days = rangeToCutoffDays(range);
  if (days == null || !curve || curve.length === 0) return curve ?? [];
  const cutoffMs = cutoffEpochFromCurve(curve, days);
  if (cutoffMs == null) return curve;
  return curve.filter((p) => {
    const t = new Date(p.timestamp).getTime();
    // F11-4 fix: drop points with unparseable timestamps rather than
    // silently passing them through the range gate.
    return Number.isNaN(t) ? false : t >= cutoffMs;
  });
}

// Re-export the shared type for callers that need the DateRangeKey union
// without importing from the chrome-heavy ChartsToolbar module.
export type { DateRangeKey };
