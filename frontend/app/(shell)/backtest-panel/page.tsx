'use client';

import { useCallback, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type AlphaStrategy } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { getIsMerged } from '@/lib/derive';
import KpiStrip from '@/components/backtest-panel/KpiStrip';
import StrategyTable from '@/components/backtest-panel/StrategyTable';
import CombinedChart from '@/components/backtest-panel/CombinedChart';
import GateCriteria from '@/components/backtest-panel/GateCriteria';
import AggregatedMetrics from '@/components/backtest-panel/AggregatedMetrics';
import MultiEquityOverlay from '@/components/backtest-panel/MultiEquityOverlay';
import MultiDailyPnlOverlay from '@/components/backtest-panel/MultiDailyPnlOverlay';
import MultiPositionOverlay from '@/components/backtest-panel/MultiPositionOverlay';
import PipelineTabs, {
  filterByScope,
  TAB_STATUSES,
  type PipelineScope,
} from '@/components/backtest-panel/PipelineTabs';
import ChartsToolbar, {
  type DateRangeKey,
  type OverlayMode,
} from '@/components/backtest-panel/ChartsToolbar';

// P11-F3-01 — pipeline version toggle. 'bg' = BG (single-anchor) pipeline,
// 'merged' = the Stage-5 merged universe surface that joins BG + auto-pipeline
// strategies into one combined backtest layer.
type PipelineKind = 'bg' | 'merged';

// C-L8 — env gating hoisted to module top so the gating intent is visible at
// a glance and the dead JSX never sneaks into a bundle when the flag is unset.
// Next.js NEXT_PUBLIC_* vars are inlined at build time so this constant is
// tree-shaken in production builds where the flag is absent.
//
// NOTE: flipping NEXT_PUBLIC_MERGED_PIPELINE_ENABLED at runtime has no
// effect — Next.js bakes the value into the JS bundle at `next build` time.
// To toggle: update .env (or the deploy-target env), then run a fresh
// `next build` so the bundle picks up the new constant.
const MERGED_PIPELINE_ENABLED = process.env.NEXT_PUBLIC_MERGED_PIPELINE_ENABLED === '1';

/**
 * /backtest-panel — P5 layout (reference UI parity).
 *
 *   ROW 1: 8-tile KPI strip
 *   ROW 2: Research / Development / Backtest / Paper / Live tabs (V2 statuses)
 *   ROW 3: Full-width strategy table — already filtered to the active scope
 *   ROW 4: Performance Charts toolbar (ALL/overlaid | COMBINED + WeightingPicker)
 *   ROW 5: Multi-strategy overlay (overlaid mode) OR combined portfolio chart group (combined mode)
 *   ROW 6: BG Pipeline Gate Criteria checklist (mirrored on /gate-review)
 */
export default function BacktestPanelPage() {
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [method, setMethod] = useState<string>('equal_weight');
  const [mode, setMode] = useState<OverlayMode>('overlaid');
  const [scope, setScope] = useState<PipelineScope>('paper');
  // P12 — date-range chip state lifted into the page so the chip selection
  // can drive every overlay chart consistently (rather than each chart
  // tracking its own range).
  const [range, setRange] = useState<DateRangeKey>('ALL');
  // P11-F3-01 — BG vs Merged pipeline filter. Wraps the existing scope
  // filter so the pipeline tabs still operate on the (BG or Merged) subset.
  const [pipelineKind, setPipelineKind] = useState<PipelineKind>('bg');

  const toggle = useCallback((id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // P11-F3-01 fix — clear the selection whenever the pipeline kind changes so
  // that stale IDs from the previous pipeline do not silently reappear when the
  // operator switches back, and so cross-pipeline ghost selections are impossible.
  const handleKindChange = useCallback(
    (kind: PipelineKind) => {
      setPipelineKind(kind);
      setSelected(new Set());
    },
    [],
  );

  const strategiesQ = useQuery({
    queryKey: queryKeys.strategies,
    queryFn: api.strategies,
    refetchInterval: 8_000,
  });
  const all: AlphaStrategy[] = useMemo(
    () => strategiesQ.data?.strategies ?? [],
    [strategiesQ.data],
  );

  // P11-F3-01 — apply BG/Merged filter before scope filter so the pipeline
  // tabs only ever see strategies from the active version pipeline.
  const kindFiltered = useMemo(() => {
    if (pipelineKind === 'merged') {
      return all.filter((s) => getIsMerged(s));
    }
    return all.filter((s) => !getIsMerged(s));
  }, [all, pipelineKind]);

  const visible = useMemo(() => filterByScope(kindFiltered, scope), [kindFiltered, scope]);
  const selectedStrategies = useMemo(
    () => kindFiltered.filter((s) => selected.has(s.id)),
    [kindFiltered, selected],
  );
  const ids = selectedStrategies.map((s) => s.id).sort((a, b) => a - b);

  return (
    <div className="flex h-full w-full flex-col gap-3 overflow-y-auto p-3">
      {/* P6-M08 — page subtitle that the reference screenshot shows above the KPI strip. */}
      <header className="rounded-xl border border-slate-800 bg-slate-900/40 px-3 py-2">
        <h1 className="text-sm font-bold tracking-widest text-slate-100">BACKTEST PANEL</h1>
        <p className="mt-0.5 text-[11px] text-slate-500">
          Cross-strategy performance comparator: select multiple strategies, overlay or combine via 9 weighting methods, evaluate against the {MERGED_PIPELINE_ENABLED && pipelineKind === 'merged' ? 'Merged' : 'BG'} Pipeline Gate Criteria.
        </p>
      </header>

      {/* P12 — hidden until backend Stage-5 merged-universe spawner lands.
          Without that backend support, flipping to "Merged" only ever shows
          an empty table because no strategy has config.is_merged === true.
          Gate behind NEXT_PUBLIC_MERGED_PIPELINE_ENABLED=1 so QA can opt-in. */}
      {MERGED_PIPELINE_ENABLED && (
        <div className="flex items-center gap-2 rounded-xl border border-slate-800 bg-slate-900/40 px-3 py-2">
          <span className="text-[10px] uppercase tracking-widest text-slate-500">
            Version Pipeline
          </span>
          <button
            type="button"
            onClick={() => setPipelineKind('bg')}
            className={
              pipelineKind === 'bg'
                ? 'rounded-md border border-cyan-700 bg-cyan-500/15 px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-cyan-200'
                : 'rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-slate-400 hover:text-slate-200'
            }
          >
            BG Pipeline
          </button>
          <button
            type="button"
            onClick={() => setPipelineKind('merged')}
            className={
              pipelineKind === 'merged'
                ? 'rounded-md border border-cyan-700 bg-cyan-500/15 px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-cyan-200'
                : 'rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-slate-400 hover:text-slate-200'
            }
          >
            Merged Pipeline
          </button>
        </div>
      )}

      <KpiStrip strategies={all} />

      {/* Pipeline scope tabs */}
      <div className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
        <PipelineTabs active={scope} onChange={setScope} strategies={kindFiltered} />
        <div className="border-b border-slate-800 px-3 py-2 text-[10px] uppercase tracking-widest text-slate-500">
          {visible.length} strategies in scope · {selectedStrategies.length} selected
        </div>
        <div className="max-h-[40vh] overflow-y-auto">
          <StrategyTableScoped
            scope={scope}
            selectedIds={selected}
            toggle={toggle}
            pipelineKind={pipelineKind}
            strategies={all}
          />
        </div>
      </div>

      {/* Performance Charts row */}
      <div className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
        <ChartsToolbar
          mode={mode}
          onModeChange={setMode}
          method={method}
          onMethodChange={setMethod}
          selectedCount={selectedStrategies.length}
          range={range}
          onRangeChange={setRange}
        />
        <div className="flex flex-col gap-3 p-3">
          {mode === 'overlaid' ? (
            <>
              <MultiEquityOverlay strategies={selectedStrategies} range={range} />
              <MultiDailyPnlOverlay strategies={selectedStrategies} range={range} />
              <MultiPositionOverlay strategies={selectedStrategies} range={range} />
            </>
          ) : (
            <CombinedChart
              strategyIds={ids}
              method={method}
              range={range}
              selectedStrategies={selectedStrategies}
            />
          )}
        </div>
      </div>

      {/* P11-F3-03 — aggregated metrics strip pinned beneath the charts.
          Re-uses /api/portfolio/combine so the four tiles always reflect the
          current weighting method even when the operator hasn't opened the
          COMBINED chart mode yet.
          P12 (L4) — wrapped in a header card so the empty-state has framing
          context instead of floating bare four em-dashes on the page. */}
      <div className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
        <div className="border-b border-slate-800 px-3 py-2">
          <div className="text-[10px] uppercase tracking-widest text-slate-500">Aggregated Metrics</div>
          {/* P13 (C-L2) — only show the empty-state hint when nothing is
              selected yet; once populated the line is stale and confuses
              the operator into thinking the picker is broken. */}
          {ids.length === 0 && (
            <div className="mt-0.5 text-[10px] text-slate-600">Pick strategies above to populate aggregated metrics.</div>
          )}
        </div>
        <div className="p-3">
          <AggregatedMetricsRow ids={ids} method={method} />
        </div>
      </div>

      {/* Gate Criteria — mirrored here for at-a-glance + on /gate-review with promote actions */}
      <div className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
        <GateCriteria />
      </div>
    </div>
  );
}

/**
 * F3 — thin wrapper that pushes the scope predicate down into StrategyTable's
 * `filter` prop. StrategyTable still owns the /api/strategies query, but the
 * rendered subset now matches the chosen pipeline tab exactly.
 *
 * P11-F3-01 — extended with a `pipelineKind` prop so the BG / Merged toggle
 * also filters the rendered table.
 */
function StrategyTableScoped({
  scope,
  selectedIds,
  toggle,
  pipelineKind,
  strategies,
}: {
  scope: PipelineScope;
  selectedIds: Set<number>;
  toggle: (id: number) => void;
  pipelineKind: PipelineKind;
  strategies: AlphaStrategy[];
}) {
  const allowed = useMemo(() => new Set(TAB_STATUSES[scope]), [scope]);
  const filter = useCallback(
    (s: AlphaStrategy) => {
      if (!allowed.has((s.status || '').toUpperCase())) return false;
      const isMerged = getIsMerged(s);
      if (pipelineKind === 'merged' && !isMerged) return false;
      if (pipelineKind === 'bg' && isMerged) return false;
      return true;
    },
    [allowed, pipelineKind],
  );
  return (
    <StrategyTable selectedIds={selectedIds} toggle={toggle} filter={filter} strategies={strategies} />
  );
}

/**
 * P11-F3-03 — pinned aggregated metrics strip. Pulls combined-portfolio
 * metrics from /api/portfolio/combine for the currently-selected strategies
 * and forwards them to <AggregatedMetrics>. When the operator hasn't picked
 * any strategies yet, we still render the four tiles in their em-dash
 * placeholder state (AggregatedMetrics handles that via `n_strategies===0`).
 */
function AggregatedMetricsRow({
  ids,
  method,
}: {
  ids: number[];
  method: string;
}) {
  const enabled = ids.length >= 1;
  const q = useQuery({
    queryKey: queryKeys.portfolioCombine(ids, method),
    queryFn: () => api.portfolioCombine(ids, method),
    enabled,
    staleTime: 30_000,
  });
  if (!enabled) {
    return (
      <AggregatedMetrics
        data={{
          method,
          method_label: method,
          weights: {},
          equity_curve: [],
          metrics: {},
          n_strategies: 0,
          n_aligned_days: 0,
          missing: [],
        }}
      />
    );
  }
  if (q.isLoading) {
    return (
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <div
            key={i}
            className="h-[58px] animate-pulse rounded-md border border-slate-800 bg-slate-950/50"
          />
        ))}
      </div>
    );
  }
  if (q.isError || !q.data) {
    return (
      <AggregatedMetrics
        data={{
          method,
          method_label: method,
          weights: {},
          equity_curve: [],
          metrics: {},
          n_strategies: 0,
          n_aligned_days: 0,
          missing: [],
        }}
      />
    );
  }
  return <AggregatedMetrics data={q.data} />;
}
