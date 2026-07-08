'use client';

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { TradeTapeRow } from '@/lib/api';
import {
  AXIS_FONT_SIZE,
  AXIS_STROKE,
  CHART_COLORS,
  ChartShell,
  EmptyChart,
  GRID_STROKE,
  TOOLTIP_STYLE,
} from './_shared';

type Props = { data: TradeTapeRow[] };

/**
 * Per-bar position signal over time — P5-FE-12 #11 + P8-FIX/H-10 colour split.
 *
 * Long bars (signal > 0) render cyan, short bars (signal < 0) render rose,
 * flat bars (≈ 0) render slate. Mirrors the reference video's "one glance
 * tells you long-vs-short bias" position panel.
 *
 * For series longer than 1500 bars we downsample by stride so tooltips stay
 * responsive; the visual shape is preserved.
 */
export default function PositionChart({ data }: Props) {
  if (!data || data.length === 0) {
    return (
      <ChartShell title="Position (Signal)">
        <EmptyChart message="Per-bar signal will appear once the trade tape is generated." />
      </ChartShell>
    );
  }
  const points = downsample(data, 1500).map((r) => {
    const sig = Number(r.signal) || 0;
    return {
      t: r.start_time.slice(0, 10),
      signal: Number(sig.toFixed(4)),
      direction: r.direction ?? (sig > 0 ? 'long' : sig < 0 ? 'short' : 'flat'),
    };
  });
  return (
    <ChartShell title="Position (Signal)" subtitle={`${data.length.toLocaleString()} bars`}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={points} margin={{ top: 8, right: 12, bottom: 0, left: -10 }}>
          <CartesianGrid stroke={GRID_STROKE} strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="t"
            stroke={AXIS_STROKE}
            tick={{ fontSize: AXIS_FONT_SIZE }}
            minTickGap={40}
          />
          <YAxis
            stroke={AXIS_STROKE}
            tick={{ fontSize: AXIS_FONT_SIZE }}
            domain={[-1.1, 1.1]}
            tickFormatter={(v) => v.toFixed(1)}
          />
          <ReferenceLine y={0} stroke={AXIS_STROKE} strokeDasharray="2 2" />
          <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: '#cbd5e1' }} itemStyle={{ color: '#e2e8f0' }} />
          <Bar dataKey="signal" isAnimationActive={false}>
            {points.map((p, i) => (
              <Cell
                key={i}
                fill={
                  p.direction === 'long'
                    ? CHART_COLORS.positionLong
                    : p.direction === 'short'
                    ? CHART_COLORS.positionShort
                    : CHART_COLORS.positionFlat
                }
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </ChartShell>
  );
}

function downsample<T>(arr: T[], target: number): T[] {
  if (arr.length <= target) return arr;
  const stride = Math.ceil(arr.length / target);
  const out: T[] = [];
  for (let i = 0; i < arr.length; i += stride) out.push(arr[i]);
  return out;
}
