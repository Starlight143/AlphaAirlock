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
import { histogram, recoveryTimes } from '@/lib/derive';
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

/** Histogram of drawdown-recovery durations in days. Orange. */
export default function RecoveryTime({ data }: Props) {
  const days = recoveryTimes(data);
  if (days.length === 0) {
    return (
      <ChartShell title="Recovery Time (days)" subtitle="trough → new high">
        <EmptyChart message="No recovered drawdowns yet." />
      </ChartShell>
    );
  }
  // Use a smaller bin count for recovery time — most strategies have only a
  // handful of distinct durations.
  const binCount = Math.min(12, Math.max(4, Math.round(Math.sqrt(days.length))));
  const h = histogram(days, binCount, /* integerLabels */ true);
  return (
    <ChartShell title="Recovery Time (days)" subtitle={`${days.length} recoveries`}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={h.bins} margin={{ top: 8, right: 12, bottom: 0, left: -10 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
          <XAxis
            dataKey="label"
            stroke={AXIS_STROKE}
            fontSize={AXIS_FONT_SIZE}
            tickLine={false}
          />
          <YAxis stroke={AXIS_STROKE} fontSize={AXIS_FONT_SIZE} tickLine={false} allowDecimals={false} />
          <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: '#cbd5e1' }} />
          <Bar dataKey="count" fill={CHART_COLORS.recovery} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </ChartShell>
  );
}
