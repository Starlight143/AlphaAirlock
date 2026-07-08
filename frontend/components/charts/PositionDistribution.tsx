'use client';

import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { TradeTapeRow } from '@/lib/api';
import { histogram } from '@/lib/derive';
import {
  AXIS_FONT_SIZE,
  AXIS_STROKE,
  CHART_COLORS,
  ChartShell,
  EmptyChart,
  GRID_STROKE,
  TOOLTIP_STYLE,
} from './_shared';

type Props = { data: TradeTapeRow[]; binCount?: number };

/**
 * Histogram of per-bar position values (P5-FE-12 #12). Cyan bars on the
 * same shell pattern as RollingSharpe / DailyPnlDistribution / etc.
 */
export default function PositionDistribution({ data, binCount = 11 }: Props) {
  if (!data || data.length === 0) {
    return (
      <ChartShell title="Position Distribution">
        <EmptyChart message="Per-bar tape required (P5-BE-04 endpoint)." />
      </ChartShell>
    );
  }
  const sigs = data.map((r) => Number(r.signal)).filter(Number.isFinite);
  const { bins, totalCount } = histogram(sigs, binCount);
  if (totalCount === 0 || bins.length === 0) {
    return (
      <ChartShell title="Position Distribution">
        <EmptyChart />
      </ChartShell>
    );
  }
  // P17/H4 — long/short/flat bias chip. Counts the raw sign of each bar so
  // the operator can read directional exposure at a glance. denom guarded
  // against the empty case even though totalCount > 0 already.
  let longCt = 0;
  let shortCt = 0;
  let flatCt = 0;
  for (const s of sigs) {
    if (s > 0) longCt++;
    else if (s < 0) shortCt++;
    else flatCt++;
  }
  const denom = sigs.length || 1;
  const pct = (n: number) => ((n / denom) * 100).toFixed(0);
  const subtitle = `${totalCount.toLocaleString()} bars · Long ${pct(longCt)}% · Short ${pct(shortCt)}% · Flat ${pct(flatCt)}%`;
  return (
    <ChartShell title="Position Distribution" subtitle={subtitle}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={bins} margin={{ top: 8, right: 12, bottom: 0, left: -10 }}>
          <CartesianGrid stroke={GRID_STROKE} strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="label"
            stroke={AXIS_STROKE}
            tick={{ fontSize: AXIS_FONT_SIZE }}
            interval={Math.max(0, Math.floor(bins.length / 8) - 1)}
          />
          <YAxis stroke={AXIS_STROKE} tick={{ fontSize: AXIS_FONT_SIZE }} allowDecimals={false} />
          <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: '#cbd5e1' }} />
          <Bar
            dataKey="count"
            fill={CHART_COLORS.position}
            isAnimationActive={false}
          />
        </BarChart>
      </ResponsiveContainer>
    </ChartShell>
  );
}
