'use client';

import { useMemo } from 'react';
import type { EquityPoint } from '@/lib/api';
import { monthlyReturns } from '@/lib/derive';
import { ChartShell, EmptyChart } from './_shared';

type Props = { data: EquityPoint[] };

// C-M1 — 3-letter labels disambiguate Jan/Jun/Jul (all 'J'), Mar/May (both
// 'M'), Apr/Aug (both 'A'). Matches reference screenshot 19-56-01.
const MONTH_LABELS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/**
 * 3-year-style calendar heatmap of monthly returns (P5-FE-11 #10).
 *
 * Symmetric color scale clamped to ±10% — wins are emerald, losses rose,
 * absent months render as bg-slate-950 so the grid stays rectangular even
 * for brand-new strategies.
 */
export default function MonthlyReturnsHeatmap({ data }: Props) {
  const rows = useMemo(() => {
    const months = monthlyReturns(data);
    if (months.length === 0) return [] as { year: number; cells: (number | null)[] }[];
    const minYear = months[0].year;
    const maxYear = months[months.length - 1].year;
    const grid: { year: number; cells: (number | null)[] }[] = [];
    for (let y = minYear; y <= maxYear; y++) {
      grid.push({ year: y, cells: new Array(12).fill(null) });
    }
    for (const m of months) {
      const yRow = grid.find((r) => r.year === m.year);
      if (!yRow) continue;
      yRow.cells[m.month - 1] = m.ret;
    }
    return grid;
  }, [data]);

  if (rows.length === 0) {
    return (
      <ChartShell title="Monthly Returns">
        <EmptyChart message="Need at least one full month of equity data." />
      </ChartShell>
    );
  }

  return (
    <ChartShell title="Monthly Returns" subtitle="color clamp ±10%" height={220}>
      <div className="flex h-full flex-col gap-1">
        {/* Month-label header */}
        <div className="grid items-center gap-1 text-[9px] uppercase tracking-wider text-slate-500"
             style={{ gridTemplateColumns: '32px repeat(12, 1fr)' }}>
          <span />
          {MONTH_LABELS.map((m, i) => (
            <span key={i} className="text-center">
              {m}
            </span>
          ))}
        </div>
        {rows.map((row) => (
          <div
            key={row.year}
            className="grid items-center gap-1"
            style={{ gridTemplateColumns: '32px repeat(12, 1fr)' }}
          >
            <span className="text-[10px] font-mono text-slate-400">{row.year}</span>
            {row.cells.map((ret, i) => (
              <Cell key={i} ret={ret} />
            ))}
          </div>
        ))}
      </div>
    </ChartShell>
  );
}

function Cell({ ret }: { ret: number | null }) {
  if (ret == null) {
    return (
      <div
        className="rounded-sm border border-slate-900 bg-slate-950"
        style={{ aspectRatio: '1.5 / 1' }}
        title="No data"
      />
    );
  }
  const color = retColor(ret);
  const label = (ret * 100).toFixed(1) + '%';
  // borderStyle is 'dashed' only for the exact-zero case so that a genuine
  // 0.0% month is visually distinguishable from a null/no-data cell at a
  // glance, without relying solely on the hover title attribute.
  const borderStyle = color.borderStyle ?? 'solid';
  return (
    <div
      className="rounded-sm text-center text-[8px] font-mono leading-snug"
      style={{
        aspectRatio: '1.5 / 1',
        background: color.bg,
        color: color.fg,
        border: `1px ${borderStyle} ${color.border}`,
      }}
      title={label}
    >
      <div className="flex h-full items-center justify-center px-0.5">{label}</div>
    </div>
  );
}

// Symmetric diverging color around 0, clamped to [-0.10, +0.10].
function retColor(ret: number): { bg: string; fg: string; border: string; borderStyle?: string } {
  const clamp = Math.max(-0.1, Math.min(0.1, ret));
  const t = clamp / 0.1; // -1..+1
  if (t > 0) {
    // 0 (slate-950) -> 1 (emerald-600)
    const alpha = 0.15 + 0.55 * t;
    return {
      bg: `rgba(16, 185, 129, ${alpha.toFixed(3)})`,
      fg: t > 0.5 ? '#022c22' : '#ecfdf5',
      border: 'rgba(16, 185, 129, 0.55)',
    };
  }
  if (t < 0) {
    const alpha = 0.15 + 0.55 * -t;
    return {
      bg: `rgba(244, 63, 94, ${alpha.toFixed(3)})`,
      fg: -t > 0.5 ? '#1f0a14' : '#ffe4e6',
      border: 'rgba(244, 63, 94, 0.55)',
    };
  }
  // Exact zero return: background is slate-900 (#0f172a), nearly as dark as
  // the null cell's slate-950 (#020617).  Use a lighter dashed border
  // (slate-600 = #334155) so a genuine 0.0% month is visually distinct from
  // an absent month even without hovering.
  return {
    bg: '#0f172a',
    fg: '#94a3b8',
    border: '#334155',
    borderStyle: 'dashed',
  };
}
