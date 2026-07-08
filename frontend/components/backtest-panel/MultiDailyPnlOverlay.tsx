'use client';

import { useMemo } from 'react';
import { useQueries } from '@tanstack/react-query';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Loader2 } from 'lucide-react';
import { api, type AlphaStrategy } from '@/lib/api';
import {
  AXIS_FONT_SIZE,
  AXIS_STROKE,
  ChartShell,
  EmptyChart,
  GRID_STROKE,
  TOOLTIP_STYLE,
  strategyColor,
} from '@/components/charts/_shared';
import { sliceCurveByRange, type DateRangeKey } from './_rangeUtils';

type Props = { strategies: AlphaStrategy[]; range?: DateRangeKey };

const MAX_OVERLAY = 16;

/**
 * Multi-strategy daily PnL overlay (P6-A5).
 *
 * Stacks each selected strategy's daily PnL onto the same bar chart so the
 * operator can see (a) overall day-to-day return correlation and (b) which
 * strategy dominated each day. Uses ``stackOffset="sign"`` so positive and
 * negative contributions stack above and below the zero line independently.
 *
 * Daily PnL is derived from the equity curve (``equity[i] - equity[i-1]``);
 * does not require per-bar tape, so works for every backtested strategy.
 */
export default function MultiDailyPnlOverlay({ strategies, range = 'ALL' }: Props) {
  // Memoize `visible` so its reference is stable across re-renders.
  // Without this, `strategies.slice()` returns a new array on every render,
  // defeating the `series` useMemo below (which includes `visible` in its
  // deps array and relies on Object.is comparison).
  const visible = useMemo(() => strategies.slice(0, MAX_OVERLAY), [strategies]);

  const queries = useQueries({
    queries: visible.map((s) => ({
      queryKey: ['strategy', s.id, 'equity-only'] as const,
      queryFn: () => api.strategy(s.id),
      staleTime: 60_000,
    })),
  });

  const loading = queries.some((q) => q.isLoading);
  // P13 — compute per-day returns on the FULL curve, then keep only points
  // within each strategy's own max(timestamp)-anchored range window (see
  // _rangeUtils.sliceCurveByRange). Computing deltas before filtering means
  // the first visible bar's return is relative to its pre-cutoff predecessor,
  // so no day's PnL is silently dropped at the range boundary.

  // R5/FE-CHART-02: stable primitive dep — useQueries returns a new array reference
  // every render in TanStack Query v5, so depending on `queries` re-runs the loop
  // below on every render. dataUpdatedAt only advances on real data changes.
  const queriesKey = queries.map((q) => q.dataUpdatedAt).join(',');

  // Per-strategy daily PnL series, derived from equity_curve.
  const series = useMemo(
    () =>
      visible.map((s, i) => {
        const payload = queries[i]?.data;
        const curveAll = payload?.equity_curve ?? [];
        const slicedCurve = sliceCurveByRange(curveAll, range);
        const minT = slicedCurve.length > 0 ? slicedCurve[0].timestamp.slice(0, 10) : null;
        const points: { t: string; pnl: number }[] = [];
        for (let j = 1; j < curveAll.length; j++) {
          const t = curveAll[j].timestamp.slice(0, 10);
          if (minT != null && t < minT) continue;
          const prevEq = Number(curveAll[j - 1].equity);
          const curEq = Number(curveAll[j].equity);
          // C-M15 — normalise to per-day simple return so the y-axis is
          // dimensionless across strategies; previous code emitted absolute
          // equity deltas which made the "%" axis label a lie.
          if (!Number.isFinite(prevEq) || !Number.isFinite(curEq) || prevEq <= 1e-14) continue;
          const ret = curEq / prevEq - 1;
          if (!Number.isFinite(ret)) continue;
          points.push({ t, pnl: ret });
        }
        return {
          id: s.id,
          name: s.slug ? s.slug.slice(0, 32) : `S#${s.id} ${(s.name || '').slice(0, 24)}`,
          color: strategyColor(i),
          points,
        };
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [queriesKey, visible, range],
  );

  // Merge into timestamp-keyed wide format.
  const merged = useMemo(() => {
    const map = new Map<string, Record<string, number | string>>();
    for (const s of series) {
      for (const p of s.points) {
        if (!map.has(p.t)) map.set(p.t, { t: p.t });
        const row = map.get(p.t)!;
        row[`pnl_${s.id}`] = p.pnl;
      }
    }
    return Array.from(map.values()).sort((a, b) =>
      String(a.t).localeCompare(String(b.t)),
    );
  }, [series]);

  if (visible.length === 0) {
    return (
      <ChartShell title="Daily Return % — Per-Strategy Overlaid">
        <EmptyChart message="Select strategies above to compare daily return %." />
      </ChartShell>
    );
  }
  if (loading) {
    return (
      <ChartShell title={`Daily Return % · ${visible.length} strategies (overlaid)`} height={220}>
        <div className="flex h-full items-center justify-center text-[11px] text-slate-500">
          <Loader2 className="mr-2 h-4 w-4 animate-spin text-cyan-400" />
          Fetching equity curves…
        </div>
      </ChartShell>
    );
  }
  if (merged.length === 0) {
    return (
      <ChartShell title="Daily Return % — Per-Strategy Overlaid">
        <EmptyChart message="No equity data available for the selected strategies." />
      </ChartShell>
    );
  }

  return (
    <ChartShell
      title={`Daily Return % · ${visible.length} strategies (overlaid)`}
      subtitle={
        strategies.length > MAX_OVERLAY
          ? `${strategies.length - MAX_OVERLAY} more hidden (cap ${MAX_OVERLAY})`
          : undefined
      }
      height={240}
    >
      <ResponsiveContainer width="100%" height="100%">
        {/* C-M2 — removed stackOffset="sign" + stackId. Stacking N=16 per-day
           bars produced an aggregate that is only meaningful when every
           strategy has the same equity base. The reference screenshot shows
           the bars OVERLAID (per-strategy, side-by-side via Recharts'
           default grouping) so the operator can compare day-to-day moves
           across strategies without the misleading total height. */}
        <BarChart data={merged} margin={{ top: 8, right: 12, bottom: 0, left: -10 }}>
          <CartesianGrid stroke={GRID_STROKE} strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="t" stroke={AXIS_STROKE} tick={{ fontSize: AXIS_FONT_SIZE }} minTickGap={48} />
          <YAxis
            stroke={AXIS_STROKE}
            tick={{ fontSize: AXIS_FONT_SIZE }}
            tickFormatter={(v) => `${(Number(v) * 100).toFixed(1)}%`}
          />
          <Tooltip
            contentStyle={TOOLTIP_STYLE}
            labelStyle={{ color: '#cbd5e1' }}
            formatter={(value: number) => `${(value * 100).toFixed(2)}%`}
          />
          <Legend wrapperStyle={{ fontSize: 10, color: '#94a3b8' }} iconSize={8} />
          {series.map((s) => (
            <Bar
              key={s.id}
              dataKey={`pnl_${s.id}`}
              name={s.name}
              fill={s.color}
              isAnimationActive={false}
              fillOpacity={0.5}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </ChartShell>
  );
}
