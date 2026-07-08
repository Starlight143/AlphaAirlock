'use client';

import type { CombinedPortfolio } from '@/lib/api';

type Props = { data: CombinedPortfolio };

type Tone = 'good' | 'neutral' | 'bad';

type Tile = { label: string; value: string; tone: Tone };

/**
 * 4-tile aggregated metrics row shown beneath the Performance Charts grid on
 * the Backtest Panel. Pulls from the existing /api/portfolio/combine output.
 */
export default function AggregatedMetrics({ data }: Props) {
  const m = data.metrics ?? {};
  // P11-F3-06 — when zero strategies are aggregated (initial mount, or the
  // operator hasn't picked anything yet), render the four tiles with em-dash
  // placeholders so the strip still occupies its slot in the page grid.
  const empty = data.n_strategies === 0;
  const tiles: Tile[] = empty
    ? [
        { label: 'Combined Sharpe', value: '—', tone: 'neutral' },
        { label: 'Combined MaxDD', value: '—', tone: 'neutral' },
        { label: 'Combined Win Rate', value: '—', tone: 'neutral' },
        { label: 'Combined Cum Return', value: '—', tone: 'neutral' },
      ]
    : [
        { label: 'Combined Sharpe', value: fmt(m.annualized_sharpe, 2), tone: tone(m.annualized_sharpe, 1.5, 0.5) },
        // P13 — wrap the inline ternaries in finite-number guards so NaN /
        // undefined no longer paints the tile red. Mirrors the existing
        // tone() helper convention for the other two tiles.
        { label: 'Combined MaxDD', value: pct(m.max_drawdown, 1), tone: ddTone(m.max_drawdown) },
        { label: 'Combined Win Rate', value: pct(m.win_rate, 1), tone: tone(m.win_rate, 0.55, 0.45) },
        { label: 'Combined Cum Return', value: pct(m.cumulative_return, 1), tone: cumRetTone(m.cumulative_return) },
      ];
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {tiles.map((t) => (
        <div key={t.label} className="rounded-md border border-slate-800 bg-slate-950/50 p-3">
          <div className={`font-mono text-lg leading-none ${toneClass(t.tone)}`}>{t.value}</div>
          <div className="mt-1 text-[9px] uppercase tracking-widest text-slate-500">
            {t.label}
          </div>
        </div>
      ))}
    </div>
  );
}

function fmt(v: unknown, digits = 2): string {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(digits) : '—';
}
function pct(v: unknown, digits = 1): string {
  const n = Number(v);
  return Number.isFinite(n) ? `${(n * 100).toFixed(digits)}%` : '—';
}
function tone(v: number | undefined, good: number, neutral: number): Tone {
  if (v == null || !Number.isFinite(v)) return 'neutral';
  if (v >= good) return 'good';
  if (v >= neutral) return 'neutral';
  return 'bad';
}
// P13 — drawdown is a non-positive fraction; closer to 0 is better. Guard
// against NaN/undefined explicitly so missing-metric tiles render neutral
// instead of incorrectly painting red.
function ddTone(v: number | undefined): Tone {
  if (v == null || !Number.isFinite(v)) return 'neutral';
  if (v >= -0.15) return 'good';
  if (v >= -0.30) return 'neutral';
  return 'bad';
}
// P13 — cum-return tone with the same finite guard as ddTone above.
// C-M13 — exactly 0 is neutral (no PnL is neither good nor bad).
function cumRetTone(v: number | undefined): Tone {
  if (v == null || !Number.isFinite(v)) return 'neutral';
  if (v > 0) return 'good';
  if (v < 0) return 'bad';
  return 'neutral';
}
function toneClass(t: Tone): string {
  if (t === 'good') return 'text-emerald-300';
  if (t === 'bad') return 'text-rose-300';
  return 'text-cyan-200';
}
