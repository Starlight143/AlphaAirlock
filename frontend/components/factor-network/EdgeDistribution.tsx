'use client';

import type { FactorGraphResponse } from '@/lib/api';

type Props = { data: FactorGraphResponse | undefined };

/**
 * Right-rail edge distribution histogram — counts of edges grouped by the
 * "from" node's category. Drives the reference UI's edge-frequency mini-chart.
 */
export default function EdgeDistribution({ data }: Props) {
  const items = data?.edge_distribution ?? [];
  const total = items.reduce((a, b) => a + (Number(b.count) || 0), 0);
  if (items.length === 0) {
    return (
      <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
        <div className="mb-2 text-[10px] font-bold uppercase tracking-widest text-slate-400">
          Edge Distribution
        </div>
        <div className="text-[11px] text-slate-600">No edges yet.</div>
      </div>
    );
  }
  const max = Math.max(...items.map((i) => i.count), 1);
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
      <div className="mb-2 flex items-baseline justify-between">
        <span className="text-[10px] font-bold uppercase tracking-widest text-slate-400">
          Edge Distribution
        </span>
        <span className="text-[9px] text-slate-600">{total} edges</span>
      </div>
      <div className="space-y-1">
        {items.map((i) => (
          <div key={i.category} className="flex items-center gap-2 text-[10px] font-mono">
            <span className="w-28 truncate text-slate-400">{i.category}</span>
            <div className="flex-1 rounded bg-slate-950">
              <div
                className="h-2 rounded bg-cyan-500/60"
                style={{ width: `${(i.count / max) * 100}%` }}
              />
            </div>
            <span className="w-10 text-right text-slate-300">{i.count}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
