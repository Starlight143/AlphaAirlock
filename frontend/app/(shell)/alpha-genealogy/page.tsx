'use client';

import { useState } from 'react';
import dynamic from 'next/dynamic';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { Brain } from 'lucide-react';

import { api, type AgForest, type AgTreeNode } from '@/lib/api';
import { queryKeys } from '@/lib/query';

const GenealogyTree = dynamic(() => import('@/components/alpha-genealogy/GenealogyTree'), {
  ssr: false,
  loading: () => <div className="flex h-full w-full items-center justify-center text-xs text-slate-500">Loading tree…</div>,
});

export default function AlphaGenealogyPage() {
  const forestQ = useQuery({ queryKey: queryKeys.agForest, queryFn: api.agForest, staleTime: 30_000 });
  const [selected, setSelected] = useState<AgTreeNode | null>(null);
  const stats = forestQ.data?.stats;

  return (
    <div className="grid h-full w-full grid-cols-12 gap-3 overflow-hidden p-3">
      <section className="col-span-9 flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
        <header className="flex items-center justify-between border-b border-slate-800 px-4 py-2">
          <h1 className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
            <Brain className="h-3.5 w-3.5 text-cyan-400" /> Alpha Genealogy
          </h1>
          <div className="flex items-center gap-3 text-[10px] text-slate-500">
            <span>{stats?.n_roots ?? 0} roots</span>
            <span>{stats?.n_strategies ?? 0} strategies</span>
            <span>depth {stats?.max_depth ?? 0}</span>
          </div>
        </header>
        <div className="flex-1 overflow-hidden">
          <GenealogyTree trees={forestQ.data?.trees ?? []} onSelect={setSelected} />
        </div>
      </section>

      <aside className="col-span-3 flex flex-col gap-3 overflow-y-auto">
        <ForestStats stats={stats} />
        <NodeInspector node={selected} />
      </aside>
    </div>
  );
}

// P16/B-L8 — use the shared `AgForest['stats']` type instead of a local
// re-declaration that risked drifting from the backend payload as fields
// were added/removed.
function ForestStats({ stats }: { stats?: AgForest['stats'] }) {
  if (!stats) return <Card title="Forest Stats"><Empty>Loading…</Empty></Card>;
  const items = [
    { label: 'Roots', value: stats.n_roots, cls: 'text-cyan-200' },
    { label: 'Max depth', value: stats.max_depth, cls: 'text-cyan-200' },
    { label: 'Fertile', value: stats.n_fertile, cls: 'text-emerald-300' },
    { label: 'Trapped', value: stats.n_trapped, cls: 'text-rose-300' },
  ];
  return (
    <Card title="Forest Stats">
      <div className="grid grid-cols-2 gap-2">
        {items.map((it) => (
          <div key={it.label} className="rounded-md border border-slate-800 bg-slate-950/50 p-2">
            <div className={`font-mono text-lg leading-none ${it.cls}`}>{it.value}</div>
            <div className="mt-1 text-[9px] uppercase tracking-wider text-slate-500">{it.label}</div>
          </div>
        ))}
      </div>
      <div className="mt-2 space-y-0.5 text-[10px] text-slate-500">
        Improving: <span className="font-mono text-emerald-300">{stats.n_improving}</span>
        <span className="ml-2">Barren: <span className="font-mono text-slate-400">{stats.n_barren}</span></span>
        {stats.n_cycle > 0 && <span className="ml-2">Cycle: <span className="font-mono text-rose-400">{stats.n_cycle}</span></span>}
      </div>
    </Card>
  );
}

function NodeInspector({ node }: { node: AgTreeNode | null }) {
  if (!node) return <Card title="Inspector"><Empty>Click a node to inspect.</Empty></Card>;
  return (
    <Card title="Inspector">
      <div className="space-y-2 text-[11px]">
        <Link href={`/strategies/${node.id}`} className="font-mono text-cyan-300 hover:underline">
          S#{node.id} {node.name}
        </Link>
        <div className="text-[10px] text-slate-500">{node.status} · stage {node.stage}</div>
        <div className="grid grid-cols-2 gap-2 font-mono">
          <KV label="Sharpe" value={node.sharpe != null ? node.sharpe.toFixed(2) : '—'} />
          <KV label="DD" value={node.max_drawdown != null ? `${(node.max_drawdown * 100).toFixed(1)}%` : '—'} />
          <KV label="Depth" value={String(node.depth)} />
          <KV label="Children" value={String(node.children.length)} />
        </div>
        {node.badges.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {node.badges.map((b) => (
              <span key={b} className={`rounded px-1.5 py-0.5 text-[9px] uppercase tracking-wider ${badgeCls(b)}`}>{b}</span>
            ))}
          </div>
        )}
        {node.postmortem_node_id && (
          <div className="text-[10px] text-slate-500">
            Postmortem: <Link href={`/kb-explorer?node=${node.postmortem_node_id}`} className="text-cyan-300 hover:underline">K#{node.postmortem_node_id}</Link>
          </div>
        )}
      </div>
    </Card>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
      <header className="mb-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">{title}</header>
      {children}
    </section>
  );
}

function KV({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-slate-800 bg-slate-950/50 p-1.5">
      <div className="text-[9px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className="text-cyan-200">{value}</div>
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="py-4 text-center text-[11px] text-slate-500">{children}</div>;
}

function badgeCls(badge: string): string {
  if (badge === 'fertile') return 'bg-cyan-500/20 text-cyan-200';
  if (badge === 'improving') return 'bg-emerald-500/20 text-emerald-200';
  if (badge === 'trapped') return 'bg-rose-500/20 text-rose-200';
  if (badge === 'barren') return 'bg-slate-700/30 text-slate-400';
  if (badge === 'cycle') return 'bg-rose-600/30 text-rose-100';
  return 'bg-slate-800 text-slate-300';
}
