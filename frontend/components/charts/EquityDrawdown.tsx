'use client';

import { useId } from 'react';
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Label,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { EquityPoint } from '@/lib/api';
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

// C-M15 — hoist the equity-axis domain accessors so Recharts sees the same
// function identity between renders. Inline arrow functions caused a
// re-render flicker because the axis registered a new domain on every paint.
const EQ_DOMAIN_MIN = (dataMin: number): number => Math.min(0, dataMin);
const EQ_DOMAIN_MAX = (dataMax: number): number => Math.max(0.01, dataMax);

/**
 * Reference equity + drawdown overlay (white line + red underwater).
 * One panel, two y-axes — matches the demo's crosshair-synced view.
 */
export default function EquityDrawdown({ data }: Props) {
  // F12-4 — SVG gradient IDs must be unique per document. useId() produces a
  // stable, per-instance ID so multiple simultaneous EquityDrawdown mounts
  // (e.g. backtest panel + paper-trade route pre-rendered in the same layout)
  // do not steal each other's gradient definition.
  const uid = useId();
  const gradId = `eqGrad-${uid.replace(/:/g, '')}`;

  // P13 — switched to the fraction convention (equity - 1, drawdown as raw
  // negative fraction) to match MultiEquityOverlay and the reference video.
  // The y-axis now reads "0.05" for +5% growth above base; tooltips and
  // tickFormatters do the % presentation. Keeps EquityDrawdown consistent
  // with every other equity consumer in the codebase.
  //
  // C-H1 — backend's daily resampler emits null drawdown for the first
  // sample of brand-new strategies; raw `.toFixed(4)` on undefined throws and
  // unmounts the entire Performance tab. Coerce both columns through
  // Number.isFinite() and fall back to 0 for the affected leading bars.
  const rows = (data ?? []).map((p) => {
    const eqRaw = Number(p.equity);
    const ddRaw = Number(p.drawdown);
    const eq = Number.isFinite(eqRaw) ? eqRaw - 1 : 0;
    const dd = Number.isFinite(ddRaw) ? ddRaw : 0;
    return {
      ts: (p.timestamp || '').slice(0, 10),
      equity: Number.isFinite(eq) ? Number(eq.toFixed(4)) : 0,
      drawdown: Number.isFinite(dd) ? Number(dd.toFixed(4)) : 0,
    };
  });
  if (rows.length === 0) {
    return (
      <ChartShell title="Equity Curve & Drawdown" subtitle="growth above starting capital">
        <EmptyChart message="No equity curve yet — backtest has not run for this strategy." />
      </ChartShell>
    );
  }
  return (
    <ChartShell title="Equity Curve & Drawdown" subtitle="growth above starting capital" height={200}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={rows} margin={{ top: 8, right: 12, bottom: 0, left: -10 }}>
          <defs>
            <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor={CHART_COLORS.equity} stopOpacity={0.85} />
              <stop offset="95%" stopColor={CHART_COLORS.equity} stopOpacity={0.05} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
          <XAxis
            dataKey="ts"
            stroke={AXIS_STROKE}
            fontSize={AXIS_FONT_SIZE}
            tickLine={false}
            minTickGap={48}
          />
          <YAxis
            yAxisId="eq"
            stroke={AXIS_STROKE}
            fontSize={AXIS_FONT_SIZE}
            tickLine={false}
            domain={[EQ_DOMAIN_MIN, EQ_DOMAIN_MAX]}
            tickFormatter={(v) => `${(Number(v) * 100).toFixed(1)}%`}
          >
            <Label value="Eq" angle={-90} position="insideLeft" offset={14} style={{ fill: AXIS_STROKE, fontSize: 9 }} />
          </YAxis>
          <YAxis
            yAxisId="dd"
            orientation="right"
            stroke={CHART_COLORS.drawdown}
            fontSize={AXIS_FONT_SIZE}
            tickLine={false}
            domain={[(dataMin: number) => Math.min(dataMin, -0.001), 0]}
            tickFormatter={(v) => `${(Number(v) * 100).toFixed(1)}%`}
          >
            <Label value="DD" angle={90} position="right" offset={22} style={{ fill: CHART_COLORS.drawdown, fontSize: 9 }} />
          </YAxis>
          <Tooltip
            contentStyle={TOOLTIP_STYLE}
            labelStyle={{ color: '#cbd5e1' }}
            formatter={(value: number) => `${(value * 100).toFixed(2)}%`}
          />
          <Area
            yAxisId="eq"
            type="monotone"
            dataKey="equity"
            name="Eq"
            stroke={CHART_COLORS.equity}
            strokeWidth={1.6}
            fill={`url(#${gradId})`}
            isAnimationActive={false}
          />
          <Line
            yAxisId="dd"
            type="monotone"
            dataKey="drawdown"
            name="DD"
            stroke={CHART_COLORS.drawdown}
            strokeWidth={1.2}
            dot={false}
            isAnimationActive={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </ChartShell>
  );
}
