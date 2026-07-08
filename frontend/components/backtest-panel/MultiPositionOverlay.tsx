'use client';

import { useMemo } from 'react';
import { useQueries } from '@tanstack/react-query';
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  ResponsiveContainer,
  ReferenceLine,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Loader2 } from 'lucide-react';
import { api, type AlphaStrategy, type EquityPoint, type TradeTapeRow } from '@/lib/api';
import {
  AXIS_FONT_SIZE,
  AXIS_STROKE,
  ChartShell,
  EmptyChart,
  GRID_STROKE,
  TOOLTIP_STYLE,
  strategyColor,
} from '@/components/charts/_shared';
import { sliceCurveByRange, cutoffEpochFromCurve, rangeToCutoffDays, type DateRangeKey } from './_rangeUtils';

type Props = { strategies: AlphaStrategy[]; range?: DateRangeKey };

const MAX_OVERLAY = 16;

type PosPoint = { t: string; sign: number };

/**
 * Multi-strategy position overlay (P6-A5).
 *
 * For each selected strategy we prefer the per-bar tape (api.strategyTrades)
 * when `raw_backtest.per_bar_available` is true — that gives us the real
 * `direction` (long / short / flat) emitted by the engine. When the per-bar
 * tape is unavailable we fall back to the historical approximation of using
 * the sign of the daily equity delta. The per-strategy legend chip is
 * suffixed with `(approx)` whenever the fallback is in play so the operator
 * can tell at a glance which series is authoritative.
 *
 * Colors stay per-strategy from the shared palette to keep the legend
 * mapping consistent with MultiEquityOverlay / MultiDailyPnlOverlay.
 */
export default function MultiPositionOverlay({ strategies, range = 'ALL' }: Props) {
  // Memoize `visible` so its reference is stable across re-renders.
  // Without this, `strategies.slice()` returns a new array on every render,
  // defeating the `series` useMemo calls below (which include `visible` in
  // their deps arrays and rely on Object.is comparison).
  const visible = useMemo(() => strategies.slice(0, MAX_OVERLAY), [strategies]);

  // 1) Per-strategy equity (always fetched — needed for the fallback path
  //    and for the range slice anchor when the per-bar tape is missing).
  const equityQueries = useQueries({
    queries: visible.map((s) => ({
      queryKey: ['strategy', s.id, 'equity-only'] as const,
      queryFn: () => api.strategy(s.id),
      staleTime: 60_000,
    })),
  });

  // 2) Per-strategy per-bar tape — only enabled when the backend has flagged
  //    `per_bar_available`. Shared queryKey with PerformanceGrid so any
  //    pre-warmed cache is reused (no duplicate roundtrip).
  const tapeQueries = useQueries({
    queries: visible.map((s) => ({
      queryKey: ['strategy-trades', s.id, false] as const,
      queryFn: () => api.strategyTrades(s.id, { limit: 2000, nonzeroOnly: false }),
      enabled: !!s.raw_backtest?.per_bar_available,
      staleTime: 60_000,
      retry: false,
    })),
  });

  const loading =
    equityQueries.some((q) => q.isLoading) || tapeQueries.some((q) => q.isLoading);

  // R5/FE-CHART-03: stable sentinels — useQueries returns new array refs every
  // render in TanStack Query v5, so depending on equityQueries/tapeQueries re-runs
  // the per-strategy classification on every render. dataUpdatedAt advances only on
  // real data changes.
  const equityUpdatedKey = equityQueries.map((q) => q.dataUpdatedAt).join(',');
  const tapeUpdatedKey = tapeQueries.map((q) => q.dataUpdatedAt).join(',');

  const series = useMemo(() => {
    return visible.map((s, i) => {
      const equityPayload = equityQueries[i]?.data;
      const tapeRows: TradeTapeRow[] = tapeQueries[i]?.data?.rows ?? [];
      const hasTape = !!s.raw_backtest?.per_bar_available && tapeRows.length > 0;
      const curveAll: EquityPoint[] = equityPayload?.equity_curve ?? [];
      const curve = sliceCurveByRange(curveAll, range);

      let points: PosPoint[];
      if (hasTape) {
        // Real direction path. Map 'long'→1, 'short'→-1, 'flat'/missing→0.
        // Fall back to sign(signal) if `direction` is absent on the row.
        points = tapeRows.map((row) => {
          const t = (row.start_time || '').slice(0, 10);
          let sign = 0;
          if (row.direction === 'long') sign = 1;
          else if (row.direction === 'short') sign = -1;
          else if (row.direction === 'flat') sign = 0;
          else {
            const sig = Number(row.signal) || 0;
            sign = sig > 0 ? 1 : sig < 0 ? -1 : 0;
          }
          return { t, sign };
        });
        // If a range chip is set, filter by the same anchor as the equity slice.
        // When curve is empty (partial/missing backend data), derive the cutoff
        // from the tape rows themselves so the range chip still takes effect.
        const days = rangeToCutoffDays(range);
        if (days != null && points.length > 0) {
          const tapeAsTs = points.map((p) => ({ timestamp: p.t + 'T00:00:00Z' }));
          const cutoffMs = cutoffEpochFromCurve(tapeAsTs, days);
          if (cutoffMs != null) {
            points = points.filter((p) => new Date(p.t).getTime() >= cutoffMs);
          } else if (curve.length > 0) {
            const minT = curve[0].timestamp.slice(0, 10);
            points = points.filter((p) => p.t >= minT);
          }
        }
      } else {
        // Approximation path: sign of daily equity delta.
        const proxy: PosPoint[] = [];
        for (let j = 1; j < curve.length; j++) {
          const t = curve[j].timestamp.slice(0, 10);
          // P34: skip bars with non-finite equity instead of coercing null→0,
          // which would otherwise read as a permanently Flat position.
          const prevEq = Number(curve[j - 1].equity);
          const curEq = Number(curve[j].equity);
          if (!Number.isFinite(prevEq) || !Number.isFinite(curEq)) continue;
          const delta = curEq - prevEq;
          const sign = Math.abs(delta) < 1e-6 ? 0 : delta > 0 ? 1 : -1;
          proxy.push({ t, sign });
        }
        points = proxy;
      }

      const baseName = s.slug ? s.slug.slice(0, 32) : `S#${s.id} ${(s.name || '').slice(0, 24)}`;
      const name = hasTape ? baseName : `${baseName} (approx)`;
      return {
        id: s.id,
        name,
        // C-M1: When only one strategy is visible we switch to a direction-keyed
        // coloring downstream (see <Area> render). The per-strategy palette
        // color is kept as a fallback for the legend chip.
        color: strategyColor(i),
        points,
        approx: !hasTape,
      };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, equityUpdatedKey, tapeUpdatedKey, range]);

  const anyApprox = series.some((s) => s.approx);

  const merged = useMemo(() => {
    const map = new Map<string, Record<string, number | string>>();
    for (const s of series) {
      for (const p of s.points) {
        if (!map.has(p.t)) map.set(p.t, { t: p.t });
        const row = map.get(p.t)!;
        row[`pos_${s.id}`] = p.sign;
      }
    }
    return Array.from(map.values()).sort((a, b) =>
      String(a.t).localeCompare(String(b.t)),
    );
  }, [series]);

  if (visible.length === 0) {
    return (
      <ChartShell title="Position Change — Per-Strategy Overlay">
        <EmptyChart message="Select strategies above to overlay their position direction." />
      </ChartShell>
    );
  }
  if (loading) {
    return (
      <ChartShell title={`Position Change · ${visible.length} strategies`} height={200}>
        <div className="flex h-full items-center justify-center text-[11px] text-slate-500">
          <Loader2 className="mr-2 h-4 w-4 animate-spin text-cyan-400" />
          Fetching position series…
        </div>
      </ChartShell>
    );
  }
  if (merged.length === 0) {
    return (
      <ChartShell title="Position Change — Per-Strategy Overlay">
        <EmptyChart message="No per-bar tape or equity for selected strategies." />
      </ChartShell>
    );
  }

  return (
    <ChartShell
      title={`Position Change · ${visible.length} strategies`}
      subtitle={
        anyApprox
          ? 'Per-bar tape used when available; sign-of-daily-delta fallback for (approx) series'
          : 'Per-bar tape · long / short / flat'
      }
      height={220}
    >
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={merged} margin={{ top: 8, right: 12, bottom: 0, left: -10 }}>
          {/* C-H5 — `<defs>` MUST come BEFORE any Area that references
             `url(#posSignFill)`. The previous render placed defs after the
             Area consumer, which under React StrictMode and certain Recharts
             versions resolves the gradient URL to nothing on first paint and
             leaves the single-series position panel blank. */}
          <defs>
            <linearGradient id="posSignFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#06B6D4" stopOpacity={0.6} />
              <stop offset="50%" stopColor="#94a3b8" stopOpacity={0.25} />
              <stop offset="100%" stopColor="#F43F5E" stopOpacity={0.6} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke={GRID_STROKE} strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="t" stroke={AXIS_STROKE} tick={{ fontSize: AXIS_FONT_SIZE }} minTickGap={48} />
          <YAxis
            stroke={AXIS_STROKE}
            tick={{ fontSize: AXIS_FONT_SIZE }}
            domain={[-1.1, 1.1]}
            ticks={[-1, 0, 1]}
            tickFormatter={(v) => (v > 0 ? 'L' : v < 0 ? 'S' : 'F')}
          />
          <ReferenceLine y={0} stroke={AXIS_STROKE} strokeDasharray="2 2" />
          <Tooltip
            contentStyle={TOOLTIP_STYLE}
            labelStyle={{ color: '#cbd5e1' }}
            formatter={(value: number, name: string): [string, string] => {
              const label = value > 0 ? 'Long' : value < 0 ? 'Short' : 'Flat';
              return [label, name];
            }}
          />
          {series.length > 1 && (
            <Legend wrapperStyle={{ fontSize: 10, color: '#94a3b8' }} iconSize={8} />
          )}
          {series.map((s) => {
            // C-M1: with a single visible series the per-strategy palette
            // collapses to one tone, which makes long/short/flat impossible
            // to read at a glance. Switch to direction-based fill (cyan long,
            // rose short, slate flat) — Recharts can't paint per-point fill
            // on a step Area, so use the long color as the fill base and
            // overlay rose / slate via stacked sibling Areas keyed to the
            // sign value.
            if (series.length === 1) {
              return (
                <Area
                  key={s.id}
                  type="step"
                  dataKey={`pos_${s.id}`}
                  name={s.name}
                  // Step area uses color based on positive/negative cleanly
                  // via `stroke` for the line and `fill` via the value sign:
                  // Recharts paints the area between the line and y=0, so a
                  // single stroke color + value-keyed gradient gives the
                  // long/short/flat read.
                  stroke="#06B6D4"
                  fill="url(#posSignFill)"
                  fillOpacity={0.5}
                  strokeWidth={1.5}
                  isAnimationActive={false}
                  connectNulls
                />
              );
            }
            return (
              <Area
                key={s.id}
                type="step"
                dataKey={`pos_${s.id}`}
                name={s.name}
                stroke={s.color}
                fill={s.color}
                fillOpacity={0.15}
                strokeWidth={1}
                isAnimationActive={false}
                connectNulls
              />
            );
          })}
        </ComposedChart>
      </ResponsiveContainer>
      {series.length === 1 && (
        <div className="mt-1 flex flex-wrap items-center gap-3 px-1 text-[10px] text-slate-400">
          <span className="inline-flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-sm bg-cyan-500" />
            Long
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-sm bg-slate-400" />
            Flat
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-sm bg-rose-500" />
            Short
          </span>
        </div>
      )}
    </ChartShell>
  );
}
