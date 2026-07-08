'use client';

import {
  Bar,
  BarChart,
  Cell,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { EquityPoint } from '@/lib/api';
import { dailyReturns, percentile } from '@/lib/derive';
import {
  AXIS_FONT_SIZE,
  AXIS_STROKE,
  CHART_COLORS,
  ChartShell,
  EmptyChart,
  GRID_STROKE,
  TOOLTIP_STYLE,
} from './_shared';

type Props = { data: EquityPoint[] };

/**
 * Daily PnL diverging bar chart — green positive bars, red negative bars
 * centered on the zero axis. Computed client-side from the equity curve.
 */
export default function DailyPnlBars({ data }: Props) {
  const rows: { ts: string; pnl: number }[] = [];
  for (let i = 1; i < data.length; i++) {
    const prev = data[i - 1].equity;
    const cur = data[i].equity;
    if (!Number.isFinite(prev) || !Number.isFinite(cur) || !(prev > 1e-14)) continue;
    rows.push({
      ts: data[i].timestamp.slice(0, 10),
      pnl: Number(((cur / prev - 1) * 100).toFixed(3)),
    });
  }
  // P12 (L2) — subtitle now surfaces the actual max/min daily PnL so the
  // operator can size up the distribution at a glance. Falls back to a bare
  // '%' when there is no data yet.
  // P13 (C-L1) — only include "max" / "min" segments when at least one bar
  // of that sign exists. Previously an entirely-positive series rendered
  // "min 0.00%" which the operator mistook for a real datapoint.
  // P17/H3 — append p25/p50/p75 quantiles so the operator can read the
  // central tendency + IQR without opening the distribution chart.
  const pnlValues = rows.map((r) => r.pnl).filter(Number.isFinite);
  const positives = pnlValues.filter((v) => v > 0);
  const negatives = pnlValues.filter((v) => v < 0);
  const p25 = percentile(pnlValues, 0.25);
  const p50 = percentile(pnlValues, 0.50);
  const p75 = percentile(pnlValues, 0.75);
  const subtitle = (() => {
    if (pnlValues.length === 0) return '%';
    const parts: string[] = [];
    if (positives.length > 0) parts.push(`max ${Math.max(...positives).toFixed(2)}%`);
    if (negatives.length > 0) parts.push(`min ${Math.min(...negatives).toFixed(2)}%`);
    if (p25 != null && p50 != null && p75 != null) {
      parts.push(`p25 ${p25.toFixed(2)}% · p50 ${p50.toFixed(2)}% · p75 ${p75.toFixed(2)}%`);
    }
    return parts.length > 0 ? parts.join(' · ') : 'all 0%';
  })();
  if (rows.length === 0) {
    return (
      <ChartShell title="Daily PnL" subtitle={subtitle}>
        <EmptyChart />
      </ChartShell>
    );
  }
  return (
    <ChartShell title="Daily PnL" subtitle={subtitle}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} margin={{ top: 8, right: 12, bottom: 0, left: -10 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
          <XAxis
            dataKey="ts"
            stroke={AXIS_STROKE}
            fontSize={AXIS_FONT_SIZE}
            tickLine={false}
            minTickGap={48}
          />
          <YAxis stroke={AXIS_STROKE} fontSize={AXIS_FONT_SIZE} tickLine={false} tickFormatter={(v: number) => `${v.toFixed(2)}%`} />
          <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: '#cbd5e1' }} itemStyle={{ color: '#e2e8f0' }} formatter={(value) => [`${(value as number).toFixed(3)}%`, 'Daily PnL']} />
          <Bar dataKey="pnl" isAnimationActive={false}>
            {rows.map((row, i) => (
              <Cell
                key={i}
                fill={row.pnl >= 0 ? CHART_COLORS.pnlPositive : CHART_COLORS.pnlNegative}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </ChartShell>
  );
}
