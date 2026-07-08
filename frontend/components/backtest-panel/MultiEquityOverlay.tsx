'use client';

import { useMemo } from 'react';
import { useQueries } from '@tanstack/react-query';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
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
import { normalizeToBase100 } from '@/lib/derive';
import { sliceCurveByRange, type DateRangeKey } from './_rangeUtils';

type Props = { strategies: AlphaStrategy[]; range?: DateRangeKey };

const MAX_OVERLAY = 16;

/**
 * Multi-strategy equity overlay (P5-FE-13). Each selected strategy gets a
 * distinct color from the 16-color palette. Capped at 16 series to keep
 * Recharts performant — operator must narrow selection if they have more.
 *
 * Fetches per-strategy equity_curve via N independent useQueries (cached
 * cross-route via TanStack Query so /strategies/[id] hits are deduped).
 */
export default function MultiEquityOverlay({ strategies, range = 'ALL' }: Props) {
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
  // P13 — slice each strategy's equity_curve to the chart-wide date range,
  // anchored per series to its own max(timestamp). This guarantees a
  // historical backtest (e.g. one that ended 6 months ago) still shows the
  // expected window when "1W" is picked, instead of vanishing because the
  // wall-clock cutoff is ahead of every datapoint.
  // C-M3 — normalise each per-series equity curve to base-100 BEFORE merge.
  // The previous code shipped raw `p.equity` (e.g. starting at 1.0 for a
  // base-1 strategy or $10_000 for a USD-scaled one) and assumed every
  // series shared the same base when computing `((v - 1) * 100)%` on the
  // y-axis. Mixed-base strategies (which happens once the merged pipeline
  // ships) produced absurd ticks. normalizeToBase100 makes every series
  // start at 100 so the y-axis label `${v-100}%` is honest for any input.
  // R5/FE-CHART-01: stable sentinel — useQueries returns a NEW array every render
  // in TanStack Query v5, so listing `queries` in the deps defeats memoization and
  // re-runs the O(16) normalize on every parent render. dataUpdatedAt advances only
  // when a query's data actually changes.
  const queriesUpdatedAt = queries.map((q) => q.dataUpdatedAt).join(',');

  const series = useMemo(
    () =>
      visible.map((s, i) => {
        const payload = queries[i]?.data;
        const curveAll = payload?.equity_curve ?? [];
        const curve = sliceCurveByRange(curveAll, range);
        const normalized = normalizeToBase100(curve);
        return {
          id: s.id,
          name: s.slug ? s.slug.slice(0, 32) : `S#${s.id} ${(s.name || '').slice(0, 24)}`,
          color: strategyColor(i),
          points: normalized.map((p) => ({ t: p.ts.slice(0, 10), [`eq_${s.id}`]: p.v })),
        };
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [queriesUpdatedAt, visible, range],
  );

  // Merge per-strategy series into one timestamp-keyed array.
  const merged = useMemo(() => {
    const map = new Map<string, Record<string, number | string>>();
    for (const s of series) {
      for (const p of s.points) {
        const t = p.t as string;
        if (!map.has(t)) map.set(t, { t });
        const row = map.get(t)!;
        for (const [k, v] of Object.entries(p)) {
          if (k !== 't') row[k] = v as number;
        }
      }
    }
    return Array.from(map.values()).sort((a, b) =>
      String(a.t).localeCompare(String(b.t)),
    );
  }, [series]);

  if (visible.length === 0) {
    return (
      <ChartShell title="Equity Curve & Drawdown — Per-Strategy Overlay">
        <EmptyChart message="Select strategies in the table above to overlay their equity curves." />
      </ChartShell>
    );
  }
  if (loading) {
    return (
      <ChartShell title={`Equity Curve & Drawdown · ${visible.length} strategies`} height={260}>
        <div className="flex h-full items-center justify-center text-[11px] text-slate-500">
          <Loader2 className="mr-2 h-4 w-4 animate-spin text-cyan-400" />
          Fetching equity curves for {visible.length} strategies…
        </div>
      </ChartShell>
    );
  }

  return (
    <ChartShell
      title={`Equity Curve & Drawdown · ${visible.length} strategies`}
      subtitle={
        strategies.length > MAX_OVERLAY
          ? `${strategies.length - MAX_OVERLAY} more hidden (cap ${MAX_OVERLAY})`
          : undefined
      }
      height={280}
    >
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={merged} margin={{ top: 8, right: 12, bottom: 0, left: -10 }}>
          <CartesianGrid stroke={GRID_STROKE} strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="t"
            stroke={AXIS_STROKE}
            tick={{ fontSize: AXIS_FONT_SIZE }}
            minTickGap={48}
          />
          <YAxis
            stroke={AXIS_STROKE}
            tick={{ fontSize: AXIS_FONT_SIZE }}
            tickFormatter={(v) => `${(Number(v) - 100).toFixed(1)}%`}
            domain={['auto', 'auto']}
          />
          <Tooltip
            contentStyle={TOOLTIP_STYLE}
            labelStyle={{ color: '#cbd5e1' }}
            formatter={(value: number) => `${(Number(value) - 100).toFixed(2)}%`}
          />
          <Legend
            wrapperStyle={{ fontSize: 10, color: '#94a3b8' }}
            iconSize={8}
          />
          {series.filter((s) => s.points.length > 0).map((s) => (
            <Line
              key={s.id}
              type="monotone"
              dataKey={`eq_${s.id}`}
              name={s.name}
              stroke={s.color}
              strokeWidth={1.3}
              dot={false}
              isAnimationActive={false}
              connectNulls
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </ChartShell>
  );
}
