'use client';

import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type AlphaStrategy, type EquityPoint } from '@/lib/api';
import { cumRetTone, ddToneKpi, getTradesCount, type KpiTone, metricTone } from '@/lib/derive';
import EquityDrawdown from '@/components/charts/EquityDrawdown';
import DailyPnlBars from '@/components/charts/DailyPnlBars';
import DrawdownDistribution from '@/components/charts/DrawdownDistribution';
import RecoveryTime from '@/components/charts/RecoveryTime';
import CategoryDoughnut from '@/components/charts/CategoryDoughnut';
import MonthlyReturnsHeatmap from '@/components/charts/MonthlyReturnsHeatmap';
import PositionChart from '@/components/charts/PositionChart';
import DistributionsRow from '@/components/backtest-panel/DistributionsRow';
import DetailedMetricsTable from './DetailedMetricsTable';

type Props = {
  strategy: AlphaStrategy;
};

/**
 * Performance tab content — 6 KPI tiles + 7 chart panels arranged in a grid
 * that mirrors the reference YouTube demo's strategy detail layout. All
 * charts compute their distributions client-side from the existing
 * equity_curve payload — no new backend fetch required.
 */
export default function PerformanceGrid({ strategy }: Props) {
  // P29-F1: memoize fallback so empty array isn't fresh `[]` each render.
  const equity: EquityPoint[] = useMemo(
    () => strategy.equity_curve ?? [],
    [strategy.equity_curve],
  );
  const metrics = strategy.metrics ?? {};

  // Per-bar tape — used by PositionChart, PositionDistribution, and (later)
  // the trade-level rows in DetailedMetricsTable. Shared queryKey with the
  // BACKTEST CSV tab so we only pull this payload once per strategy.
  const tradesQ = useQuery({
    queryKey: ['strategy-trades', strategy.id, false],
    queryFn: () => api.strategyTrades(strategy.id, { limit: 2000, nonzeroOnly: false }),
    enabled: !!strategy.raw_backtest?.per_bar_available,
    staleTime: 60_000,
    retry: false,
  });
  const trades = tradesQ.data?.rows ?? [];
  const tradesFromConfig = getTradesCount(strategy);

  const tiles = useMemo(() => {
    const fmtPct = (v: unknown, digits = 2) =>
      Number.isFinite(v as number)
        ? `${(Number(v) * 100).toFixed(digits)}%`
        : '—';
    const fmt = (v: unknown, digits = 2) =>
      Number.isFinite(v as number) ? Number(v).toFixed(digits) : '—';
    // P8-FIX/M-13: derive Annualized Vol + Calmar client-side when the
    // backend omits them (older strategy rows from before the metric was
    // added to engine.py).
    const annRet = Number(metrics.annualized_return);
    const maxDD = Number(metrics.max_drawdown);
    const annVolBackend = Number(metrics.annualized_volatility);
    const calmarBackend = Number(metrics.calmar_ratio);
    // C-M10 — prefer the backend's authoritative annualized_volatility when
    // present. Fallback uses metrics.std_period_return * sqrt(annualization_factor)
    // so cadence-mixed strategies (hourly / 15m / daily) annualize correctly
    // from the engine's own annualization_factor on raw_backtest. The legacy
    // sqrt(8760) hardcoded path is retained ONLY for the rare row that still
    // carries std_hourly_return without an annualization_factor — falls back
    // to '—' when neither path is satisfied.
    const rawBacktest = (strategy.raw_backtest ?? {}) as { annualization_factor?: number };
    const annFactor = Number(rawBacktest.annualization_factor);
    const stdPeriod = Number((metrics as { std_period_return?: unknown }).std_period_return);
    const stdHourly = Number(metrics.std_hourly_return);
    const annVolDerived =
      Number.isFinite(annVolBackend) && annVolBackend > 0
        ? annVolBackend
        : Number.isFinite(stdPeriod) && Number.isFinite(annFactor) && annFactor > 0
          ? stdPeriod * Math.sqrt(annFactor)
          : Number.isFinite(stdHourly)
            ? stdHourly * Math.sqrt(Number.isFinite(annFactor) && annFactor > 0 ? annFactor : 8760)
            : NaN;
    const calmarDerived =
      Number.isFinite(calmarBackend)
        ? calmarBackend
        : Number.isFinite(annRet) && Number.isFinite(maxDD) && Math.abs(maxDD) > 1e-14
          ? annRet / Math.abs(maxDD)
          : NaN;
    // C-H2 — all KPI tones now route through metricTone() so NaN / undefined
    // metrics paint a neutral 'muted' tile instead of silently falling
    // through to 'bad' (rose). Display value stays '—' via fmt/fmtPct above;
    // the tile background no longer contradicts the missing-value label.
    return [
      {
        label: 'Sharpe',
        value: fmt(metrics.annualized_sharpe, 2),
        tone: metricTone(Number(metrics.annualized_sharpe), 1.5, 0.5),
      },
      {
        label: 'Ann. Return',
        value: fmtPct(metrics.annualized_return, 1),
        tone: metricTone(Number(metrics.annualized_return), 0.20, 0.05),
      },
      {
        // C-M7 — Ann.Vol carries no inherent good/bad polarity; vol quality
        // is assessed via Sharpe/Calmar, not absolute vol. Render neutral
        // cyan whenever a number is present so the operator isn't
        // misled into thinking high vol alone is "bad".
        label: 'Ann. Vol',
        value: Number.isFinite(annVolDerived) ? `${(annVolDerived * 100).toFixed(1)}%` : '—',
        tone: (Number.isFinite(annVolDerived) ? 'neutral' : 'muted') as KpiTone,
      },
      {
        label: 'Max DD',
        value: fmtPct(metrics.max_drawdown, 1),
        tone: ddToneKpi(Number(metrics.max_drawdown)),
      },
      {
        label: 'Calmar',
        value: Number.isFinite(calmarDerived) ? calmarDerived.toFixed(2) : '—',
        tone: metricTone(calmarDerived, 1.0, 0.5),
      },
      {
        label: 'Win Rate',
        value: fmtPct(metrics.win_rate, 1),
        tone: metricTone(Number(metrics.win_rate), 0.55, 0.45),
      },
      {
        label: 'Profit Factor',
        value: fmt(metrics.profit_factor, 2),
        tone: metricTone(Number(metrics.profit_factor), 1.5, 1.05),
      },
      {
        label: 'Total Return',
        value: fmtPct(metrics.cumulative_return, 2),
        tone: cumRetTone(Number(metrics.cumulative_return)),
      },
      {
        label: 'Trades',
        value: tradesFromConfig != null ? String(tradesFromConfig) : '—',
        tone: (tradesFromConfig == null
          ? 'muted'
          : tradesFromConfig >= 20
            ? 'good'
            : 'neutral') as KpiTone,
      },
      // P17/M4 — backend (engine.py:236-255) computes avg_recovery_days from daily
      // drawdown series; surface it on the Performance grid. Neutral tone because
      // Calmar/MaxDD already classify drawdown quality.
      {
        label: 'Avg Recovery (days)',
        value: Number.isFinite(Number(metrics.avg_recovery_days))
          ? Number(metrics.avg_recovery_days).toFixed(1)
          : '—',
        tone: (Number.isFinite(Number(metrics.avg_recovery_days))
          ? 'neutral'
          : 'muted') as KpiTone,
      },
      // P6-M03: total days tile — counted from the daily-resampled equity curve.
      {
        label: 'Total Days',
        value: equity.length > 0 ? String(equity.length) : '—',
        tone: (equity.length === 0
          ? 'muted'
          : equity.length >= 252
          ? 'good'
          : equity.length >= 60
            ? 'neutral'
            : 'bad') as KpiTone,
      },
    ] as const;
  }, [metrics, tradesFromConfig, equity]);

  const categorySlices = useMemo(() => {
    const cat = (strategy.config?.alpha_category as string | undefined) || null;
    if (!cat) return [];
    return [{ name: cat, value: 1 }];
  }, [strategy.config]);

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-1">
      {/* KPI tiles — P8-FIX/M-13 widened to 10 tiles; P17/M4 adds Avg Recovery (days) for 11 total. */}
      <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-6 xl:grid-cols-11">
        {tiles.map((t) => (
          <KpiTile key={t.label} {...t} />
        ))}
      </div>

      {/* P8-FIX/H-13 — costs included chip strip. */}
      <CostsStrip strategy={strategy} />

      {/* C-M11 — gate every chart-row consumer of `equity` behind a single
         empty state when no equity curve is available. Previously each
         downstream chart rendered its own ~220px em-dash panel, stacking
         ~800px of dead pixels onto the page when the strategy hasn't
         been backtested yet. The category doughnut + position panels stay
         outside the gate because they read independent data. */}
      {equity.length > 0 ? (
        <>
          {/* Big equity+drawdown panel */}
          <EquityDrawdown data={equity} />

          {/* P11-F3-08 — Monthly Returns heatmap now sits directly under the
             equity panel so the operator can scan the monthly distribution
             before diving into per-bar PnL. */}
          <MonthlyReturnsHeatmap data={equity} />

          {/* Per-day signal & bars */}
          <DailyPnlBars data={equity} />

          {/* P5-FE-12 — Per-bar position chart (full-width strip) */}
          <PositionChart data={trades} />

          {/* P17/M7 — primary distributions row (Rolling / DailyPnl / Position)
             pulled into its own 3-col grid for side-by-side comparison. */}
          <DistributionsRow equity={equity} trades={trades} />
          {/* Secondary distributions + category — 2-col below the row. */}
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            <DrawdownDistribution data={equity} />
            <RecoveryTime data={equity} />
            <div className="lg:col-span-2"><CategoryDoughnut slices={categorySlices} /></div>
          </div>
        </>
      ) : (
        <EquityDrawdown data={equity} />
      )}

      {/* P6-A7 — Detailed metrics: now consumes the per-bar tape for true
         trade-level rows. Grouped into TRADING / DISTRIBUTION / ROBUSTNESS. */}
      <DetailedMetricsTable strategy={strategy} trades={trades} />
    </div>
  );
}

function KpiTile({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: KpiTone;
}) {
  const color =
    tone === 'good'
      ? 'text-emerald-300'
      : tone === 'bad'
      ? 'text-rose-300'
      : tone === 'muted'
      ? 'text-slate-500'
      : 'text-slate-200';
  return (
    <div className="rounded-md border border-slate-800 bg-slate-950/50 p-2">
      <div className={`font-mono text-base leading-none ${color}`}>{value}</div>
      <div className="mt-1 text-[9px] uppercase tracking-wider text-slate-500">
        {label}
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// P8-FIX/H-13 — Costs Strip
// ---------------------------------------------------------------------------

function CostsStrip({ strategy }: { strategy: AlphaStrategy }) {
  const raw = strategy.raw_backtest ?? {};
  const fee = Number(raw.fee_per_side);
  const slip = Number(raw.slippage_per_side);
  // round always stores the TRUE full round-trip cost:
  //   - raw.round_trip_cost is already a round-trip value (no doubling needed)
  //   - fallback fee_per_side + slippage_per_side is ONE side only, so multiply by 2
  const round =
    Number.isFinite(Number(raw.round_trip_cost))
      ? Number(raw.round_trip_cost)
      : Number.isFinite(fee) && Number.isFinite(slip)
        ? (fee + slip) * 2
        : NaN;
  const bars = Number(raw.bars);
  const annFactor = Number(raw.annualization_factor);
  if (!Number.isFinite(fee) && !Number.isFinite(slip)) {
    return (
      <div className="rounded border border-slate-800 bg-slate-950/30 px-2 py-1 text-[9px] uppercase tracking-widest text-slate-600">
        Costs unavailable for this run · re-run backtest to populate
      </div>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2 rounded border border-slate-800 bg-slate-950/30 px-2 py-1 text-[10px] font-mono text-slate-400">
      <span className="font-bold uppercase tracking-widest text-cyan-300">Costs incl.</span>
      {Number.isFinite(fee) && (
        <Chip color="text-cyan-300">Fee {(fee * 10000).toFixed(1)} bps × 2 sides</Chip>
      )}
      {Number.isFinite(slip) && (
        <Chip color="text-amber-300">Slippage {(slip * 10000).toFixed(1)} bps × 2 sides</Chip>
      )}
      {Number.isFinite(round) && (
        <Chip color="text-rose-300">Round-trip {(round * 10000).toFixed(1)} bps</Chip>
      )}
      {Number.isFinite(bars) && bars > 0 && <Chip>{bars.toLocaleString()} bars</Chip>}
      {Number.isFinite(annFactor) && (
        <Chip>annualised × {annFactor.toLocaleString()}</Chip>
      )}
    </div>
  );
}

function Chip({ children, color = 'text-slate-300' }: { children: React.ReactNode; color?: string }) {
  return (
    <span className={`rounded border border-slate-800 bg-slate-900/60 px-1.5 py-0.5 ${color}`}>
      {children}
    </span>
  );
}
