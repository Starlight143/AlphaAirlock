'use client';

import { useMemo } from 'react';
import type { AlphaStrategy, EquityPoint, TradeTapeRow } from '@/lib/api';
import {
  autocorrelation,
  avgHoldingBars,
  avgLoss,
  avgWin,
  bestAutocorrelation,
  dailyReturns,
  extractTrades,
  kurtosis,
  largestLoss,
  largestWin,
  longShortCounts,
  longShortRatio,
  loseTradesCount,
  maxHoldingBars,
  monthlyWinRate,
  skewness,
  turnoverPerBar,
  winningStreak,
  winTradesCount,
  yearlyReturns,
} from '@/lib/derive';

type Props = {
  strategy: AlphaStrategy;
  /** Per-bar tape (P6-A7). When omitted, trade-level rows render `—`. */
  trades?: TradeTapeRow[];
};

type Row = { label: string; value: string; tone: string; hint?: string };
type Group = { title: string; rows: Row[] };

/**
 * Detailed metrics list for the Performance tab.
 *
 * P6-A7 rewrite:
 *   - Grouped into TRADING / DISTRIBUTION / ROBUSTNESS sections with
 *     uppercase headers matching the reference screenshot.
 *   - Trade-level rows finally consume the per-bar tape (previously hardcoded
 *     `—`). Trades are extracted via lib/derive.extractTrades — see that helper
 *     for the "contiguous same-sign signal run" semantics.
 *   - Added 5 missing rows from the reference: Win Trades, Lose Trades,
 *     Winning Streak, Largest Win, Largest Loss.
 *
 * Layout is 3-column (label / value / hint) on md+; 2-column on mobile.
 */
export default function DetailedMetricsTable({ strategy, trades = [] }: Props) {
  // P29-F1: memoize equity fallback (mirror PerformanceGrid).
  const equity: EquityPoint[] = useMemo(
    () => strategy.equity_curve ?? [],
    [strategy.equity_curve],
  );
  const groups = useMemo(() => buildGroups(equity, trades, strategy), [equity, trades, strategy]);

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
      <div className="mb-2 font-mono text-[10px] font-bold uppercase tracking-widest text-cyan-300">
        ## Detailed Metrics ##
      </div>
      {groups.map((g) => (
        <section key={g.title} className="mb-3 last:mb-0">
          <header className="mb-1 font-mono text-[10px] font-bold uppercase tracking-widest text-emerald-300">
            {g.title}
          </header>
          <div className="grid grid-cols-1 gap-x-6 gap-y-1 text-[11px] font-mono md:grid-cols-2">
            {g.rows.map((r) => (
              <div
                key={r.label}
                className="flex items-baseline justify-between border-b border-slate-900/60 py-1"
                title={r.hint}
              >
                <span className="text-slate-500">{r.label}</span>
                <span className={r.tone}>{r.value}</span>
              </div>
            ))}
          </div>
        </section>
      ))}
      {trades.length === 0 && (
        !!strategy.raw_backtest?.per_bar_available ? (
          <div className="mt-2 text-[9px] uppercase tracking-widest text-slate-600">
            Strategy held no positions — no trade-level metrics available.
          </div>
        ) : (
          <div className="mt-2 text-[9px] uppercase tracking-widest text-slate-600">
            Trade-level rows will populate once the per-bar tape is available
            for this strategy.
          </div>
        )
      )}
    </div>
  );
}


function buildGroups(equity: EquityPoint[], tape: TradeTapeRow[], _s: AlphaStrategy): Group[] {
  const dr = dailyReturns(equity);
  const sk = skewness(dr);
  const ku = kurtosis(dr);
  const lag1 = autocorrelation(dr, 1);
  const best = bestAutocorrelation(dr, 10);
  const mwin = monthlyWinRate(equity);
  const ys = yearlyReturns(equity);
  const worstYear = ys.length ? ys.reduce((a, b) => (a.ret <= b.ret ? a : b)) : null;
  const bestYear = ys.length ? ys.reduce((a, b) => (a.ret >= b.ret ? a : b)) : null;

  const trades = extractTrades(tape);
  const lsc = longShortCounts(trades);
  const lsr = longShortRatio(trades);
  const wins = winTradesCount(trades);
  const losses = loseTradesCount(trades);
  const aw = avgWin(trades);
  const al = avgLoss(trades);
  const ah = avgHoldingBars(trades);
  const mh = maxHoldingBars(trades);
  const turn = turnoverPerBar(tape);
  const streak = winningStreak(trades);
  const lw = largestWin(trades);
  const ll = largestLoss(trades);

  const muted = 'text-slate-300';
  const good = 'text-emerald-300';
  const bad = 'text-rose-300';

  const fmt = (v: number | null | undefined, digits = 3) =>
    v == null || !Number.isFinite(v) ? '—' : v.toFixed(digits);
  const fmtPct = (v: number | null | undefined, digits = 2) =>
    v == null || !Number.isFinite(v) ? '—' : `${(v * 100).toFixed(digits)}%`;
  const fmtBars = (v: number | null | undefined) =>
    v == null || !Number.isFinite(v) ? '—' : `${v.toFixed(1)} bars`;
  const fmtRatio = (v: number | null | undefined) =>
    v == null ? '—' : !Number.isFinite(v) ? '∞ (no shorts)' : v.toFixed(2);

  return [
    {
      title: '## TRADING ##',
      rows: [
        { label: 'Win Trades', value: trades.length ? String(wins) : '—', tone: good },
        { label: 'Lose Trades', value: trades.length ? String(losses) : '—', tone: bad },
        { label: 'Avg Win', value: fmtPct(aw, 2), tone: good, hint: 'Mean P&L over winning trades' },
        { label: 'Avg Loss', value: fmtPct(al, 2), tone: bad, hint: 'Mean P&L over losing trades' },
        { label: 'Largest Win', value: fmtPct(lw, 2), tone: good },
        { label: 'Largest Loss', value: fmtPct(ll, 2), tone: bad },
        { label: 'Winning Streak', value: trades.length ? String(streak) : '—', tone: muted, hint: 'Longest consecutive run of winning trades' },
        { label: 'Avg Holding Time', value: fmtBars(ah), tone: muted, hint: 'Trade duration in backtest bars' },
        { label: 'Max Holding Time', value: fmtBars(mh), tone: muted },
        { label: 'Long Trades', value: trades.length ? String(lsc.long) : '—', tone: muted },
        { label: 'Short Trades', value: trades.length ? String(lsc.short) : '—', tone: muted },
        { label: 'Long/Short Ratio', value: fmtRatio(lsr), tone: muted },
        { label: 'Turnover (per bar)', value: turn == null || !Number.isFinite(turn) ? '—' : `${turn.toFixed(3)} /bar`, tone: muted, hint: 'Σ|Δsignal| / bars' },
      ],
    },
    {
      title: '## DISTRIBUTION ##',
      rows: [
        { label: 'Skewness', value: fmt(sk, 3), tone: muted },
        { label: 'Excess Kurtosis', value: fmt(ku, 3), tone: muted },
        { label: 'Lag-1 Autocorr', value: fmt(lag1, 3), tone: muted },
        {
          label: 'Best Autocorr',
          value: best ? `${fmt(best.value, 3)} @ lag ${best.lag}` : '—',
          tone: muted,
        },
      ],
    },
    {
      title: '## ROBUSTNESS ##',
      rows: [
        {
          label: 'Monthly Win Rate',
          value: fmtPct(mwin, 1),
          tone: mwin != null && mwin >= 0.55 ? good : muted,
        },
        {
          label: 'Best Year',
          value: bestYear ? `${bestYear.year}: ${fmtPct(bestYear.ret, 2)}` : '—',
          tone: good,
        },
        {
          label: 'Worst Year',
          value: worstYear ? `${worstYear.year}: ${fmtPct(worstYear.ret, 2)}` : '—',
          tone: bad,
        },
        { label: 'Years Sampled', value: ys.length ? String(ys.length) : '—', tone: muted },
      ],
    },
  ];
}
