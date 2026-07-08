'use client';

import { useMemo } from 'react';
import {
  Bar,
  BarChart,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { FactorGraphResponse } from '@/lib/api';
import {
  AXIS_FONT_SIZE,
  AXIS_STROKE,
  CHART_COLORS,
  ChartShell,
  EmptyChart,
  TOOLTIP_STYLE,
} from '@/components/charts/_shared';
import { histogram } from '@/lib/derive';

type Props = {
  data: FactorGraphResponse | undefined;
};

/**
 * Per-node IC score histogram (P6-A12).
 *
 * Mirrors the small red/green distribution panel in the reference screenshot's
 * right rail. Negative bins (below 0) render rose; positive bins render
 * emerald — matches the "good vs bad alpha" colour grammar used across the
 * Performance grid.
 *
 * 20 bins is a sweet spot for the typical 20-200 node payload — fine-grained
 * enough to see the IC distribution shape, coarse enough to keep each bar
 * legible at sidebar width.
 */
export default function MetricDistribution({ data }: Props) {
  const points = useMemo(() => {
    if (!data) return [];
    const xs = (data.nodes ?? [])
      .map((n) => Number(n.ic_score))
      .filter((v) => Number.isFinite(v));
    if (xs.length === 0) return [];
    const h = histogram(xs, 20);
    return h.bins.map((b) => ({
      ...b,
      fill: Math.abs(b.x) < 0.01
        ? '#64748b'
        : b.x >= 0
          ? CHART_COLORS.pnlPositive
          : CHART_COLORS.pnlNegative,
    }));
  }, [data]);

  if (points.length === 0) {
    return (
      <ChartShell title="IC Histogram" height={120}>
        <EmptyChart message="No nodes yet — IC distribution unavailable." />
      </ChartShell>
    );
  }

  return (
    <ChartShell title="IC Histogram" subtitle={`${data?.nodes.length ?? 0} nodes`} height={140}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={points} margin={{ top: 4, right: 4, bottom: 0, left: -16 }}>
          <XAxis
            dataKey="label"
            stroke={AXIS_STROKE}
            tick={{ fontSize: AXIS_FONT_SIZE }}
            minTickGap={32}
          />
          <YAxis stroke={AXIS_STROKE} tick={{ fontSize: AXIS_FONT_SIZE }} />
          <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: '#cbd5e1' }} itemStyle={{ color: '#e2e8f0' }} />
          <Bar dataKey="count" isAnimationActive={false}>
            {points.map((p, i) => (
              <Cell key={i} fill={p.fill} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </ChartShell>
  );
}
