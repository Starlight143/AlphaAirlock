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
import type { EquityPoint } from '@/lib/api';
import { drawdownSeries, histogram } from '@/lib/derive';
import {
  AXIS_FONT_SIZE,
  AXIS_STROKE,
  CHART_COLORS,
  ChartShell,
  EmptyChart,
  GRID_STROKE,
  TOOLTIP_STYLE,
} from './_shared';

type Props = { data: EquityPoint[]; bins?: number };

/** Histogram of drawdown magnitudes (already negative). Red. */
export default function DrawdownDistribution({ data, bins = 25 }: Props) {
  // Filter to genuine drawdown bars only (dd < 0). Bars where equity equals
  // the running maximum produce drawdown = 0.0 exactly (engine.py:131), and
  // resampled non-underwater bars are explicitly filled to 0.0 (engine.py:450).
  // In a typical backtest the strategy is at all-time-high the majority of the
  // time, so without this filter ~80 %+ of histogram mass concentrates in the
  // zero bin and the actual drawdown shape is compressed into a barely visible
  // tail — directly contradicting the chart's stated purpose.
  // Threshold -1e-9 (not strict 0) absorbs IEEE 754 rounding in the
  // equity / running_max subtraction.
  const dd = drawdownSeries(data).filter((v) => v < -1e-9).map((v) => v * 100);
  const h = histogram(dd, bins);
  if (dd.length === 0) {
    return (
      <ChartShell title="Drawdown Distribution" subtitle="% bins">
        <EmptyChart />
      </ChartShell>
    );
  }
  return (
    <ChartShell title="Drawdown Distribution" subtitle={`${h.totalCount} drawdown samples`}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={h.bins} margin={{ top: 8, right: 12, bottom: 0, left: -10 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
          <XAxis
            dataKey="label"
            stroke={AXIS_STROKE}
            fontSize={AXIS_FONT_SIZE}
            tickLine={false}
            interval={Math.max(0, Math.floor(h.bins.length / 6) - 1)}
          />
          <YAxis stroke={AXIS_STROKE} fontSize={AXIS_FONT_SIZE} tickLine={false} allowDecimals={false} />
          <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: '#cbd5e1' }} />
          <Bar dataKey="count" fill={CHART_COLORS.ddDist} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </ChartShell>
  );
}
