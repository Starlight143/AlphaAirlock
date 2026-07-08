'use client';

import { useEffect, useMemo, useState } from 'react';
import dynamic from 'next/dynamic';
import { useQuery } from '@tanstack/react-query';
import { Network, Eye, EyeOff } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import type { FactorGraphNode } from '@/lib/api';
import { api } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { markdownComponents, remarkMathPlugins, rehypeMathPlugins } from '@/lib/markdownComponents';
import { safeExternalUrl } from '@/lib/safeUrl';
import FactorNetworkToolbar, {
  DEFAULT_FACTOR_FILTERS,
  type FactorNetworkFilters,
} from '@/components/factor-network/FactorNetworkToolbar';
import EdgeDistribution from '@/components/factor-network/EdgeDistribution';
import FactorCorrelations from '@/components/factor-network/FactorCorrelations';
import MetricDistribution from '@/components/factor-network/MetricDistribution';

// vis-network is browser-only.
const FactorGraph = dynamic(
  () => import('@/components/factor-network/FactorGraph'),
  {
    ssr: false,
    loading: () => (
      <div className="flex h-full w-full items-center justify-center text-xs text-slate-500">
        Loading factor network…
      </div>
    ),
  },
);

// P16/B-L5 — small helper so loading state shows the plural label (matches
// the "—" placeholder rest of the strip uses) instead of falsely
// implying a single-item count.
function plural(n: number | undefined, sing: string, pl: string): string {
  if (n === undefined) return pl;
  return n === 1 ? sing : pl;
}

/**
 * /factor-network — interactive factor graph (P5 layout).
 *   - Top: filter toolbar (category chips, shape, scope, granger DAG, reset)
 *   - Center: vis-network canvas, filterable via DataSet.update()
 *   - Right rail: Node Inspector + Factor Correlations + Edge Distribution
 *     + Network Stats (PageRank / Communities / Granger placeholders)
 */
export default function FactorNetworkPage() {
  const [selected, setSelected] = useState<FactorGraphNode | null>(null);
  const [filters, setFilters] = useState<FactorNetworkFilters>(DEFAULT_FACTOR_FILTERS);

  const q = useQuery({
    queryKey: queryKeys.factorNetwork,
    queryFn: api.factorNetwork,
    staleTime: 60_000,
  });

  return (
    <div className="grid h-full w-full grid-cols-12 gap-3 overflow-hidden p-3">
      <section className="col-span-9 flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
        <header className="flex items-center justify-between border-b border-slate-800 px-4 py-2">
          <h1 className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
            <Network className="h-3.5 w-3.5 text-cyan-400" />
            Factor Network
          </h1>
          <div className="flex items-center gap-3 text-[10px] text-slate-500">
            <span>
              {q.data?.n_factors ?? '—'} {plural(q.data?.n_factors, 'factor', 'factors')}
            </span>
            <span>
              {q.data?.nodes.length ?? '—'} {plural(q.data?.nodes.length, 'node', 'nodes')} sourced
            </span>
            <span>
              {/* P12/C-L3 — when the backend has no Granger edges at all AND
                  hasn't seen any in the last 24h we surface a "cold start"
                  marker instead of the misleading "0 new granger" copy.
                  P16/B-M5 — also show "— granger" while loading instead of
                  "0 new granger" so the column matches the rest of the
                  strip's `—` placeholder during the initial query. */}
              {!q.data
                ? '— granger'
                : q.data.n_granger_p === 0 && q.data.n_granger_new_24h === 0
                ? '— granger cold start'
                : `${q.data.n_granger_new_24h ?? 0} new granger`}
            </span>
            <span>
              computed{' '}
              {q.data?.computed_at
                ? new Date(q.data.computed_at).toLocaleString()
                : '—'}
            </span>
          </div>
        </header>
        <FactorNetworkToolbar
          data={q.data}
          filters={filters}
          onChange={setFilters}
          onReset={() => setFilters(DEFAULT_FACTOR_FILTERS)}
        />
        <div className="flex-1 overflow-hidden">
          <FactorGraph onNodeSelect={setSelected} filters={filters} />
        </div>
      </section>

      <aside aria-label="Inspector and stats" className="col-span-3 flex flex-col gap-3 overflow-y-auto">
        <KpiGrid
          factorCount={q.data?.n_factors ?? 0}
          nodeCount={q.data?.nodes.length ?? 0}
          edgeCount={q.data?.n_edges ?? 0}
          communities={q.data?.n_communities ?? 0}
          grangerP={q.data?.n_granger_p ?? 0}
          ffEdges={q.data?.n_factor_factor_edges ?? 0}
          isolates={q.data?.n_isolates ?? 0}
        />
        <NodeInspector node={selected} />
        <FactorCorrelations node={selected} data={q.data} />
        <MetricDistribution data={q.data} />
        <EdgeDistribution data={q.data} />
      </aside>
    </div>
  );
}


function KpiGrid({
  factorCount,
  nodeCount,
  edgeCount,
  communities,
  grangerP,
  ffEdges,
  isolates,
}: {
  factorCount: number;
  nodeCount: number;
  edgeCount: number;
  communities: number;
  grangerP: number;
  ffEdges: number;
  isolates: number;
}) {
  // A-M3: 6 KPI tiles (Factors / Nodes / Edges F-F / Communities / Granger P / Isolates).
  const tiles = [
    { label: 'Factors', value: factorCount, color: 'text-cyan-200' },
    { label: 'Nodes Sourced', value: nodeCount, color: 'text-slate-200' },
    { label: 'Edges F-F', value: ffEdges, color: 'text-emerald-200' },
    { label: 'Communities', value: communities, color: 'text-amber-200' },
    { label: 'Granger Edges', value: grangerP, color: 'text-cyan-200' },
    { label: 'Isolates', value: isolates, color: isolates > 0 ? 'text-rose-300' : 'text-slate-400' },
  ];
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
      <div className="mb-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">
        Network Stats
      </div>
      <div className="grid grid-cols-3 gap-2">
        {tiles.map((t) => (
          <div key={t.label} className="rounded-md border border-slate-800 bg-slate-950/50 p-2">
            <div className={`font-mono text-lg leading-none ${t.color}`}>{t.value}</div>
            <div className="mt-1 text-[9px] uppercase tracking-wider text-slate-500">{t.label}</div>
          </div>
        ))}
      </div>
      <div className="mt-2 text-[10px] text-slate-500">
        Total edges: <span className="font-mono text-slate-300">{edgeCount}</span>
      </div>
    </div>
  );
}

function NodeInspector({ node }: { node: FactorGraphNode | null }) {
  // P6-D06: inline markdown preview on knowledge-node click. Only KnowledgeNodes
  // (id prefix "k") have backend content; strategy nodes (prefix "s") show
  // metadata only.
  const [previewOpen, setPreviewOpen] = useState(false);
  const numericId = useMemo(() => {
    if (!node?.id?.startsWith('k')) return null;
    const n = Number(node.id.slice(1));
    return Number.isFinite(n) ? n : null;
  }, [node?.id]);
  const previewQ = useQuery({
    queryKey: queryKeys.knowledgeOne(numericId ?? 0),
    queryFn: () => api.knowledgeOne(numericId as number),
    enabled: previewOpen && numericId != null,
    staleTime: 60_000,
  });
  // Reset preview when the selected node changes to a non-knowledge node (e.g.,
  // strategy nodes), so the '## Markdown Preview ##' heading is never shown
  // without content beneath it.
  useEffect(() => {
    if (numericId == null) setPreviewOpen(false);
  }, [numericId]);

  if (!node) {
    return (
      <div className="flex h-32 shrink-0 items-center justify-center rounded-xl border border-slate-800 bg-slate-900/40 p-4">
        <div className="text-center text-xs text-slate-500">
          Click a node to inspect PageRank,
          <br />
          neighbours and source URL.
        </div>
      </div>
    );
  }
  return (
    // shrink-0: NodeInspector sits inside an `aside` that is `flex flex-col overflow-y-auto`.
    // Because this card has `overflow-hidden` (for rounded-xl clip), its implicit
    // `min-height: auto` resolves to 0 per the CSS flex spec, which lets the flex
    // parent crush it down to just the header height when sibling cards are tall.
    // The body's KV list (incl. the "Granger p" row) then collapses to a 1-line
    // strip. `shrink-0` opts this card out of flex shrinking so it keeps its
    // natural height; the outer `aside` already provides vertical scrolling.
    <div className="flex shrink-0 flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
      <header className="border-b border-slate-800 px-4 py-2">
        <div className="flex items-center justify-between">
          <div className="text-[10px] font-bold uppercase tracking-[0.2em] text-cyan-300">
            Node Inspector
          </div>
          {numericId != null && (
            <button
              onClick={() => setPreviewOpen((v) => !v)}
              className="inline-flex items-center gap-1 rounded border border-slate-700 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-slate-400 hover:bg-slate-800 hover:text-cyan-300"
              title="Toggle KnowledgeNode markdown preview"
            >
              {previewOpen ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
              {previewOpen ? 'Hide content' : 'Show content'}
            </button>
          )}
        </div>
        <div className="mt-1 font-mono text-xs text-slate-200">{node.id}</div>
        <div className="text-xs text-slate-100">{node.title}</div>
      </header>
      <div className="px-4 py-3 text-[11px] text-slate-300">
        <KV label="Type" value={node.kind} />
        <KV label="Category" value={node.category ?? '—'} />
        <KV label="IC" value={node.ic_score != null && Number.isFinite(node.ic_score) ? node.ic_score.toFixed(2) : '—'} />
        <KV label="PageRank" value={node.pagerank != null && Number.isFinite(node.pagerank) ? node.pagerank.toFixed(4) : '—'} />
        <KV label="Out Degree" value={node.out_degree != null ? String(node.out_degree) : '—'} />
        <KV
          label="Community"
          value={
            node.community_id != null
              ? `#${node.community_id}`
              : '—'
          }
        />
        <KV label="Data Points" value={node.data_points != null ? String(node.data_points) : '—'} />
        <KV
          label="Data Sources"
          value={
            node.data_sources && node.data_sources.length > 0
              ? node.data_sources.join(', ')
              : '—'
          }
        />
        <KV
          label="Risk Score"
          value={
            node.risk_score != null ? (
              <span
                className={
                  node.risk_score >= 0.7
                    ? 'text-rose-300'
                    : node.risk_score >= 0.4
                    ? 'text-amber-300'
                    : 'text-emerald-300'
                }
              >
                {node.risk_score.toFixed(2)}
              </span>
            ) : (
              '—'
            )
          }
        />
        <KV
          label="Source URL"
          value={(() => {
            // P29-F11: sanitize before rendering as anchor.
            if (!node.source_url) return '—';
            const safe = safeExternalUrl(node.source_url);
            if (!safe) {
              return (
                <span className="font-mono text-slate-500" title={`Blocked URL: ${node.source_url}`}>
                  [blocked URL]
                </span>
              );
            }
            return (
              <a
                href={safe}
                target="_blank"
                rel="noreferrer noopener"
                className="text-cyan-300 underline-offset-2 hover:underline"
              >
                {safe.slice(0, 28)}…
              </a>
            );
          })()}
        />
        <KV
          label="Granger p"
          value={node.granger_p != null ? node.granger_p.toFixed(3) : '—'}
        />
        {previewOpen && (
          <div className="mt-3 border-t border-slate-800 pt-3">
            <div className="mb-1 font-mono text-[9px] uppercase tracking-widest text-cyan-300">
              ## Markdown Preview ##
            </div>
            {previewQ.isLoading ? (
              <div className="text-[10px] text-slate-500">Loading…</div>
            ) : previewQ.isError ? (
              <div className="text-[10px] text-rose-300">
                Failed to load: {(previewQ.error as Error).message}
              </div>
            ) : previewQ.data ? (
              <article className="prose prose-invert prose-sm max-w-none text-[11px] prose-headings:text-slate-100 prose-h2:text-rose-300 prose-code:text-emerald-300">
                <ReactMarkdown components={markdownComponents} remarkPlugins={remarkMathPlugins} rehypePlugins={rehypeMathPlugins}>
                  {previewQ.data.content?.slice(0, 4000) || '_(no body)_'}
                </ReactMarkdown>
                {(previewQ.data.content?.length ?? 0) > 4000 && (
                  <div className="text-[10px] text-slate-500">
                    Truncated — open in /kb-explorer for full content.
                  </div>
                )}
              </article>
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}

function KV({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-slate-900/40 py-1">
      <span className="text-[9px] uppercase tracking-wider text-slate-500">{label}</span>
      <span className="text-right">{value}</span>
    </div>
  );
}
