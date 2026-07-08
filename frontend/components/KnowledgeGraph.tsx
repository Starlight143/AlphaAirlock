'use client';

import { useEffect, useRef, useState } from 'react';
import { api, type GraphEdge, type GraphNode } from '@/lib/api';
import { safeExternalUrl } from '@/lib/safeUrl';

type Props = {
  onNodeClick: (kind: 'knowledge' | 'strategy', id: number) => void;
  refreshKey: number;
};

// P15/B-M23 — legend swatch colors must match the backend palette returned
// in the graph payload (server-side ColorMap). Keep these in sync with
// backend `/api/graph` color emission.
const KIND_COLOR_HEX = {
  past_alpha: '#EF4444',
  concept: '#22C55E',
  active: '#22D3EE',
  postmortem: '#D946EF',
  in_progress: '#A855F7',
  approved: '#F59E0B',
  paper_trade: '#F97316',
  small_capital: '#FB923C',
  live: '#10B981',
  rejected: '#6B7280',
  graveyard: '#475569',
} as const;

export default function KnowledgeGraph({ onNodeClick, refreshKey }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const networkRef = useRef<unknown | null>(null);
  // P11-F3-14 — populated by boot() once the graph payload returns so the
  // top-right overlay can show nodes/edges totals at a glance.
  const [counts, setCounts] = useState<{ nodes: number; edges: number }>({
    nodes: 0,
    edges: 0,
  });
  // P30 — surface /api/graph failures instead of rendering a misleading
  // empty canvas. `graphError` holds the message; `localRefresh` lets the
  // in-overlay Retry button re-run boot() without touching the parent's
  // refreshKey prop contract.
  const [graphError, setGraphError] = useState<string | null>(null);
  const [localRefresh, setLocalRefresh] = useState(0);
  // P12-B-H3 — selection state for the new NodeInspector overlay. Holds the
  // vis-network node id (e.g. "k42" / "s7") that the operator clicked, or
  // null when nothing is selected / the user clicked empty canvas.
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  // P12-B-H3 — refs are populated inside boot() so the inspector (a sibling
  // React component) can read the latest node payload + computed degree
  // without forcing a re-render every time graph data lands.
  const nodesByIdRef = useRef<Map<string, GraphNode>>(new Map());
  const degreeByIdRef = useRef<Map<string, number>>(new Map());

  useEffect(() => {
    let cancelled = false;
    let cleanup = () => {};

    async function boot() {
      if (!containerRef.current) return;
      // Dynamic import keeps vis-network out of SSR + the initial bundle.
      const { Network } = await import('vis-network/standalone');

      let graph: { nodes: GraphNode[]; edges: GraphEdge[] };
      try {
        graph = await api.graph();
      } catch (err) {
        if (cancelled) return;
        // P30 — surface the failure (matches factor-network / cointegration /
        // kb-explorer error branches) instead of silently swallowing it.
        // eslint-disable-next-line no-console
        console.error('[KnowledgeGraph] /api/graph failed:', err);
        setGraphError(err instanceof Error ? err.message : String(err));
        setCounts({ nodes: 0, edges: 0 });
        setSelectedNodeId(null);
        return;
      }
      if (cancelled || !containerRef.current) return;

      // P30 — clear any previous error on a successful load.
      setGraphError(null);
      // P11-F3-14 — surface totals in the overlay.
      setCounts({ nodes: graph.nodes.length, edges: graph.edges.length });
      // P12-B-H3 — reset selection whenever the graph rebuilds so the
      // inspector doesn't show stale data from a previous payload.
      setSelectedNodeId(null);

      // P11-F3-11 — compute degree per node so we can size knowledge dots by
      // how central they are. Strategy boxes stay fixed-size for layout
      // stability. Undirected count: both endpoints get +1 per edge.
      const degreeById = new Map<string, number>();
      for (const e of graph.edges) {
        degreeById.set(e.from, (degreeById.get(e.from) ?? 0) + 1);
        degreeById.set(e.to, (degreeById.get(e.to) ?? 0) + 1);
      }
      const dotSize = (id: string): number =>
        Math.max(8, Math.min(22, 6 + Math.log1p(degreeById.get(id) ?? 0) * 2.5));

      // P11-F3-12 — node-id → node lookup so the edge mapper can peek at
      // each endpoint's color/kind without an inner scan per edge.
      const nodeById = new Map<string, GraphNode>();
      for (const n of graph.nodes) nodeById.set(n.id, n);

      // P12-B-H3 — publish lookups onto refs so the inspector overlay can
      // pull node metadata + degree without us threading state through React.
      nodesByIdRef.current = nodeById;
      degreeByIdRef.current = degreeById;

      const data = {
        nodes: graph.nodes.map((n) => {
          // P16/B-M9 — knowledge nodes carry `status: "INTAKE"` because the
          // status field is only meaningful for strategies. Showing it on
          // every knowledge node tooltip produced misleading "· INTAKE"
          // labels everywhere. Discriminate: strategies keep stage/status,
          // knowledge nodes surface `node_kind` (concept / past_alpha / …)
          // instead.
          const rawId = String(n.id).replace(/^[ks]/i, '');
          const deg = degreeById.get(n.id) ?? 0;
          const title =
            n.kind === 'strategy'
              ? `S#${rawId} · stage ${n.stage} · ${n.status} · deg ${deg}`
              : `K#${rawId} · ${n.node_kind ?? 'concept'} · deg ${deg}`;
          return {
            id: n.id,
            label: n.label,
            title,
            color: { background: n.color, border: '#0f172a', highlight: { background: n.color, border: '#fafafa' } },
            font: { color: '#e2e8f0', size: 11, face: 'monospace' },
            shape: n.kind === 'strategy' ? 'box' : 'dot',
            size: n.kind === 'strategy' ? 18 : dotSize(n.id),
          };
        }),
        edges: graph.edges.map((e) => {
          // P11-F3-12 — color edges by what they connect:
          //   purple = touches a postmortem node, red = strategy edge,
          //   slate (default) = everything else.
          const fromNode = nodeById.get(e.from);
          const toNode = nodeById.get(e.to);
          const isStrategyEdge =
            fromNode?.kind === 'strategy' || toNode?.kind === 'strategy';
          const touchesPostmortem =
            fromNode?.color === '#D946EF' || toNode?.color === '#D946EF';
          const color = touchesPostmortem
            ? '#a855f7'
            : isStrategyEdge
              ? '#ef4444'
              : '#475569';
          return {
            from: e.from,
            to: e.to,
            arrows: 'to',
            color: { color, highlight: '#a855f7' },
            width: isStrategyEdge ? 1.4 : 1,
          };
        }),
      };

      const options = {
        autoResize: true,
        physics: {
          stabilization: { iterations: 220 },
          barnesHut: {
            // P16/B-M10 — scale gravity with node count so dense knowledge
            // graphs (13k+ nodes) don't collapse into a tight ball. Mirrors
            // the FactorGraph scaling pattern.
            gravitationalConstant:
              -1500 - Math.min(8000, graph.nodes.length * 0.5),
            springLength: 140,
            springConstant: 0.04,
          },
        },
        // P13 A-L5 / P15 B-M25 — tooltipDelay bumped to 400ms so dense graphs
        // (thousands of nodes) don't flash tooltips on every mouse
        // micro-movement. `hover: true` preserved for the visual node
        // highlight; only the popup is throttled.
        interaction: { hover: true, tooltipDelay: 400 },
        layout: { improvedLayout: true },
      };

      const network = new Network(containerRef.current, data, options);
      networkRef.current = network;

      // P12-B-H3 — clicking a node opens the inspector; clicking empty
      // canvas closes it. The parent's onNodeClick contract is preserved
      // but moved to the inspector's "Open file" button so a single click
      // is now non-navigating (no surprise full-page jumps from the canvas).
      network.on('click', (params: { nodes: string[] }) => {
        if (!params.nodes || params.nodes.length === 0) {
          setSelectedNodeId(null);
          return;
        }
        setSelectedNodeId(String(params.nodes[0]));
      });

      cleanup = () => network.destroy();
    }

    boot();

    return () => {
      cancelled = true;
      cleanup();
    };
    // re-run when parent bumps refreshKey
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey, localRefresh]);

  // P12-B-H3 — resolve the currently-selected node + its degree from the
  // refs populated by boot(). When the user clicks a node, this evaluates
  // synchronously on the next render and the inspector pops in.
  const selectedNode = selectedNodeId
    ? nodesByIdRef.current.get(selectedNodeId) ?? null
    : null;
  const selectedDegree = selectedNodeId
    ? degreeByIdRef.current.get(selectedNodeId) ?? 0
    : 0;

  return (
    <div className="relative flex-1 overflow-hidden">
      <div ref={containerRef} className="absolute inset-0" />
      {/* P11-F3-14 — totals badge so the operator always sees graph size. */}
      <div className="absolute right-2 top-2 z-10 rounded-md border border-slate-800 bg-slate-950/80 px-2 py-1 font-mono text-[10px] text-slate-300 backdrop-blur">
        {counts.nodes.toLocaleString()} nodes · {counts.edges.toLocaleString()} edges
      </div>
      {graphError && (
        <div className="absolute left-2 right-2 top-2 z-30 flex items-start justify-between gap-2 rounded-md border border-rose-800/60 bg-rose-950/90 px-3 py-2 font-mono text-[11px] text-rose-300 shadow-xl backdrop-blur">
          <div className="min-w-0">
            <div className="font-bold uppercase tracking-wider">Failed to load graph</div>
            <div className="mt-0.5 line-clamp-2 break-all text-rose-400/90" title={graphError}>
              {graphError}
            </div>
          </div>
          <button
            onClick={() => setLocalRefresh((k) => k + 1)}
            className="shrink-0 rounded border border-rose-700 bg-rose-500/15 px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-rose-200 hover:bg-rose-500/25"
          >
            Retry
          </button>
        </div>
      )}
      {/* P12-B-H3 — Node inspector overlay. Sits just under the totals badge
          so the two stack visually. Closing it (× / empty-canvas click) just
          clears selectedNodeId; "Open file" delegates to the parent's
          onNodeClick handler exactly the way the legacy single-click did. */}
      {selectedNode && (
        <NodeInspector
          node={selectedNode}
          degree={selectedDegree}
          onClose={() => setSelectedNodeId(null)}
          onOpen={() => {
            const raw = selectedNode.id;
            const kind = selectedNode.kind === 'strategy' ? 'strategy' : 'knowledge';
            const id = parseInt(String(raw).replace(/^[a-z]+/i, ''), 10);
            if (!Number.isFinite(id)) return;
            onNodeClick(kind, id);
          }}
        />
      )}
      <Legend />
    </div>
  );
}

// P12-B-H3 — inline component (kept in this file so it has trivial access to
// the GraphNode type + no extra import surface). Renders all the metadata the
// /api/graph endpoint now ships per node.
function NodeInspector({
  node,
  degree,
  onClose,
  onOpen,
}: {
  node: GraphNode;
  degree: number;
  onClose: () => void;
  onOpen: () => void;
}) {
  const rows: { label: string; value: string }[] = [
    { label: 'kind', value: node.kind },
    { label: 'node_kind', value: String(node.node_kind ?? '—') },
    { label: 'stage', value: String(node.stage) },
    { label: 'status', value: node.status || '—' },
    { label: 'category', value: node.category ?? '—' },
    { label: 'out_degree', value: String(node.out_degree ?? degree) },
    {
      label: 'ic_score',
      value:
        typeof node.ic_score === 'number'
          ? node.ic_score.toFixed(3)
          : '—',
    },
  ];

  return (
    <div className="absolute right-2 top-10 z-20 w-[280px] max-w-[88vw] rounded-md border border-slate-700 bg-slate-950/95 p-2.5 font-mono text-[10px] text-slate-200 shadow-xl backdrop-blur">
      <div className="mb-1.5 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate font-bold text-slate-100">{node.label}</div>
          <div className="truncate text-[9px] text-slate-500">{node.id}</div>
        </div>
        <button
          onClick={onClose}
          className="shrink-0 rounded border border-slate-700 px-1.5 py-0.5 text-[10px] text-slate-400 hover:bg-slate-800 hover:text-slate-100"
          aria-label="Close inspector"
        >
          ×
        </button>
      </div>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-2 gap-y-0.5 border-t border-slate-800 pt-1.5">
        {rows.map((r) => (
          <FragmentRow key={r.label} label={r.label} value={r.value} />
        ))}
      </dl>
      {node.source_url && (() => {
        // P29-F11: only render anchor for http(s)/mailto schemes.
        const safe = safeExternalUrl(node.source_url);
        return (
          <div className="mt-1.5 border-t border-slate-800 pt-1.5">
            {safe ? (
              <a
                href={safe}
                target="_blank"
                rel="noreferrer noopener"
                className="line-clamp-2 break-all text-cyan-300 hover:underline"
                title={safe}
              >
                {safe}
              </a>
            ) : (
              <span
                className="line-clamp-2 break-all font-mono text-slate-500"
                title={`Blocked URL scheme: ${node.source_url}`}
              >
                [blocked URL] {node.source_url}
              </span>
            )}
          </div>
        );
      })()}
      <button
        onClick={onOpen}
        className="mt-2 w-full rounded border border-cyan-700 bg-cyan-500/15 px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-cyan-200 hover:bg-cyan-500/25"
      >
        Open file
      </button>
    </div>
  );
}

function FragmentRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-[9px] uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className="truncate text-slate-200" title={value}>{value}</dd>
    </>
  );
}

function Legend() {
  // P12-B-M1 — split legend into two visual groups: KNOWLEDGE NODES (dots,
  // rounded-full) vs STRATEGY STATUSES (boxes, rounded-sm). Makes it obvious
  // at a glance which palette applies to which shape. Includes all 7 strategy
  // status colours (in-progress purple, approved amber, paper-trade orange,
  // small-capital lighter-orange, live emerald, rejected slate-500,
  // graveyard slate-600) so the legend is complete.
  const KIND_ITEMS = [
    { label: 'Past Alpha', color: KIND_COLOR_HEX.past_alpha },
    { label: 'Concept', color: KIND_COLOR_HEX.concept },
    { label: 'Active', color: KIND_COLOR_HEX.active },
    { label: 'Postmortem', color: KIND_COLOR_HEX.postmortem },
  ];
  const STRATEGY_ITEMS = [
    { label: 'In-progress', color: KIND_COLOR_HEX.in_progress },
    { label: 'Approved', color: KIND_COLOR_HEX.approved },
    { label: 'Paper Trade', color: KIND_COLOR_HEX.paper_trade },
    { label: 'Small Capital', color: KIND_COLOR_HEX.small_capital },
    { label: 'Live', color: KIND_COLOR_HEX.live },
    { label: 'Rejected', color: KIND_COLOR_HEX.rejected },
    { label: 'Graveyard', color: KIND_COLOR_HEX.graveyard },
  ];
  return (
    <div className="absolute bottom-2 left-2 z-10 flex flex-col gap-1.5 rounded-md border border-slate-800 bg-slate-950/80 px-2 py-1.5 backdrop-blur">
      <div>
        <div className="mb-0.5 text-[9px] font-bold uppercase tracking-widest text-slate-500">
          Knowledge
        </div>
        <div className="flex flex-col gap-0.5">
          {KIND_ITEMS.map((it) => (
            <div key={it.label} className="flex items-center gap-2 text-[10px] text-slate-300">
              <span className="h-2.5 w-2.5 rounded-full" style={{ background: it.color }} />
              {it.label}
            </div>
          ))}
        </div>
      </div>
      <div className="border-t border-slate-800 pt-1">
        <div className="mb-0.5 text-[9px] font-bold uppercase tracking-widest text-slate-500">
          Strategy
        </div>
        <div className="flex flex-col gap-0.5">
          {STRATEGY_ITEMS.map((it) => (
            <div key={it.label} className="flex items-center gap-2 text-[10px] text-slate-300">
              <span className="h-2.5 w-2.5 rounded-sm" style={{ background: it.color }} />
              {it.label}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
