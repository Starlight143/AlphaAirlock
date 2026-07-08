'use client';

import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { AlphaStrategy } from '@/lib/api';
import { api } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { getTradesCount } from '@/lib/derive';

type Props = {
  /**
   * C-M8 — when the parent already owns the strategies array, pass it here
   * so we skip our own useQuery roundtrip. TanStack de-dupes identical
   * queryKeys but explicit prop-passing avoids the refetchInterval doubling
   * up across mounts. Mirrors the same C-M9 wiring on StrategyTable.
   */
  strategies?: AlphaStrategy[];
};

/**
 * 8-tile KPI strip atop the Backtest Panel — Total Strategies / Backtested /
 * Avg Sharpe / Worst MaxDD / Avg Win Rate / Total Trades / Avg Bars Held /
 * Pipeline Health. (P11-F3-02 reordered + replaced standalone Rejected tile
 * with a "N rejected" sub-line on Total Strategies, and added Avg Bars Held.)
 */
export default function KpiStrip({ strategies: strategiesProp }: Props = {}) {
  const sq = useQuery({
    queryKey: queryKeys.strategies,
    queryFn: api.strategies,
    refetchInterval: 8_000,
    // C-M8 — opt out of the local fetch when the parent supplied strategies.
    enabled: strategiesProp === undefined,
  });
  // F3 — switched to V2 buckets so `live` / `graveyard` counts come from
  // the merged Stage 5 (live_trade) / Stage 6 (graveyard) buckets exposed
  // by /api/v2/pipeline/buckets. The V1 endpoint never gets called here.
  const bq = useQuery({
    queryKey: queryKeys.pipelineBucketsV2,
    queryFn: api.pipelineBucketsV2,
    refetchInterval: 6_000,
  });

  const tiles = useMemo(() => {
    // C-M8 — prefer the parent-supplied array; fall back to local query data.
    const strategies: AlphaStrategy[] = strategiesProp ?? sq.data?.strategies ?? [];
    const total = bq.data?.total ?? strategies.length;
    const backtested = strategies.filter((s) => {
      const m = s.metrics ?? {};
      return Number.isFinite(m.annualized_sharpe);
    });
    const sharpes = backtested
      .map((s) => Number(s.metrics?.annualized_sharpe))
      .filter(Number.isFinite);
    const dds = backtested
      .map((s) => Number(s.metrics?.max_drawdown))
      .filter(Number.isFinite);
    const winRates = backtested
      .map((s) => Number(s.metrics?.win_rate))
      .filter(Number.isFinite);
    // P17/M5 — aggregate profit_factor across backtested strategies. backend caps
    // +inf at 999.0 (engine.py:271); filter the sentinel out of the mean.
    const profitFactors = backtested
      .map((s) => Number(s.metrics?.profit_factor))
      .filter((v) => Number.isFinite(v) && v < 999);
    const trades = strategies
      .map((s) => getTradesCount(s))
      .filter((n): n is number => n != null);
    const avg = (arr: number[]) =>
      arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null;
    // P11-F3-02 — Avg Bars Held = mean of (bars / trades) per strategy.
    // Skip strategies where either bars or trades are missing / zero so we
    // never divide by zero or surface a misleading aggregate.
    const barsHeldPerStrategy = strategies
      .map((s) => {
        const bars = Number(
          s.raw_backtest?.bars ??
            (s.config as { bars?: unknown } | undefined)?.bars ??
            NaN,
        );
        const tr = getTradesCount(s);
        if (!Number.isFinite(bars) || tr == null || tr <= 0) return NaN;
        return bars / tr;
      })
      .filter(Number.isFinite);
    const avgBarsHeld = avg(barsHeldPerStrategy);

    const buckets = bq.data?.buckets ?? [];
    // V2 key is 'live_trade'; keep 'live' fallback for any stale V1 payload
    // that may still be sitting in a sibling query cache.
    const live =
      buckets.find((b) => b.key === 'live_trade')?.count ??
      buckets.find((b) => b.key === 'live')?.count ??
      0;
    const graveyard = buckets.find((b) => b.key === 'graveyard')?.count ?? 0;
    // Reject count direct from strategies — reflects current REJECTED status
    // regardless of bucket bookkeeping (P5-FE-13 #15).
    const rejected = strategies.filter((s) => (s.status || '').toUpperCase() === 'REJECTED').length;
    // Only compute pipeline health when bq.data is present; total and graveyard
    // come from the same V2 response, so mixing total=strategies.length with
    // graveyard=0 (empty buckets fallback) would always yield a misleading 100%.
    const pipelineHealth: number | null = bq.data
      ? Math.max(0, Math.min(100, ((bq.data.total - graveyard) / Math.max(bq.data.total, 1)) * 100))
      : null;

    const avgSharpe = avg(sharpes);
    const worstDD = dds.length ? Math.min(...dds) : null;
    const avgWin = avg(winRates);
    const avgPF = avg(profitFactors);

    // P11-F3-02 — new tile order. Rejected is folded into Total Strategies
    // as an `extra` sub-line ("N rejected" when N>0). Avg Bars Held added.
    // C-M6 — explicit tile shape so the `extra` sub-line stops needing
    // `(t as any).extra` casts at the consumer.
    const result: { label: string; value: string; tone: string; extra?: string }[] = [
      {
        label: 'Total Strategies',
        value: total.toString(),
        tone: 'neutral',
        extra: rejected > 0 ? `${rejected} rejected` : undefined,
      },
      { label: 'Backtested', value: backtested.length.toString(), tone: 'neutral' },
      {
        label: 'Avg Sharpe',
        value: avgSharpe == null ? '—' : avgSharpe.toFixed(2),
        tone: avgSharpe == null ? 'neutral' : avgSharpe > 0.5 ? 'good' : 'bad',
      },
      {
        label: 'Worst MaxDD',
        value: worstDD == null ? '—' : `${(worstDD * 100).toFixed(1)}%`,
        tone: worstDD == null ? 'neutral' : worstDD > -0.25 ? 'good' : 'bad',
      },
      {
        label: 'Avg Win Rate',
        value: avgWin == null ? '—' : `${(avgWin * 100).toFixed(1)}%`,
        tone: avgWin != null && avgWin > 0.5 ? 'good' : 'neutral',
      },
      {
        label: 'Avg Profit Factor',
        value: avgPF == null ? '—' : avgPF.toFixed(2),
        tone:
          avgPF == null
            ? 'neutral'
            : avgPF >= 1.5
              ? 'good'
              : avgPF >= 1.0
                ? 'neutral'
                : 'bad',
      },
      {
        // C-M14 — always surface the sum + reporting ratio whenever at least
        // one strategy reports trades; the operator can read the ratio and
        // weight the value accordingly. Em-dash reserved for the zero-report
        // case only.
        label: 'Total Trades',
        value: (() => {
          const totalCount = strategies.length;
          const reporting = trades.length;
          if (reporting === 0 || totalCount === 0) return '—';
          const sum = trades.reduce((a, b) => a + b, 0).toString();
          return reporting < totalCount
            ? `${sum} (${reporting}/${totalCount})`
            : sum;
        })(),
        tone: 'neutral',
      },
      {
        label: 'Avg Bars Held',
        value: avgBarsHeld == null ? '—' : avgBarsHeld.toFixed(1),
        tone: 'neutral',
      },
      {
        label: 'Pipeline Health',
        value: pipelineHealth == null ? '—' : `${pipelineHealth.toFixed(0)}%`,
        tone: pipelineHealth == null ? 'neutral' : pipelineHealth > 75 ? 'good' : pipelineHealth > 50 ? 'neutral' : 'bad',
        extra: `live=${live} · graveyard=${graveyard}`,
      },
    ];
    return result;
  // When strategiesProp is defined, sq is disabled (enabled: false) so sq.data
  // is permanently undefined — it is a dead dependency in that branch. It is
  // harmless to leave it here (React sees a stable undefined), but listing it
  // creates false expectations for reviewers. bq.data and strategiesProp are
  // the two true signals that drive re-computation.
  }, [sq.data, bq.data, strategiesProp]);

  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-5 xl:grid-cols-9">
      {tiles.map((t) => (
        <div
          key={t.label}
          className="rounded-md border border-slate-800 bg-slate-950/50 px-3 py-2"
        >
          <div
            className={
              t.tone === 'good'
                ? 'font-mono text-base leading-none text-emerald-300'
                : t.tone === 'bad'
                ? 'font-mono text-base leading-none text-rose-300'
                : 'font-mono text-base leading-none text-cyan-200'
            }
          >
            {t.value}
          </div>
          <div className="mt-1 text-[9px] uppercase tracking-wider text-slate-500">
            {t.label}
          </div>
          {t.extra && (
            <div className="text-[9px] text-slate-600">{t.extra}</div>
          )}
        </div>
      ))}
    </div>
  );
}
