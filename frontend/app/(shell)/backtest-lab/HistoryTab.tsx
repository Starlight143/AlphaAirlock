'use client';

import { useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import { useQueries, useQuery } from '@tanstack/react-query';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Loader2, Layers } from 'lucide-react';
import { api, type AlphaStrategy, type EquityPoint } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import {
  AXIS_FONT_SIZE,
  AXIS_STROKE,
  ChartShell,
  EmptyChart,
  GRID_STROKE,
  TOOLTIP_STYLE,
  strategyColor,
} from '@/components/charts/_shared';

/**
 * HistoryTab — backtest-lab P8-FIX/C-3.
 *
 * Lists every strategy with a sparkline of its equity curve plus the four
 * headline KPIs (Sharpe / MaxDD / Win Rate / Trades). Checkbox-select multiple
 * rows to render an overlay panel below the list, and always show an
 * "ALL COMBINED" equal-weight curve so the operator gets the aggregated view
 * out of the box.
 */
export default function HistoryTab() {
  const strategiesQ = useQuery({
    queryKey: queryKeys.strategies,
    queryFn: api.strategies,
    staleTime: 30_000,
  });
  const strategies: AlphaStrategy[] = strategiesQ.data?.strategies ?? [];

  const [selected, setSelected] = useState<Set<number>>(new Set());
  const selectedStrategies = useMemo(
    () => strategies.filter((s) => selected.has(s.id)),
    [strategies, selected],
  );

  // Per-strategy equity curves are lazy-loaded INSIDE each card, gated on the
  // card scrolling into view (see StrategyCard + useInView). This replaced a
  // hard MAX_DETAIL=16 cap that silently dropped sparklines for every strategy
  // past the 16th newest — as the history grew the older cards rendered "no
  // curve" even though their on-disk results were intact. The selection overlay
  // still needs the SELECTED strategies' curves eagerly; those are few +
  // explicit, and share the per-card query key so the fetch is deduped against
  // whatever a card already loaded.
  const selectionDetailQueries = useQueries({
    queries: selectedStrategies.map((s) => ({
      queryKey: queryKeys.strategy(s.id),
      queryFn: () => api.strategy(s.id),
      staleTime: 60_000,
    })),
  });

  // ALL COMBINED — equal-weight portfolio combine over every strategy id with
  // an equity curve. Cached by /api/portfolio/combine query key so cross-tab
  // navigation is instant.
  const combineIds = useMemo(() => strategies.map((s) => s.id), [strategies]);
  const combineQ = useQuery({
    queryKey: queryKeys.portfolioCombine(combineIds, 'equal_weight'),
    queryFn: () => api.portfolioCombine(combineIds, 'equal_weight'),
    enabled: combineIds.length >= 2,
    staleTime: 60_000,
  });

  const toggle = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  if (strategiesQ.isLoading) {
    return (
      <div className="flex h-full items-center justify-center text-[11px] text-slate-500">
        <Loader2 className="mr-2 h-4 w-4 animate-spin text-cyan-400" /> Loading strategies…
      </div>
    );
  }
  if (strategiesQ.isError) {
    return (
      <div className="flex h-full items-center justify-center text-xs text-rose-400">
        Failed to load strategies: {(strategiesQ.error as Error).message}
      </div>
    );
  }
  if (strategies.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-[11px] text-slate-500">
        No strategies yet — extract one from Alpha Lab to populate the history.
      </div>
    );
  }

  return (
    <div className="flex h-full w-full flex-col gap-3 overflow-y-auto p-3">
      <header className="rounded-xl border border-slate-800 bg-slate-900/40 px-3 py-2">
        <h1 className="flex items-center gap-2 text-sm font-bold tracking-widest text-slate-100">
          <Layers className="h-4 w-4 text-cyan-400" /> BACKTEST HISTORY
        </h1>
        <p className="mt-0.5 text-[11px] text-slate-500">
          {strategies.length} strategies on file · {selected.size} selected for overlay
          {strategies.length === 1 ? ' · combined curve needs ≥2 strategies' : ''}
        </p>
      </header>

      {/* ALL COMBINED curve — always rendered if we have ≥2 strategies. */}
      {combineIds.length >= 2 && (
        <ChartShell
          title="All Combined · Equal Weight"
          subtitle={
            combineQ.data
              ? `${combineQ.data.n_strategies} aligned · ${combineQ.data.n_aligned_days} days`
              : combineQ.isLoading
                ? 'combining…'
                : combineQ.isError
                  ? 'failed'
                  : undefined
          }
          height={220}
        >
          {combineQ.isLoading ? (
            <div className="flex h-full items-center justify-center text-[11px] text-slate-500">
              <Loader2 className="mr-2 h-4 w-4 animate-spin text-cyan-400" /> Combining {combineIds.length} strategies…
            </div>
          ) : combineQ.isError || !combineQ.data ? (
            <EmptyChart message="Combine failed — check backend logs." />
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={combineQ.data.equity_curve.map((p) => ({
                  t: p.timestamp.slice(0, 10),
                  equity: p.equity,
                }))}
                margin={{ top: 8, right: 12, bottom: 0, left: -10 }}
              >
                <CartesianGrid stroke={GRID_STROKE} strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="t" stroke={AXIS_STROKE} tick={{ fontSize: AXIS_FONT_SIZE }} minTickGap={48} />
                <YAxis stroke={AXIS_STROKE} tick={{ fontSize: AXIS_FONT_SIZE }} />
                <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: '#cbd5e1' }} />
                <Line
                  type="monotone"
                  dataKey="equity"
                  stroke="#22d3ee"
                  strokeWidth={1.8}
                  dot={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          )}
        </ChartShell>
      )}

      {/* Selection overlay — only renders if the operator has ticked rows. */}
      {selectedStrategies.length > 0 && (
        <SelectionOverlay
          strategies={selectedStrategies}
          curves={selectionDetailQueries.map((q) => q.data?.equity_curve ?? [])}
        />
      )}

      {/* Strategy cards grid — each card lazy-loads its own curve on scroll. */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2 xl:grid-cols-3">
        {strategies.map((s) => (
          <StrategyCard
            key={s.id}
            strategy={s}
            checked={selected.has(s.id)}
            onToggle={() => toggle(s.id)}
          />
        ))}
      </div>
    </div>
  );
}

function StrategyCard({
  strategy,
  checked,
  onToggle,
}: {
  strategy: AlphaStrategy;
  checked: boolean;
  onToggle: () => void;
}) {
  // Lazy-load this card's equity curve only once it scrolls near the viewport,
  // so a history of ANY size never fires one /api/strategies/{id} request per
  // strategy on mount. The 300px rootMargin prefetches just before the card is
  // visible so the sparkline is usually ready by the time it scrolls in.
  const [cardRef, inView] = useInView<HTMLLabelElement>('300px');
  const detailQ = useQuery({
    queryKey: queryKeys.strategy(strategy.id),
    queryFn: () => api.strategy(strategy.id),
    enabled: inView,
    staleTime: 60_000,
  });
  const curve: EquityPoint[] = detailQ.data?.equity_curve ?? strategy.equity_curve ?? [];
  const loading = detailQ.isFetching && curve.length === 0;
  const m = strategy.metrics ?? {};
  const sharpe = Number(m.annualized_sharpe);
  const dd = Number(m.max_drawdown);
  const winRate = Number(m.win_rate);
  const trades = Number(strategy.raw_backtest?.trades ?? m.trades);

  const rows = curve.map((p) => ({ t: p.timestamp.slice(0, 10), equity: p.equity }));
  const status = (strategy.status || 'NEW').toUpperCase();
  const statusTone =
    status === 'LIVE' || status === 'SMALL_CAPITAL'
      ? 'text-emerald-300 border-emerald-700/40 bg-emerald-500/10'
      : status === 'PAPER_TRADE' || status === 'APPROVED'
        ? 'text-cyan-300 border-cyan-700/40 bg-cyan-500/10'
        : status === 'REJECTED' || status === 'RETIRED'
          ? 'text-rose-300 border-rose-700/40 bg-rose-500/10'
          : 'text-slate-400 border-slate-700 bg-slate-900';

  return (
    <label
      ref={cardRef}
      className={`flex cursor-pointer flex-col gap-2 rounded-xl border bg-slate-900/40 p-3 transition ${
        checked ? 'border-cyan-700/60 ring-1 ring-cyan-500/40' : 'border-slate-800 hover:border-slate-700'
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <input
            type="checkbox"
            checked={checked}
            onChange={onToggle}
            onClick={(e) => e.stopPropagation()}
            className="accent-cyan-500"
          />
          <span className="truncate text-[11px] font-bold text-slate-100">
            S#{strategy.id} <span className="font-normal text-slate-400">{strategy.name}</span>
          </span>
        </div>
        <span className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider ${statusTone}`}>
          {status}
        </span>
      </div>

      {/* Sparkline */}
      <div className="h-20 rounded-md border border-slate-800 bg-slate-950/40 p-1">
        {loading ? (
          <div className="flex h-full items-center justify-center text-[10px] text-slate-600">
            <Loader2 className="mr-1 h-3 w-3 animate-spin text-cyan-400" /> loading…
          </div>
        ) : rows.length === 0 ? (
          <div className="flex h-full items-center justify-center text-[10px] text-slate-600">no curve</div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={rows} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
              <Line
                type="monotone"
                dataKey="equity"
                stroke="#22d3ee"
                strokeWidth={1.2}
                dot={false}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* 4 KPI tiles */}
      <div className="grid grid-cols-4 gap-1.5">
        <Kpi label="Sharpe" value={Number.isFinite(sharpe) ? sharpe.toFixed(2) : '—'} tone={tone(sharpe, 1.5, 0.5)} />
        <Kpi
          label="Max DD"
          value={Number.isFinite(dd) ? `${(dd * 100).toFixed(1)}%` : '—'}
          tone={!Number.isFinite(dd) ? 'neutral' : dd >= -0.15 ? 'good' : dd >= -0.30 ? 'neutral' : 'bad'}
        />
        <Kpi
          label="Win Rate"
          value={Number.isFinite(winRate) ? `${(winRate * 100).toFixed(0)}%` : '—'}
          tone={tone(winRate, 0.55, 0.45)}
        />
        <Kpi label="Trades" value={Number.isFinite(trades) ? String(trades) : '—'} tone="neutral" />
      </div>
    </label>
  );
}

type Tone = 'good' | 'neutral' | 'bad';

function tone(v: number | undefined, good: number, neutral: number): Tone {
  if (v == null || !Number.isFinite(v)) return 'neutral';
  if (v >= good) return 'good';
  if (v >= neutral) return 'neutral';
  return 'bad';
}

function Kpi({ label, value, tone }: { label: string; value: string; tone: Tone }) {
  const color =
    tone === 'good' ? 'text-emerald-300' : tone === 'bad' ? 'text-rose-300' : 'text-cyan-200';
  return (
    <div className="rounded border border-slate-800 bg-slate-950/40 px-1 py-1">
      <div className={`font-mono text-[11px] leading-none ${color}`}>{value}</div>
      <div className="mt-1 text-[8px] uppercase tracking-wider text-slate-500">{label}</div>
    </div>
  );
}

function SelectionOverlay({
  strategies,
  curves,
}: {
  strategies: AlphaStrategy[];
  curves: EquityPoint[][];
}) {
  // Merge per-strategy series into one timestamp-keyed array for Recharts.
  const merged = useMemo(() => {
    const map = new Map<string, Record<string, number | string>>();
    strategies.forEach((s, i) => {
      const curve = curves[i] ?? [];
      for (const p of curve) {
        const t = p.timestamp.slice(0, 10);
        if (!map.has(t)) map.set(t, { t });
        const row = map.get(t)!;
        row[`eq_${s.id}`] = p.equity;
      }
    });
    return Array.from(map.values()).sort((a, b) => String(a.t).localeCompare(String(b.t)));
  }, [strategies, curves]);

  if (merged.length === 0) {
    return (
      <ChartShell title={`Selection Overlay · ${strategies.length} strategies`}>
        <EmptyChart message="Selected strategies have no equity data yet." />
      </ChartShell>
    );
  }

  return (
    <ChartShell title={`Selection Overlay · ${strategies.length} strategies`} height={260}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={merged} margin={{ top: 8, right: 12, bottom: 0, left: -10 }}>
          <CartesianGrid stroke={GRID_STROKE} strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="t" stroke={AXIS_STROKE} tick={{ fontSize: AXIS_FONT_SIZE }} minTickGap={48} />
          <YAxis stroke={AXIS_STROKE} tick={{ fontSize: AXIS_FONT_SIZE }} />
          <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: '#cbd5e1' }} />
          <Legend wrapperStyle={{ fontSize: 10, color: '#94a3b8' }} iconSize={8} />
          {strategies.map((s, i) => (
            <Line
              key={s.id}
              type="monotone"
              dataKey={`eq_${s.id}`}
              name={`S#${s.id} ${s.name?.slice(0, 24) ?? ''}`}
              stroke={strategyColor(i)}
              strokeWidth={1.3}
              dot={false}
              connectNulls
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </ChartShell>
  );
}

/**
 * Fire `inView` true once the referenced element scrolls within `rootMargin`
 * of the viewport, then disconnect (one-shot — once a card has loaded its
 * curve it stays loaded + cached). Falls back to eager render when
 * IntersectionObserver is unavailable (SSR / very old browsers) so a card can
 * never be permanently hidden.
 */
function useInView<T extends Element>(rootMargin = '200px'): [RefObject<T>, boolean] {
  const ref = useRef<T>(null);
  const [inView, setInView] = useState(false);
  useEffect(() => {
    if (inView) return;
    const el = ref.current;
    if (!el || typeof IntersectionObserver === 'undefined') {
      setInView(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setInView(true);
          observer.disconnect();
        }
      },
      { rootMargin },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [inView, rootMargin]);
  return [ref, inView];
}
