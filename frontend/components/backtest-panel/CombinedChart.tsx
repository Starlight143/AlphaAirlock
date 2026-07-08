'use client';

import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Loader2 } from 'lucide-react';
import { api, type AlphaStrategy, type EquityPoint } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import EquityDrawdown from '@/components/charts/EquityDrawdown';
import DailyPnlBars from '@/components/charts/DailyPnlBars';
import DailyPnlDistribution from '@/components/charts/DailyPnlDistribution';
import RollingSharpe from '@/components/charts/RollingSharpe';
import DrawdownDistribution from '@/components/charts/DrawdownDistribution';
import RecoveryTime from '@/components/charts/RecoveryTime';
import CategoryDoughnut from '@/components/charts/CategoryDoughnut';
import AssetRatio from '@/components/charts/AssetRatio';
import MultiEquityOverlay from './MultiEquityOverlay';
import { sliceCurveByRange } from './_rangeUtils';
import type { DateRangeKey } from './ChartsToolbar';

type Props = {
  strategyIds: number[];
  method: string;
  /**
   * P13 — chart-wide date-range chip from the parent toolbar. When set, the
   * combined equity_curve is sliced client-side to entries on/after
   * `max(timestamp) - days` (anchored to the curve's last timestamp, NOT
   * wall-clock — see _rangeUtils.cutoffFromCurve).
   */
  range?: DateRangeKey;
  /**
   * P13 — full strategy rows for `strategyIds`, supplied by the parent page
   * which already holds them in state. Eliminates a redundant in-component
   * useQuery(queryKeys.strategies) just to derive category slices + member
   * overlay rows. Parent passes `[]` when nothing is selected (the early
   * return on `strategyIds.length === 0` covers that).
   */
  selectedStrategies?: AlphaStrategy[];
};

/**
 * Combined-portfolio chart group on the Backtest Panel.
 * Shows: combined equity+DD overlay, daily PnL, rolling Sharpe, PnL dist,
 * and a per-strategy weights legend.
 */
export default function CombinedChart({
  strategyIds,
  method,
  range = 'ALL',
  selectedStrategies = [],
}: Props) {
  // P8-FIX/M-15 — operator can opt-in to seeing the per-member curves overlaid
  // beneath the combined view. Default off to keep the page light.
  const [showMembers, setShowMembers] = useState<boolean>(false);

  const q = useQuery({
    queryKey: queryKeys.portfolioCombine(strategyIds, method),
    queryFn: () => api.portfolioCombine(strategyIds, method),
    enabled: strategyIds.length > 0,
    staleTime: 30_000,
  });

  // P13 — derive both categorySlices and memberStrategies from the parent-
  // supplied selectedStrategies array. Removes the prior in-component
  // useQuery(queryKeys.strategies) — the parent page already holds these rows
  // in its `selectedStrategies` useMemo (backtest-panel/page.tsx:79-82).
  const categorySlices = useMemo(() => {
    const weights = (q.data?.weights ?? {}) as Record<string, number>;
    const acc: Record<string, number> = {};
    for (const s of selectedStrategies) {
      const cat = (s.config?.alpha_category as string | undefined) || 'unclassified';
      const w = Number(weights[String(s.id)] ?? 0);
      if (!Number.isFinite(w) || w <= 0) continue;
      acc[cat] = (acc[cat] || 0) + w;
    }
    return Object.entries(acc).map(([name, value]) => ({ name, value }));
  }, [selectedStrategies, q.data]);

  // P12 — Rules of Hooks: this useMemo MUST run on every render, so it lives
  // above the early returns below. Moving it under a conditional return would
  // change hook order between renders and crash React in strict mode.
  // P13 — now a pass-through of the parent-supplied selectedStrategies; kept
  // as a useMemo for referential stability across renders (Recharts re-runs
  // its layout when the data prop identity changes).
  const memberStrategies: AlphaStrategy[] = useMemo(
    () => selectedStrategies.slice(),
    [selectedStrategies],
  );

  if (strategyIds.length === 0) {
    return (
      <div className="flex h-full items-center justify-center rounded-xl border border-slate-800 bg-slate-900/40 text-xs text-slate-500">
        Pick one or more strategies on the left to combine them.
      </div>
    );
  }

  if (q.isLoading) {
    return (
      <div className="flex h-full items-center justify-center rounded-xl border border-slate-800 bg-slate-900/40 text-xs text-slate-500">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        Combining {strategyIds.length} strategies via {method}…
      </div>
    );
  }

  if (q.isError || !q.data) {
    return (
      <div className="flex h-full items-center justify-center rounded-xl border border-rose-800 bg-rose-500/10 text-xs text-rose-300">
        Failed to load combined portfolio.
      </div>
    );
  }

  const data = q.data;
  // P13 — apply the chart-wide range chip to the combined equity_curve before
  // forwarding to every downstream chart. Anchor to the LAST timestamp in the
  // curve (not wall-clock) so historical backtests aren't truncated to empty.
  const equityAll = data.equity_curve;
  const equity: EquityPoint[] = sliceCurveByRange(equityAll, range);
  const metrics = data.metrics;
  const weightEntries = Object.entries(data.weights)
    .map(([sid, w]) => ({ id: Number(sid), weight: Number(w) }))
    .sort((a, b) => b.weight - a.weight);

  // C-H3 — guarded tone helpers. The previous inline tone() silently
  // classified NaN as 'bad' (rose) because the comparison returned false in
  // every branch. Tiles read em-dash for value but painted red — directly
  // contradicting the missing-value semantics shown by the screenshot.
  const tone = (n: number, good: number, neutral: number): string => {
    if (!Number.isFinite(n)) return 'text-slate-500';
    if (n >= good) return 'text-emerald-300';
    if (n >= neutral) return 'text-cyan-200';
    return 'text-rose-300';
  };
  const cumRetColor = (n: number): string => {
    if (!Number.isFinite(n)) return 'text-slate-500';
    return n > 0 ? 'text-emerald-300' : n < 0 ? 'text-rose-300' : 'text-slate-400';
  };
  const ddColor = (n: number): string => {
    if (!Number.isFinite(n)) return 'text-slate-500';
    if (n >= -0.15) return 'text-emerald-300';
    if (n >= -0.30) return 'text-amber-300';
    return 'text-rose-300';
  };

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto">
      {/* Header KPIs + weights */}
      <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
        <div className="mb-2 flex items-baseline justify-between">
          <h2 className="text-[10px] font-bold uppercase tracking-[0.2em] text-cyan-300">
            Combined Portfolio · {data.method_label}
          </h2>
          <span className="text-[9px] text-slate-500">
            {data.n_strategies} strategies aligned over {data.n_aligned_days} days
            {data.missing.length > 0 && ` · ${data.missing.length} missing`}
          </span>
        </div>
        <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
          <KpiTile
            label="Sharpe"
            value={Number.isFinite(metrics.annualized_sharpe) ? metrics.annualized_sharpe.toFixed(2) : '—'}
            color={tone(metrics.annualized_sharpe, 1.5, 0.5)}
          />
          <KpiTile
            label="Total Return"
            value={Number.isFinite(metrics.cumulative_return) ? `${(metrics.cumulative_return * 100).toFixed(1)}%` : '—'}
            color={cumRetColor(Number(metrics.cumulative_return))}
          />
          <KpiTile
            label="Annualized"
            value={Number.isFinite(metrics.annualized_return) ? `${(metrics.annualized_return * 100).toFixed(1)}%` : '—'}
            color={cumRetColor(Number(metrics.annualized_return))}
          />
          <KpiTile
            label="Max DD"
            value={Number.isFinite(metrics.max_drawdown) ? `${(metrics.max_drawdown * 100).toFixed(1)}%` : '—'}
            color={ddColor(Number(metrics.max_drawdown))}
          />
          <KpiTile
            label="Win Rate"
            value={Number.isFinite(metrics.win_rate) ? `${(metrics.win_rate * 100).toFixed(1)}%` : '—'}
            color={tone(metrics.win_rate, 0.55, 0.45)}
          />
          <KpiTile
            label="Profit Factor"
            value={Number.isFinite(metrics.profit_factor) ? metrics.profit_factor.toFixed(2) : '—'}
            color={tone(metrics.profit_factor, 1.5, 1.05)}
          />
        </div>

        {weightEntries.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {weightEntries.map((w) => (
              <span
                key={w.id}
                className={
                  w.weight > 0
                    ? 'rounded border border-cyan-700/40 bg-cyan-500/10 px-2 py-0.5 text-[10px] text-cyan-200'
                    : 'rounded border border-slate-800 bg-slate-950 px-2 py-0.5 text-[10px] text-slate-600'
                }
              >
                S#{w.id} · {(w.weight * 100).toFixed(1)}%
              </span>
            ))}
          </div>
        )}
      </div>

      {/* P8-FIX/M-15 — Members overlay toggle row */}
      <div className="flex items-center gap-1.5 rounded-md border border-slate-800 bg-slate-950/40 px-3 py-1.5">
        <span className="text-[10px] uppercase tracking-widest text-slate-500">View</span>
        <button
          type="button"
          onClick={() => setShowMembers(false)}
          className={`rounded px-2 py-1 text-[10px] font-bold uppercase tracking-wider ${
            !showMembers
              ? 'border border-cyan-700 bg-cyan-500/15 text-cyan-200'
              : 'border border-transparent text-slate-400 hover:text-slate-200'
          }`}
        >
          Combined only
        </button>
        <button
          type="button"
          onClick={() => setShowMembers(true)}
          className={`rounded px-2 py-1 text-[10px] font-bold uppercase tracking-wider ${
            showMembers
              ? 'border border-cyan-700 bg-cyan-500/15 text-cyan-200'
              : 'border border-transparent text-slate-400 hover:text-slate-200'
          }`}
        >
          + Members
        </button>
      </div>

      {/* Charts — top row: combined equity + drawdown, daily PnL */}
      <EquityDrawdown data={equity} />
      {showMembers && memberStrategies.length > 0 && (
        <MultiEquityOverlay strategies={memberStrategies} range={range} />
      )}
      <DailyPnlBars data={equity} />

      {/* P6-A3: 2×3 grid mirrors the reference screenshot — Rolling Sharpe
          (weekly window=7), PnL distribution, Drawdown distribution, Recovery
          time, plus the two category pies. */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2 xl:grid-cols-3">
        <RollingSharpe data={equity} window={7} />
        <DailyPnlDistribution data={equity} />
        <DrawdownDistribution data={equity} />
        <RecoveryTime data={equity} />
        <CategoryDoughnut slices={categorySlices} title="Alpha Category" variant="pie" />
        <AssetRatio data={data} title="Asset Ratio" />
      </div>
    </div>
  );
}

function KpiTile({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-950/50 p-2">
      <div className={`font-mono text-base leading-none ${color}`}>{value}</div>
      <div className="mt-1 text-[9px] uppercase tracking-wider text-slate-500">
        {label}
      </div>
    </div>
  );
}
