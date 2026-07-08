'use client';

import type { ReactNode } from 'react';

/**
 * Visual chrome shared by every chart primitive on the strategy detail page.
 * Keeping all colors / typography / sizing here means the Performance grid
 * stays visually consistent no matter how many chart types get added in P1.
 */

export const TOOLTIP_STYLE: React.CSSProperties = {
  background: '#020617',
  border: '1px solid #334155',
  borderRadius: 6,
  fontSize: 11,
  color: '#e2e8f0',
};

export const AXIS_STROKE = '#475569';
export const GRID_STROKE = '#1e293b';
export const AXIS_FONT_SIZE = 10;

// Per-chart accent — keyed by chart type, matches the reference UI exactly.
// P8-FIX/H-10: position chart now distinguishes long (cyan), short (rose),
// flat (slate). Old single ``position`` key is kept as an alias to
// ``positionLong`` so external consumers don't break.
export const CHART_COLORS = {
  // P6-L08: equity line is now slate-200 (white-ish) to mirror the reference
  // screenshot's monochrome equity overlay. The emerald gradient fill is
  // still tasteful — only the line stroke changed.
  equity: '#e2e8f0',         // slate-200 (was emerald-500)
  drawdown: '#f43f5e',       // rose
  pnlPositive: '#22C55E',    // green
  pnlNegative: '#EF4444',    // red
  position: '#06B6D4',        // cyan (alias of positionLong — back-compat)
  positionLong: '#06B6D4',    // cyan
  positionShort: '#F43F5E',   // rose
  positionFlat: '#94a3b8',    // slate-400 — two shades brighter than AXIS_STROKE (slate-600) so flat-position cells stay visible against the axis
  pnlDist: '#3B82F6',        // blue
  rollingSharpe: '#A855F7',  // purple
  recovery: '#F97316',       // orange
  ddDist: '#EF4444',         // red
  category: ['#22D3EE', '#22C55E', '#A855F7', '#F59E0B', '#EF4444', '#D946EF', '#3B82F6', '#64748B'],
  // 16-color palette for multi-strategy overlay charts on the Backtest Panel
  // (P5-FE-13). Picked for contrast on the slate-950 background; loops past
  // 16 strategies via index modulo.
  strategySeries: [
    '#22D3EE', '#10B981', '#A855F7', '#F59E0B',
    '#F43F5E', '#D946EF', '#3B82F6', '#FB923C',
    '#84CC16', '#06B6D4', '#EC4899', '#EAB308',
    '#22C55E', '#8B5CF6', '#0EA5E9', '#F97316',
  ],
} as const;

export function strategyColor(index: number): string {
  const palette = CHART_COLORS.strategySeries;
  return palette[index % palette.length];
}

export function ChartShell({
  title,
  subtitle,
  children,
  height = 180,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  height?: number;
}) {
  return (
    <div
      className="flex flex-col rounded-lg border border-slate-800 bg-slate-950/40 p-3"
      style={{ minHeight: height + 36 }}
    >
      <div className="mb-1 flex items-center justify-between">
        <span className="text-[10px] font-bold uppercase tracking-widest text-slate-400">
          {title}
        </span>
        {subtitle && (
          <span className="text-[9px] uppercase tracking-wider text-slate-600">
            {subtitle}
          </span>
        )}
      </div>
      <div style={{ height }}>{children}</div>
    </div>
  );
}

export function EmptyChart({ message }: { message?: string }) {
  return (
    <div className="flex h-full items-center justify-center text-[11px] text-slate-600">
      {message ?? 'No data yet — run a strategy to populate.'}
    </div>
  );
}
