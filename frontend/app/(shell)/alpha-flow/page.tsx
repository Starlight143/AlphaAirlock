'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { GitBranch } from 'lucide-react';
import {
  ResponsiveContainer, Sankey, Tooltip, Layer, Rectangle,
  BarChart, Bar, XAxis, YAxis, CartesianGrid, ComposedChart, Line,
} from 'recharts';

import { api, type AlphaFlowLink, type AlphaFlowNode } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { stageLabel } from '@/lib/stageLabels';

/**
 * /alpha-flow — P7-05
 * Aggregate stage Sankey + drop-out chart + single-strategy timeline.
 */
export default function AlphaFlowPage() {
  const [days, setDays] = useState(30);
  const [strategyId, setStrategyId] = useState<number | null>(null);

  const sankQ = useQuery({
    queryKey: queryKeys.alphaFlowSankey(days),
    queryFn: () => api.alphaFlowSankey(days),
    refetchInterval: 60_000,
  });
  const dropQ = useQuery({
    queryKey: queryKeys.alphaFlowDropout(days),
    queryFn: () => api.alphaFlowDropoutStats(days),
    refetchInterval: 60_000,
  });
  // NOTE: must use raw `api.strategies` (not `.then((r) => r.strategies)`) so
  // every other consumer sharing `queryKeys.strategies` sees the same cached
  // shape `{ strategies: [...] }`. See arena/page.tsx for the full rationale.
  const stratsQ = useQuery({ queryKey: queryKeys.strategies, queryFn: api.strategies });
  const tlQ = useQuery({
    queryKey: queryKeys.alphaFlowTimeline(strategyId ?? -1),  // R5/FE-DATA-004: consistent key shape
    queryFn: () => api.alphaFlowStrategyTimeline(strategyId!),
    enabled: strategyId != null,
  });

  return (
    <div className="flex h-full w-full flex-col gap-3 overflow-y-auto p-3">
      <header className="flex items-center justify-between rounded-xl border border-slate-800 bg-slate-900/40 px-3 py-2">
        <div>
          <h1 className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
            <GitBranch className="h-3.5 w-3.5 text-cyan-400" /> Alpha Flow
          </h1>
          <p className="mt-0.5 text-[11px] text-slate-500">Aggregate stage transitions · per-strategy journeys</p>
        </div>
        <div className="flex overflow-hidden rounded-md border border-slate-700 text-[10px]">
          {[7, 30, 90, 180].map((d) => (
            <button key={d} onClick={() => setDays(d)} className={`px-2 py-1 ${days === d ? 'bg-cyan-500/20 text-cyan-200' : 'text-slate-400 hover:bg-slate-800'}`}>{d}d</button>
          ))}
        </div>
      </header>

      <Card title={`Aggregate Flow · ${sankQ.data?.total_transitions ?? 0} transitions`}>
        <SankeyView nodes={sankQ.data?.nodes ?? []} links={sankQ.data?.links ?? []} />
      </Card>

      <div className="grid grid-cols-12 gap-3">
        <Card title="Drop-out by Stage" className="col-span-7">
          <DropoutChart rows={dropQ.data?.stages ?? []} />
        </Card>
        <Card title="Strategy Timeline" className="col-span-5">
          <div className="mb-2">
            <select
              value={strategyId ?? ''}
              onChange={(e) => setStrategyId(e.target.value ? Number(e.target.value) : null)}
              className="w-full rounded-md border border-slate-700 bg-slate-950 px-2 py-1 text-[11px] text-slate-200"
            >
              <option value="">— pick a strategy —</option>
              {(stratsQ.data?.strategies ?? []).map((s) => {
                // P16/B-L2 — drop the trailing space when the strategy has
                // neither a slug nor a name (legacy template rendered "S#42 ").
                const label = s.slug ?? s.name;
                return (
                  <option key={s.id} value={s.id} title={label ?? `S#${s.id}`}>
                    S#{s.id}{label ? ` ${label}` : ''}
                  </option>
                );
              })}
            </select>
          </div>
          <Timeline events={tlQ.data?.events ?? []} />
        </Card>
      </div>
    </div>
  );
}

function Card({ title, className = '', children }: { title: string; className?: string; children: React.ReactNode }) {
  return (
    <section className={`flex flex-col rounded-xl border border-slate-800 bg-slate-900/40 p-3 ${className}`}>
      <header className="mb-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">{title}</header>
      <div className="flex-1 min-h-[280px]">{children}</div>
    </section>
  );
}

const LINK_COLOR: Record<string, string> = {
  advance: '#06b6d4', reject: '#f43f5e', loop: '#64748b', retreat: '#f59e0b',
};

/**
 * recharts' <Sankey> requires a directed ACYCLIC graph. Its internal
 * `updateDepthOfTargets` walks every link recursively and, on any cycle,
 * recurses forever → "RangeError: Maximum call stack size exceeded" (the crash
 * seen on this page). The backend legitimately emits cyclic links: `retreat`
 * (a strategy bounced back, e.g. CRITIC_LOOP → CODE_GEN) and same-stage `loop`
 * edges both close cycles in the stage graph.
 *
 * We sanitise at the render boundary so recharts can NEVER receive a cycle,
 * whatever the API returns. Every node carries its canonical left-to-right
 * column index (`stage`); keeping only strictly-forward links
 * (`target.stage > source.stage`) is provably acyclic — every path strictly
 * increases `stage`, so no node is reachable from itself. Backward, same-stage
 * and self/out-of-range links are dropped and counted, so the UI can disclose
 * what was hidden instead of lying by omission. Orphaned nodes are pruned and
 * surviving links re-indexed to keep recharts' index-based node references valid.
 */
function toAcyclicSankey(
  nodes: AlphaFlowNode[],
  links: AlphaFlowLink[],
): { nodes: AlphaFlowNode[]; links: AlphaFlowLink[]; hidden: number } {
  const n = nodes.length;
  const forward: AlphaFlowLink[] = [];
  let hidden = 0;
  for (const lk of links) {
    const s = lk.source;
    const t = lk.target;
    const inRange =
      Number.isInteger(s) && Number.isInteger(t) &&
      s >= 0 && t >= 0 && s < n && t < n && s !== t;
    if (inRange && nodes[t].stage > nodes[s].stage) {
      forward.push(lk);
    } else {
      hidden += 1;
    }
  }

  // Prune orphan nodes (untouched by any surviving link) and re-index so
  // recharts' integer node references stay valid against the compacted array.
  const used = new Set<number>();
  for (const lk of forward) {
    used.add(lk.source);
    used.add(lk.target);
  }
  const remap = new Map<number, number>();
  const compactNodes: AlphaFlowNode[] = [];
  for (let i = 0; i < n; i++) {
    if (used.has(i)) {
      remap.set(i, compactNodes.length);
      compactNodes.push({ ...nodes[i] }); // copy: keep react-query cache immutable
    }
  }
  const compactLinks = forward.map((lk) => ({
    ...lk,
    source: remap.get(lk.source) as number,
    target: remap.get(lk.target) as number,
  }));

  return { nodes: compactNodes, links: compactLinks, hidden };
}

function SankeyView({ nodes, links }: { nodes: AlphaFlowNode[]; links: AlphaFlowLink[] }) {
  if (!nodes.length || !links.length) {
    return <Empty>No transitions in window. Once strategies advance, the flow appears here.</Empty>;
  }
  // Break cycles into a DAG before recharts sees the data — otherwise any
  // `retreat`/`loop` link overflows the Sankey depth recursion. See
  // toAcyclicSankey for the proof and the why.
  const { nodes: dagNodes, links: dagLinks, hidden } = toAcyclicSankey(nodes, links);
  if (!dagLinks.length) {
    return (
      <Empty>
        Only backward / same-stage transitions in window — no forward flow to chart yet.
      </Empty>
    );
  }
  // P16/B-M3 — dropped the pre-computed `fill` field on links. The custom
  // `SankeyLink` renderer recomputes the colour from `payload.kind` and
  // never consulted the pre-computed field, so the map was dead code.
  // P16/B-M4 — Sankey emits the same tooltip formatter for both node and
  // link hover; the legacy formatter assumed link-only fields and rendered
  // "undefined transitions · median —" when hovering nodes. Discriminate
  // node vs link via the presence of `kind` (links carry kind, nodes do
  // not).
  const data = { nodes: dagNodes, links: dagLinks };
  return (
    <div className="flex h-full flex-col">
      <ResponsiveContainer width="100%" height={420}>
        <Sankey
          data={data}
          node={(props: any) => <SankeyNode {...props} payload={props.payload} />}
          link={(props: any) => <SankeyLink {...props} />}
          margin={{ top: 8, right: 80, bottom: 8, left: 60 }}
        >
          <Tooltip
            formatter={(_v: number, _n: string, item: any) => {
              const p = item?.payload?.payload ?? item?.payload ?? {};
              if (p.kind != null) {
                return [`${p.value ?? 0} transitions · median ${p.median_dwell_human ?? '—'}`, p.kind];
              }
              return [
                `${p.incoming ?? 0} in · ${p.outgoing ?? 0} out${p.self_loops ? ` · ${p.self_loops} loop` : ''}`,
                p.name,
              ];
            }}
            contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 11 }}
            itemStyle={{ color: '#e2e8f0' }}
            labelStyle={{ color: '#cbd5e1' }}
          />
        </Sankey>
      </ResponsiveContainer>
      {hidden > 0 && (
        <p className="mt-1 text-center text-[9px] text-slate-500">
          {hidden} backward/looping transition{hidden === 1 ? '' : 's'} hidden — Sankey charts forward flow only.
        </p>
      )}
    </div>
  );
}

// P-FIX — cap how thick a single node/link may render. recharts sizes Sankey
// nodes & links proportionally to flow value, so when the window holds just
// one transition (common in a fresh system) that lone link is scaled to the
// FULL canvas height and paints as a solid block that swallows the whole
// chart. Bounding node height + link stroke to a sane band keeps sparse data
// readable as a ribbon; dense data is unaffected because each link's natural
// width is already well below the cap (the height is shared across many flows).
const SANKEY_BAND_MAX = 46;

function SankeyNode({ x, y, width, height, payload }: any) {
  const h = Math.min(height, SANKEY_BAND_MAX);
  const yy = y + (height - h) / 2; // keep the bar centred on its flow band
  return (
    <Layer>
      <Rectangle x={x} y={yy} width={width} height={h} fill="#1e293b" stroke="#334155" />
      <text x={x + width + 6} y={y + height / 2} dy={3} fill="#cbd5e1" fontSize={10} fontFamily="monospace">
        {payload?.name} ({payload?.outgoing ?? 0})
      </text>
    </Layer>
  );
}

function SankeyLink({ sourceX, sourceY, sourceControlX, targetX, targetY, targetControlX, linkWidth, payload }: any) {
  const color = LINK_COLOR[payload?.kind] ?? '#64748b';
  // Clamp the ribbon: floor 2px so it never vanishes, ceiling SANKEY_BAND_MAX so
  // a single dominant flow can't fill the canvas. sourceY/targetY are the band
  // centres recharts hands us, so a centred clamp stays aligned with the nodes.
  const w = Math.min(Math.max(2, linkWidth), SANKEY_BAND_MAX);
  const d = `M${sourceX},${sourceY}C${sourceControlX},${sourceY} ${targetControlX},${targetY} ${targetX},${targetY}`;
  return (
    <path d={d} stroke={color} strokeOpacity={0.45} strokeWidth={w} fill="none" />
  );
}

function DropoutChart({ rows }: { rows: { status: string; label: string; advanced: number; rejected: number; total: number; reject_rate: number | null }[] }) {
  if (!rows.length) return <Empty>No drop-out data in window.</Empty>;
  const data = rows.map((r) => ({
    ...r,
    reject_pct: r.reject_rate != null ? r.reject_rate * 100 : null,
  }));
  return (
    <ResponsiveContainer width="100%" height={280}>
      <ComposedChart data={data} barCategoryGap="20%">
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis dataKey="label" stroke="#475569" tick={{ fontSize: 9 }} />
        <YAxis yAxisId="left" stroke="#475569" tick={{ fontSize: 9 }} allowDecimals={false} />
        <YAxis yAxisId="right" orientation="right" stroke="#f43f5e" tick={{ fontSize: 9 }} domain={[0, 100]} />
        {/* cursor + maxBarSize: with a single stage row the bar would balloon to
            the full plot width and the default near-white tooltip cursor would
            paint the whole band as a giant light block (same failure mode as the
            pipeline-analytics throughput chart). */}
        <Tooltip
          cursor={{ fill: 'rgba(148, 163, 184, 0.08)' }}
          contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 10 }}
        />
        <Bar dataKey="advanced" stackId="t" fill="#22d3ee" yAxisId="left" maxBarSize={56} />
        <Bar dataKey="rejected" stackId="t" fill="#f43f5e" yAxisId="left" maxBarSize={56} />
        <Line type="monotone" dataKey="reject_pct" stroke="#fbbf24" yAxisId="right" dot={false} strokeWidth={2} />
      </ComposedChart>
    </ResponsiveContainer>
  );
}

function Timeline({ events }: { events: { transitioned_at: string | null; to_status: string; from_status: string | null; from_stage?: number | null; to_stage?: number | null; dwell_sec_in_from: number | null; actor: string; reason: string | null }[] }) {
  if (!events.length) return <Empty>Pick a strategy to view its history.</Empty>;
  return (
    <ul className="max-h-[420px] space-y-1 overflow-y-auto text-[11px]">
      {events.map((e, i) => (
        <li key={`${i}-${e.transitioned_at ?? ''}-${e.from_status ?? ''}-${e.to_status}`} className="rounded-md border border-slate-900 bg-slate-950/40 px-2 py-1.5">
          <div className="flex items-center justify-between">
            <span className="font-mono text-slate-200">
              {e.from_status ?? '—'} → <span className="text-cyan-200">{e.to_status}</span>
            </span>
            <span className="text-[9px] text-slate-500">{e.transitioned_at ? new Date(e.transitioned_at).toLocaleString() : ''}</span>
          </div>
          {/* P17/A-H5 + A-M2 — semantic stage labels alongside the raw status enums */}
          <div className="mt-0.5 font-mono text-[9px] text-slate-500">
            {e.from_stage != null ? stageLabel(e.from_stage) : '—'} → <span className="text-cyan-300">{stageLabel(e.to_stage)}</span>
          </div>
          <div className="mt-0.5 flex items-center gap-2 text-[9px] text-slate-500">
            <span>by {e.actor}</span>
            {e.dwell_sec_in_from != null && <span>· dwell {formatDwell(e.dwell_sec_in_from)}</span>}
            {e.reason && <span className="truncate text-slate-400">· {e.reason}</span>}
          </div>
        </li>
      ))}
    </ul>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="flex h-full min-h-[120px] items-center justify-center text-center text-[11px] text-slate-500">{children}</div>;
}

function formatDwell(sec: number): string {
  if (sec < 60) return `${sec.toFixed(1)}s`;
  if (sec < 3600) return `${(sec / 60).toFixed(1)}m`;
  if (sec < 86400) return `${(sec / 3600).toFixed(1)}h`;
  return `${(sec / 86400).toFixed(1)}d`;
}
