'use client';

import { useMemo } from 'react';
import type { FactorGraphNode, FactorGraphResponse } from '@/lib/api';

type Props = {
  node: FactorGraphNode | null;
  data: FactorGraphResponse | undefined;
};

/**
 * Right-rail bar list of the selected node's edges, weighted by the
 * neighbour's PageRank (closest available proxy for connection strength
 * until real correlation edges land in P6).
 */
export default function FactorCorrelations({ node, data }: Props) {
  const neighbours = useMemo(() => {
    if (!node || !data) return [] as { id: string; label: string; weight: number; color: string }[];
    const byId = new Map<string, FactorGraphNode>();
    for (const n of data.nodes) byId.set(n.id, n);
    const rows: { id: string; label: string; weight: number; color: string }[] = [];
    const seen = new Set<string>();
    for (const e of data.edges) {
      let other: FactorGraphNode | undefined;
      if (e.from === node.id) other = byId.get(e.to);
      else if (e.to === node.id) other = byId.get(e.from);
      if (!other || seen.has(other.id)) continue;
      seen.add(other.id);
      const weight = Number(other.pagerank ?? 0) || 0;
      rows.push({ id: other.id, label: other.label, weight, color: other.color });
    }
    rows.sort((a, b) => b.weight - a.weight);
    return rows.slice(0, 12);
  }, [node, data]);

  const totalNeighbours = useMemo(() => {
    if (!node || !data) return 0;
    const distinctIds = new Set<string>();
    for (const e of data.edges) {
      if (e.from === node.id) distinctIds.add(e.to);
      else if (e.to === node.id) distinctIds.add(e.from);
    }
    return distinctIds.size;
  }, [node, data]);

  if (!node) return null;

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        {/* P16/B-M6 — renamed from "Neighbour Centrality". The bar widths
            actually come from each neighbour's own PageRank (global
            importance), NOT a pairwise correlation/centrality weight, so
            the legacy title misled operators into thinking the bars
            represented connection strength to the selected node. Real
            per-pair correlation edges are still on the roadmap. */}
        <span className="text-[10px] font-bold uppercase tracking-widest text-slate-400">
          Neighbours by Global Importance
        </span>
        <span className="rounded border border-slate-800 bg-slate-950 px-1.5 py-0.5 text-[8px] uppercase tracking-wider text-slate-500">
          top {neighbours.length} of {totalNeighbours}
        </span>
      </div>
      {neighbours.length === 0 ? (
        <div className="text-[11px] text-slate-600">
          No neighbours — node is isolated in the current graph.
        </div>
      ) : (
        <div className="space-y-1 font-mono">
          {neighbours.map((n) => {
            const max = neighbours[0].weight || 1;
            return (
              <div key={n.id} className="flex items-center gap-2 text-[10px]">
                <span
                  className="h-2 w-2 shrink-0 rounded-full"
                  style={{ background: n.color }}
                />
                <span className="w-32 truncate text-slate-300">{n.label}</span>
                <div className="flex-1 rounded bg-slate-950">
                  <div
                    className="h-1.5 rounded bg-emerald-500/60"
                    style={{ width: `${Math.max(0.05, n.weight / max) * 100}%` }}
                  />
                </div>
                <span className="w-12 text-right text-slate-500">
                  {n.weight.toFixed(3)}
                </span>
              </div>
            );
          })}
          <div className="pt-1 text-[9px] text-slate-600">
            Ranked by neighbour PageRank — no per-pair weights available yet
            (correlation edges still on roadmap).
          </div>
        </div>
      )}
    </div>
  );
}
