'use client';

import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type PipelineBucket } from '@/lib/api';
import { queryKeys } from '@/lib/query';

/**
 * P13 A-H3 — Mission Control left rail KPIs, 2-column dense grid (12 tiles).
 *
 * Sourced from /api/pipeline/buckets + /api/strategies + /api/graph. The
 * P12 8-tile slice deliberately dropped `total`, `graveyard`, `paper_trade`
 * to declutter — reference screenshots (19-42-03, 19-50-24) actually show
 * a denser ~12-16 tile rail, so the three buckets are restored alongside
 * a `Rejected` tile so the operator sees both win/loss counts symmetrically.
 */
export default function KpiRail() {
  const strategiesQ = useQuery({
    queryKey: queryKeys.strategies,
    queryFn: api.strategies,
    refetchInterval: 6_000,
  });
  const bucketsQ = useQuery({
    queryKey: queryKeys.pipelineBuckets,
    queryFn: api.pipelineBuckets,
    refetchInterval: 4_000,
  });
  const graphQ = useQuery({
    queryKey: queryKeys.graph,
    queryFn: api.graph,
    refetchInterval: 15_000,
  });
  // T2-C — true KB count + vital signs (graph payload caps at 1000).
  const statsQ = useQuery({
    queryKey: queryKeys.knowledgeStats,
    queryFn: api.knowledgeStats,
    refetchInterval: 15_000,
  });
  // T-FINOPS — rolling LLM spend from the per-call cost ledger.
  const costQ = useQuery({
    queryKey: queryKeys.costSummary,
    queryFn: () => api.costSummary(7),
    refetchInterval: 30_000,
  });

  const kpis = useMemo(() => {
    const strategies = strategiesQ.data?.strategies ?? [];
    const buckets: PipelineBucket[] = bucketsQ.data?.buckets ?? [];
    // Count strategies that have ever reached or passed the approval gate.
    // Backend bucket index 4 ("paper_trade") groups statuses ['APPROVED','PAPER_TRADE'],
    // meaning PAPER_TRADE is a post-approval state — strategies that graduate from
    // APPROVED to PAPER_TRADE must still be counted as "approved". SMALL_CAPITAL and
    // LIVE are further downstream and likewise imply prior approval.
    const APPROVED_OR_BEYOND = new Set(['APPROVED', 'PAPER_TRADE', 'SMALL_CAPITAL', 'LIVE']);
    const approved = strategies.filter((s) => APPROVED_OR_BEYOND.has(s.status)).length;

    const pickCount = (...keys: string[]) => {
      for (const k of keys) {
        const b = buckets.find((x) => x.key === k);
        if (b) return b.count;
      }
      return 0;
    };

    const live = pickCount('live', 'live_trade');
    const small = pickCount('small_capital');

    const activeInPipeline = buckets
      .filter((b) => b.index >= 0 && b.index <= 3)  // stages 0-3: INTAKE→Full Backtest
      .reduce((sum, b) => sum + b.count, 0);

    const nodes = graphQ.data?.nodes ?? [];
    const edges = graphQ.data?.edges ?? [];
    // P12 A-M1 — "Knowledge Files" is the count of `knowledge` nodes in the
    // /api/graph payload; "Concept Edges" is the edge count.
    // P16 A-L2 — drop `(n: any)` cast; GraphNode.kind is already a
    // typed union that includes 'knowledge'.
    // T2-C — prefer the TRUE total (the graph-derived count saturates at the
    // 1000-node render cap); fall back to the graph count while stats load.
    const trueKnowledge = statsQ.data?.total_knowledge_nodes;
    const knowledgeFiles = trueKnowledge ?? nodes.filter((n) => n.kind === 'knowledge').length;
    const conceptEdges = edges.length;
    const vitals = statsQ.data?.vital_signs;
    const llmCost7d = costQ.data?.total_cost_usd ?? null;
    const llmCostToday = costQ.data?.today_cost_usd ?? null;

    const sharpes = strategies
      .map((s) => Number(s.metrics?.annualized_sharpe))
      .filter((x) => Number.isFinite(x));
    const avgSharpe = sharpes.length
      ? sharpes.reduce((a, b) => a + b, 0) / sharpes.length
      : null;

    // P12 A-M1 — average information coefficient. Backend stores IC under
    // metrics.ic OR metrics.spearman_ic depending on the daemon; tolerate
    // both so missing strategies don't poison the mean.
    const ics = strategies
      .map((s) => {
        const m = s.metrics as Record<string, number> | undefined;
        const ic = Number(m?.ic);
        if (Number.isFinite(ic)) return ic;
        const sic = Number(m?.spearman_ic);
        return Number.isFinite(sic) ? sic : NaN;
      })
      .filter((x) => Number.isFinite(x));
    const avgIC = ics.length
      ? ics.reduce((a, b) => a + b, 0) / ics.length
      : null;

    const rejected = strategies.filter((s) => s.status === 'REJECTED').length;
    const pastAlphas = strategies.length;

    const totalStrategies = strategies.length;
    const graveyard = pickCount('graveyard');
    const paperTrade = pickCount('paper_trade');

    return [
      {
        label: 'Past Alphas',
        value: pastAlphas.toString(),
        secondary: approved.toString(),
      },
      {
        label: 'Knowledge Files',
        value: knowledgeFiles.toString(),
        secondary: null,
      },
      {
        label: 'Concept Edges',
        value: conceptEdges.toString(),
        secondary: null,
      },
      {
        label: 'Total',
        value: totalStrategies.toString(),
        secondary: null,
      },
      {
        label: 'IC Avg',
        value: avgIC == null ? '—' : avgIC.toFixed(3),
        secondary: null,
      },
      {
        label: 'Avg Sharpe',
        value: avgSharpe == null ? '—' : avgSharpe.toFixed(2),
        secondary: null,
      },
      {
        label: 'Approved',
        value: approved.toString(),
        secondary: null,
      },
      {
        label: 'Rejected',
        value: rejected.toString(),
        secondary: null,
      },
      {
        label: 'In Pipeline',
        value: activeInPipeline.toString(),
        secondary: null,
      },
      {
        label: 'Paper Trade',
        value: paperTrade.toString(),
        secondary: null,
      },
      {
        label: 'Live',
        value: live.toString(),
        secondary: small.toString(),
      },
      {
        label: 'Graveyard',
        value: graveyard.toString(),
        secondary: null,
      },
      // T2-C — KB vital signs ('—' while loading / on cold graph).
      {
        label: 'KB Diversity',
        value: vitals == null ? '—' : vitals.diversity_index.toFixed(2),
        secondary: null,
      },
      {
        label: 'Gap Pressure',
        value: vitals == null ? '—' : vitals.gap_pressure.toFixed(1),
        secondary: null,
      },
      {
        label: 'Bridges',
        value: vitals == null ? '—' : vitals.bridge_count.toString(),
        secondary: null,
      },
      {
        label: 'Orphan %',
        value: vitals == null ? '—' : (vitals.orphan_rate * 100).toFixed(0),
        secondary: null,
      },
      // T-FINOPS — trailing-7d estimated LLM spend; secondary = today's spend.
      {
        label: 'LLM Cost 7d',
        value: llmCost7d == null ? '—' : `$${llmCost7d.toFixed(2)}`,
        secondary: llmCostToday == null ? null : `$${llmCostToday.toFixed(2)}`,
      },
    ] as { label: string; value: string; secondary: string | null }[];
  }, [strategiesQ.data, bucketsQ.data, graphQ.data, statsQ.data, costQ.data]);

  return (
    <aside className="flex h-full flex-col overflow-y-auto rounded-xl border border-slate-800 bg-slate-900/50 p-2">
      <h2 className="mb-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">
        System Metrics
      </h2>
      <div className="grid grid-cols-2 gap-1">
        {kpis.map((k) => (
          <div key={k.label} className="rounded-md border border-slate-800 bg-slate-950/50 p-1.5">
            <div className="flex items-baseline gap-1 font-mono text-base leading-none">
              <span className="text-cyan-300">{k.value}</span>
              {k.secondary && (
                <>
                  <span className="text-slate-600">·</span>
                  {/* P13 A-L4 — rose-300 matches reference UI accent. */}
                  <span
                    className="text-xs text-rose-300"
                    title={k.label === 'Live' ? 'Small Capital' : k.label === 'Past Alphas' ? 'Ever Approved (incl. Paper Trade, Small Cap, Live)' : k.label}
                  >
                    {k.secondary}
                  </span>
                  {k.label === 'Live' && (
                    <span className="ml-0.5 text-[8px] uppercase tracking-wider text-slate-600">
                      sc
                    </span>
                  )}
                </>
              )}
            </div>
            <div className="mt-0.5 text-[9px] uppercase tracking-wider text-slate-500">{k.label}</div>
          </div>
        ))}
      </div>
    </aside>
  );
}
