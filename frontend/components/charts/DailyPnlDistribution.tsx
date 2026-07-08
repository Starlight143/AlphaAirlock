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
import { dailyReturns, histogram } from '@/lib/derive';
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

/** Histogram of daily PnL %. Blue. */
export default function DailyPnlDistribution({ data, bins = 30 }: Props) {
  const rets = dailyReturns(data).map((v) => v * 100);
  const h = histogram(rets, bins);
  if (h.totalCount === 0) {
    return (
      <ChartShell title="Daily PnL Distribution" subtitle="% bins">
        <EmptyChart />
      </ChartShell>
    );
  }
  return (
    <ChartShell title="Daily PnL Distribution" subtitle={`${h.totalCount} samples`}>
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
          <Bar dataKey="count" fill={CHART_COLORS.pnlDist} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </ChartShell>
  );
}
