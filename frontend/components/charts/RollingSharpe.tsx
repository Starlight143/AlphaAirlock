'use client';

import { useState } from 'react';
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
import type { EquityPoint } from '@/lib/api';
import { dailyReturns, rollingSharpe } from '@/lib/derive';
import {
  AXIS_FONT_SIZE,
  AXIS_STROKE,
  CHART_COLORS,
  ChartShell,
  EmptyChart,
  GRID_STROKE,
  TOOLTIP_STYLE,
} from './_shared';

// P11-F3-07 — weekly subsample default. Plotting 252+ daily rolling-sharpe
// bars on a single chart turns into a thick wall of noise; weekly subsample
// (one bar per ISO-week) gives the operator the same shape with a quarter of
// the bars and lets the larger 90-bar window dominate as intended.
type Cadence = 'daily' | 'weekly';

type Props = {
  data: EquityPoint[];
  window?: number;
  /** P8-FIX/M-16: when true, render a 7|30|90|252 window selector chip group. */
  windowSelector?: boolean;
  /** P11-F3-07: 'weekly' (default) subsamples to one bar per ISO week. */
  cadence?: Cadence;
};

const WINDOW_CHOICES: number[] = [7, 30, 90, 252];

/**
 * Rolling Sharpe over the daily-return series, annualized by sqrt(252).
 * Rendered as a histogram-style bar chart (P6-M05) — green bars for positive
 * windows, rose for negative.
 *
 * P8-FIX/M-16: optional windowSelector exposes a chip group so the operator
 * can pick the rolling lookback inside the chart shell, without the parent
 * page needing to re-render.
 */
export default function RollingSharpe({
  data,
  window = 90,
  windowSelector = false,
  cadence = 'weekly',
}: Props) {
  const [active, setActive] = useState<number>(window);
  const effectiveWindow = windowSelector ? active : window;
  // Build (ret, ts) pairs inline so skipped bars do not shift timestamp
  // alignment — dailyReturns drops degenerate bars without carrying the
  // original data index, so data[p.i + 1] would be wrong after any skip.
  const retPairs: { ret: number; ts: string }[] = [];
  for (let i = 1; i < data.length; i++) {
    const prev = data[i - 1].equity;
    const cur = data[i].equity;
    if (!Number.isFinite(prev) || !Number.isFinite(cur) || !(prev > 1e-14)) continue;
    retPairs.push({ ret: cur / prev - 1, ts: data[i].timestamp.slice(0, 10) });
  }
  const rets = retPairs.map((p) => p.ret);
  const rs = rollingSharpe(rets, effectiveWindow);
  const allRows = rs.map((p) => ({
    ts: retPairs[p.i]?.ts ?? String(p.i),
    sharpe: Number(p.sharpe.toFixed(3)),
  }));
  // P11-F3-07 — collapse daily windows into one bar per ISO week so the
  // chart stays readable at the wider default window.
  const rows = cadence === 'weekly' ? subsampleWeekly(allRows) : allRows;
  // C-L10 — unified subtitle format. Discloses the daily √252 basis so it
  // is not confused with the contract sqrt(8760) hourly Sharpe (see lib/derive.ts:66-68).
  const subtitle = `${cadence}, ${effectiveWindow}D window · daily-approx (√252)`;
  const title =
    cadence === 'weekly'
      ? `Rolling Sharpe (${effectiveWindow}D, weekly)`
      : `Rolling Sharpe (${effectiveWindow}D)`;
  const selector = windowSelector ? (
    <div className="flex gap-0.5">
      {WINDOW_CHOICES.map((w) => (
        <button
          key={w}
          onClick={() => setActive(w)}
          className={`rounded px-1.5 py-0.5 text-[9px] font-mono uppercase tracking-wider ${
            w === effectiveWindow
              ? 'bg-cyan-500/20 text-cyan-200'
              : 'text-slate-500 hover:text-slate-300'
          }`}
        >
          {w}D
        </button>
      ))}
    </div>
  ) : null;
  if (rows.length === 0) {
    return (
      <ChartShell title={title} subtitle={subtitle}>
        {selector ? <div className="mb-1 flex justify-end">{selector}</div> : null}
        <EmptyChart message={`Need at least ${effectiveWindow + 1} daily bars.`} />
      </ChartShell>
    );
  }
  return (
    <ChartShell title={title} subtitle={subtitle}>
      {selector ? <div className="mb-1 flex justify-end">{selector}</div> : null}
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
          <YAxis stroke={AXIS_STROKE} fontSize={AXIS_FONT_SIZE} tickLine={false} />
          <ReferenceLine y={0} stroke={AXIS_STROKE} strokeDasharray="2 2" />
          <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: '#cbd5e1' }} itemStyle={{ color: '#e2e8f0' }} />
          <Bar dataKey="sharpe" isAnimationActive={false}>
            {rows.map((p, i) => (
              <Cell
                key={i}
                fill={p.sharpe >= 0 ? CHART_COLORS.pnlPositive : CHART_COLORS.pnlNegative}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </ChartShell>
  );
}

// ---------------------------------------------------------------------------
// P11-F3-07 helpers — keep last row per ISO week so the chart shows one bar
// per week regardless of how many trading days fell inside that bucket.
// ---------------------------------------------------------------------------

type SharpeRow = { ts: string; sharpe: number };

function subsampleWeekly(rows: SharpeRow[]): SharpeRow[] {
  if (rows.length === 0) return rows;
  const byWeek = new Map<string, SharpeRow>();
  for (const r of rows) {
    const key = isoWeekKey(r.ts);
    // Last-write-wins keeps the closing sharpe for each ISO week, which is
    // what an analyst expects from a weekly bar.
    if (key) byWeek.set(key, r);
  }
  return Array.from(byWeek.values());
}

function isoWeekKey(ts: string): string | null {
  // Expect 'YYYY-MM-DD' input (already sliced upstream). Anything else falls
  // back to the raw string so we never silently drop a row.
  if (!ts) return null;
  const d = new Date(`${ts.slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return ts;
  // ISO 8601 week calc — copy into a Date, shift to Thursday in current week
  // (per the ISO rule), then count Thursdays back to Jan 1.
  const target = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  const dayNr = (target.getUTCDay() + 6) % 7; // Mon=0 ... Sun=6
  target.setUTCDate(target.getUTCDate() - dayNr + 3);
  const firstThursday = new Date(Date.UTC(target.getUTCFullYear(), 0, 4));
  const week =
    1 +
    Math.round(
      ((target.getTime() - firstThursday.getTime()) / 86400000 -
        3 +
        ((firstThursday.getUTCDay() + 6) % 7)) /
        7,
    );
  const yearStr = String(target.getUTCFullYear());
  const weekStr = String(week).padStart(2, '0');
  return `${yearStr}-W${weekStr}`;
}
