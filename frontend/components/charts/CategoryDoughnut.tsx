'use client';

import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts';
import { CHART_COLORS, ChartShell, EmptyChart, TOOLTIP_STYLE } from './_shared';

type Slice = { name: string; value: number };

type Props = {
  slices: Slice[];
  title?: string;
  // 'doughnut' keeps the legacy hollow-center look; 'pie' renders solid for
  // the Backtest Panel aggregated category view (P5-FE-13).
  variant?: 'doughnut' | 'pie';
  // 'fraction' => slice.value is a 0..1 weight, so the tooltip renders it as a
  // percentage. 'absolute' (default) leaves raw counts/values untouched, keeping
  // existing alpha-category callers unchanged. (round-3 FE6-4)
  valueUnit?: 'fraction' | 'absolute';
};

/**
 * Alpha-category pie / doughnut chart. For a single-strategy detail view this
 * will show one slice = 100%; the same component scales to N-strategy
 * portfolios on the Backtest Panel without modification.
 */
export default function CategoryDoughnut({
  slices,
  title = 'Alpha Category',
  variant = 'doughnut',
  valueUnit = 'absolute',
}: Props) {
  const filtered = (slices ?? []).filter((s) => s.value > 0);
  if (filtered.length === 0) {
    return (
      <ChartShell title={title}>
        <EmptyChart message="Category not yet assigned — runs after Critic." />
      </ChartShell>
    );
  }
  return (
    <ChartShell title={title}>
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie
            data={filtered}
            dataKey="value"
            nameKey="name"
            cx="50%"
            cy="50%"
            innerRadius={variant === 'pie' ? '0%' : '55%'}
            outerRadius="85%"
            stroke="#020617"
            strokeWidth={2}
            isAnimationActive={false}
          >
            {filtered.map((_, i) => (
              <Cell
                key={i}
                fill={CHART_COLORS.category[i % CHART_COLORS.category.length]}
              />
            ))}
          </Pie>
          <Tooltip
            contentStyle={TOOLTIP_STYLE}
            labelStyle={{ color: '#cbd5e1' }}
            itemStyle={{ color: '#e2e8f0' }}
            formatter={
              valueUnit === 'fraction'
                ? (value: number) => [`${(value * 100).toFixed(1)}%`, '']
                : undefined
            }
          />
        </PieChart>
      </ResponsiveContainer>
    </ChartShell>
  );
}
