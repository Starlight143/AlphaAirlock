'use client';

import { useEffect, useMemo, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type FactorGraphEdge, type FactorGraphNode } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { pValueEdgeColor } from '@/lib/pValueColor';
import type { FactorNetworkFilters } from './FactorNetworkToolbar';

type Props = {
  onNodeSelect: (node: FactorGraphNode | null) => void;
  filters: FactorNetworkFilters;
};

// P12/C-H2 — backend payload sometimes carries a `granger_p` field on the
// FactorGraphEdge (Granger-causality p-value, lower = stronger). The shared
// `FactorGraphEdge` type does not yet declare it, so we narrow locally here
// (the field is optional — undefined means "not a granger edge").
type EdgeWithGranger = FactorGraphEdge & { granger_p?: number | null };

// P13/B-L2 — Colour an edge by Granger p-value via the shared helper. Rose
// for the strictest tier (p<0.01), amber for moderate (p<0.025), cyan for
// marginal (p<0.05). Anything above 0.05 falls back to the slate dim
// (#475569) since it isn't statistically meaningful — that fallback is
// passed explicitly so the cointegration scan can keep its own cyan default.
function grangerEdgeColor(p: number): string {
  return pValueEdgeColor(p, '#475569');
}

// P15/B-M7 — extract the edge-update derivation so the initial build and the
// filter-update path agree on colour/arrows/opacity in one place. P16/B-H1
// moves the `opacity` field INTO the color object — vis-network 9.x reads
// opacity exclusively from `color.opacity` inside `getFormattingValues`, so a
// top-level `opacity` field was being silently dropped. P16/B-H2 changes the
// arrows clear-path from `undefined` (no-op in vis-network 9.x) to `''` so
// toggling Granger DAG off actively wipes arrowheads. P16/B-L6 drops the
// index suffix from edge ids so the id stays stable across rebuilds.
function buildEdgeUpdate(
  e: EdgeWithGranger,
  _i: number,
  endpointsVisible: boolean,
  grangerOn: boolean,
): { id: string; color: any; arrows: string; hidden: boolean } {
  const hasGranger = e.granger_p != null && Number.isFinite(Number(e.granger_p));
  let color: any = { color: '#334155', highlight: '#22D3EE', opacity: 1.0 };
  let arrows: string = '';
  if (grangerOn) {
    if (hasGranger) {
      const p = Number(e.granger_p);
      color = { color: grangerEdgeColor(p), highlight: grangerEdgeColor(p), opacity: 1.0 };
      arrows = 'to';
    } else {
      color = { color: '#1e293b', opacity: 0.1 };
    }
  }
  return {
    id: `e_${e.from}__${e.to}`,
    color,
    arrows,
    hidden: !endpointsVisible,
  };
}

/**
 * Force-directed factor network with live filtering. Uses vis-data DataSets so
 * filter changes mutate node visibility in place (no full re-init) — keeps
 * the physics simulation stable across user interactions.
 */
export default function FactorGraph({ onNodeSelect, filters }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const nodesDsRef = useRef<any>(null);
  const edgesDsRef = useRef<any>(null);
  const networkRef = useRef<any>(null);
  const nodesByIdRef = useRef<Map<string, FactorGraphNode>>(new Map());
  // P12/C-H2 — keep the raw edge list around so filter changes can rebuild
  // edge update payloads (granger highlight / hidden) without going back
  // through the React Query cache.
  const edgesRawRef = useRef<EdgeWithGranger[]>([]);

  // P12/C-M4 — stash the click callback in a ref so the mount useEffect can
  // drop `onNodeSelect` from its deps. Otherwise every parent re-render
  // (filter changes, selection updates) re-runs boot(), tears down the
  // network, and re-runs the physics stabilisation — making the graph feel
  // jumpy and incidentally racing the in-flight `cleanup`.
  const onSelectRef = useRef(onNodeSelect);
  useEffect(() => {
    onSelectRef.current = onNodeSelect;
  }, [onNodeSelect]);

  const q = useQuery({
    queryKey: queryKeys.factorNetwork,
    queryFn: api.factorNetwork,
    staleTime: 60_000,
  });

  // Canvas legend — derived from the LIVE node set so it can never mislabel.
  // main.py/api_factor_network paints knowledge nodes by KIND (KIND_COLORS) and
  // strategy nodes by STATUS (_strategy_color); category is NOT the colour key
  // (the toolbar chips are a filter aid, not a node-colour legend). We surface
  // one row per knowledge kind actually present (stable colour each) and
  // collapse strategy nodes into a single row — their colour varies by status,
  // so hover / the inspector shows the exact status.
  const legendItems = useMemo(() => {
    const nodes = q.data?.nodes ?? [];
    const knowledge = new Map<string, string>(); // prettyKind -> colour
    const strategy = new Map<string, string>(); // colour -> status label
    for (const n of nodes) {
      if (n.kind === 'strategy') {
        if (!strategy.has(n.color)) {
          strategy.set(n.color, STRATEGY_STATUS_LABEL[n.color.toUpperCase()] ?? 'Strategy');
        }
      } else if (n.kind) {
        const label = prettyKind(n.kind);
        if (!knowledge.has(label)) knowledge.set(label, n.color);
      }
    }
    const items: { label: string; color: string; box: boolean }[] = [];
    for (const [label, color] of knowledge) items.push({ label, color, box: false });
    for (const [color, label] of strategy) items.push({ label, color, box: true });
    return items;
  }, [q.data]);

  // Granger edge tiers — only shown in DAG mode. Colours are sampled from the
  // SAME pValueEdgeColor() the edges use (with representative p-values), so the
  // swatches always match what's drawn, with zero risk of a hardcoded drift.
  const grangerLegend = useMemo(
    () => [
      { label: 'p < 0.01', color: pValueEdgeColor(0.005, '#475569') },
      { label: 'p < 0.025', color: pValueEdgeColor(0.02, '#475569') },
      { label: 'p < 0.05', color: pValueEdgeColor(0.04, '#475569') },
    ],
    [],
  );

  // --- Mount / refresh data ---
  useEffect(() => {
    let cancelled = false;
    let cleanup = () => {};

    async function boot() {
      if (!containerRef.current) return;
      if (!q.data) return;
      const { Network } = await import('vis-network/standalone');
      const { DataSet } = await import('vis-data/standalone');

      const nodesById = new Map<string, FactorGraphNode>();
      for (const n of q.data.nodes) nodesById.set(n.id, n);
      nodesByIdRef.current = nodesById;

      // P12/C-H2+C-H4 — pre-compute which nodes pass the active filter so the
      // initial edge build can mark edges hidden when either endpoint is
      // filtered out (otherwise dangling edges flash on first paint).
      const initialPass = new Map<string, boolean>();
      for (const n of q.data.nodes) initialPass.set(n.id, nodePassesFilter(n, filters));

      const nodesArr = q.data.nodes.map((n) => ({
        id: n.id,
        label: n.label,
        title: `${String(n.kind ?? '').toUpperCase()} · ${n.category ?? '—'} · IC ${Number.isFinite(n.ic_score) ? n.ic_score.toFixed(2) : '—'}`,
        color: {
          background: n.color,
          border: '#0f172a',
          highlight: { background: n.color, border: '#fafafa' },
        },
        font: { color: '#e2e8f0', size: 11, face: 'monospace' },
        shape: defaultShape(n.kind, filters.shape),
        size: n.size,
        hidden: !(initialPass.get(n.id) ?? true),
      }));
      // P12/C-H2 — keep edge ids stable across rebuilds (from/to is unique
      // enough for the public payload; falls back to the index when not).
      const edgesRaw: EdgeWithGranger[] = q.data.edges as EdgeWithGranger[];
      edgesRawRef.current = edgesRaw;
      const grangerOnInitial = filters.grangerDag === true;
      const edgesArr = edgesRaw.map((e, i) => {
        const hasGranger = e.granger_p != null && Number.isFinite(Number(e.granger_p));
        const endpointsVisible =
          (initialPass.get(e.from) ?? false) && (initialPass.get(e.to) ?? false);
        const upd = buildEdgeUpdate(e, i, endpointsVisible, grangerOnInitial);
        return {
          id: upd.id,
          from: e.from,
          to: e.to,
          // Preserve granger_p on the edge so the filter effect can re-derive
          // colour without going back to the raw payload.
          granger_p: hasGranger ? Number(e.granger_p) : null,
          color: upd.color,
          arrows: upd.arrows,
          width: 0.6,
          // P16/B-H1 — opacity now lives inside `color`; the standalone field
          // was being silently dropped by vis-network 9.x.
          smooth: { type: 'continuous', enabled: true, roundness: 0.3 },
          hidden: upd.hidden,
        };
      });

      const nodesDs = new DataSet(nodesArr);
      const edgesDs = new DataSet(edgesArr);
      nodesDsRef.current = nodesDs;
      edgesDsRef.current = edgesDs;

      const options = {
        autoResize: true,
        physics: {
          stabilization: { iterations: 280 },
          barnesHut: {
            // Scale gravity with node count so dense graphs don't collapse into
            // the centre. -2200 was tuned for ~50 nodes; ~3000-node payloads
            // need ~3x stronger repulsion to keep clusters legible.
            gravitationalConstant: -2200 - Math.min(7000, q.data.nodes.length * 4),
            springLength: 110,
            springConstant: 0.045,
          },
        },
        interaction: { hover: true, tooltipDelay: 80 },
        layout: { improvedLayout: true },
      };

      if (cancelled || !containerRef.current) return;
      const network = new Network(containerRef.current, { nodes: nodesDs, edges: edgesDs } as any, options);
      networkRef.current = network;
      network.on('click', (params: { nodes: string[] }) => {
        // P12/C-M4 — read through the ref so the latest callback fires without
        // forcing a network rebuild on every parent re-render.
        if (!params.nodes || params.nodes.length === 0) {
          onSelectRef.current(null);
          return;
        }
        const id = params.nodes[0];
        const node = nodesByIdRef.current.get(id) ?? null;
        onSelectRef.current(node);
      });
      cleanup = () => network.destroy();
    }

    boot();
    return () => {
      cancelled = true;
      cleanup();
      networkRef.current = null;
      nodesDsRef.current = null;
      edgesDsRef.current = null;
      edgesRawRef.current = [];
    };
    // P12/C-M4 — `onNodeSelect` lives on a ref now, so the boot effect only
    // needs to react to actual data changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q.data]);

  // --- Re-apply filters without rebuilding the network ---
  // P12/C-H2+C-H4 — single pass: compute node visibility once into a map,
  // push node updates AND edge updates (hidden + granger highlight) so the
  // graph stays in sync without two separate effects fighting over the
  // same DataSets.
  useEffect(() => {
    const nodesDs = nodesDsRef.current;
    const edgesDs = edgesDsRef.current;
    const map = nodesByIdRef.current;
    if (!nodesDs || !map.size) return;

    // 1) Cache "did this node pass the filter?" — used by both the node
    //    update payload and the edge endpoint visibility check below.
    const passById = new Map<string, boolean>();
    map.forEach((n, id) => passById.set(id, nodePassesFilter(n, filters)));

    const nodeUpdates: { id: string; hidden: boolean; shape: string }[] = [];
    map.forEach((n, id) => {
      nodeUpdates.push({
        id,
        hidden: !(passById.get(id) ?? true),
        shape: defaultShape(n.kind, filters.shape),
      });
    });
    nodesDs.update(nodeUpdates);

    // 2) Edge updates: colour by Granger p-value when DAG mode is on, and
    //    hide any edge whose endpoint was filtered out of the node set.
    if (edgesDs) {
      const grangerOn = filters.grangerDag === true;
      const edgeUpdates: any[] = [];
      const edgesRaw = edgesRawRef.current;
      edgesRaw.forEach((e, i) => {
        const endpointsVisible =
          (passById.get(e.from) ?? false) && (passById.get(e.to) ?? false);
        edgeUpdates.push(buildEdgeUpdate(e, i, endpointsVisible, grangerOn));
      });
      if (edgeUpdates.length > 0) edgesDs.update(edgeUpdates);
    }
  }, [filters]);

  // P11-F4-05 — listen for the toolbar's "Reset View" button. The toolbar
  // already resets the filter state via `onReset`; we additionally call
  // network.fit() so the camera snaps back to a sensible framing. The event is
  // dispatched on `window` so we don't need to thread a ref through the
  // toolbar; the listener cleans itself up on unmount.
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const handler = () => {
      networkRef.current?.fit?.({
        animation: { duration: 350, easingFunction: 'easeOutQuad' },
      });
    };
    window.addEventListener('factor-network:reset-view', handler);
    return () => {
      window.removeEventListener('factor-network:reset-view', handler);
    };
  }, []);

  return (
    <div className="relative h-full w-full overflow-hidden">
      <div ref={containerRef} className="absolute inset-0" />
      {q.isLoading && (
        <div className="absolute inset-0 flex items-center justify-center text-xs text-slate-500">
          Loading factor network…
        </div>
      )}
      {q.isError && (
        <div className="absolute inset-0 flex items-center justify-center text-xs text-rose-400">
          Failed to load factor network. Please refresh.
        </div>
      )}
      <div className="absolute bottom-2 left-2 z-10 flex flex-col gap-1 rounded-md border border-slate-800 bg-slate-950/80 px-2 py-1.5 backdrop-blur">
        <div className="text-[9px] uppercase tracking-wider text-slate-500">
          {q.data?.n_factors ?? '—'} factors · {q.data?.n_edges ?? '—'} edges
          {filters.enabledCategories.size > 0 &&
            ` · ${filters.enabledCategories.size} cat filter`}
        </div>
      </div>
      {legendItems.length > 0 && (
        <div className="absolute bottom-2 right-2 z-10 flex max-w-[160px] flex-col gap-0.5 rounded-md border border-slate-800 bg-slate-950/80 px-2 py-1.5 backdrop-blur">
          <div className="mb-0.5 text-[9px] font-bold uppercase tracking-widest text-slate-500">
            Node color
          </div>
          {legendItems.map((it) => (
            <div key={it.label} className="flex items-center gap-2 text-[10px] text-slate-300">
              <span
                className={it.box ? 'h-2.5 w-2.5 rounded-sm' : 'h-2.5 w-2.5 rounded-full'}
                style={{ background: it.color }}
              />
              {it.label}
            </div>
          ))}
          {filters.grangerDag && (
            <div className="mt-1 border-t border-slate-800 pt-1">
              <div className="mb-0.5 text-[9px] font-bold uppercase tracking-widest text-slate-500">
                Granger edge
              </div>
              {grangerLegend.map((it) => (
                <div key={it.label} className="flex items-center gap-2 text-[10px] text-slate-300">
                  <span
                    className="inline-block h-[2px] w-3 rounded-full"
                    style={{ background: it.color }}
                  />
                  {it.label}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// Mirrors backend STRATEGY_STATUS_COLORS / _strategy_color (main.py) so each
// strategy node's colour maps back to a precise status label in the legend
// (kept in sync with the same palette the kb-explorer legend uses).
const STRATEGY_STATUS_LABEL: Record<string, string> = {
  '#F59E0B': 'Approved',
  '#F97316': 'Paper Trade',
  '#FB923C': 'Small Capital',
  '#10B981': 'Live',
  '#6B7280': 'Rejected',
  '#475569': 'Graveyard',
  '#A855F7': 'In-progress',
};

function prettyKind(kind: string): string {
  return kind.replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

function defaultShape(kind: string, requested: 'dot' | 'box' | 'diamond'): string {
  // Strategy nodes are always boxes (matches the reference); concept/factor
  // nodes honour the toggle (dot / box / diamond).
  if (kind === 'strategy') return 'box';
  if (requested === 'diamond') return 'diamond';
  return requested === 'box' ? 'box' : 'dot';
}

function nodePassesFilter(n: FactorGraphNode, f: FactorNetworkFilters): boolean {
  if (f.scope === 'factors' && n.kind === 'strategy') return false;
  if (f.scope === 'strategies' && n.kind !== 'strategy') return false;
  // P6-A12: Factor×Factor view = hide all strategy nodes outright.
  if (f.factorPairMode && n.kind === 'strategy') return false;
  if (f.enabledCategories.size > 0) {
    const cat = n.category || 'unclassified';
    if (!f.enabledCategories.has(cat)) return false;
  }
  // P13/B-L3 — the fixed preset chip row was removed (keys never matched
  // backend canonical category strings, so the filter was a no-op trap). The
  // dynamic category chips above this comment already cover every backend
  // category, so the preset shortcut added confusion without functionality.
  // P6-A12 / P8-FIX/H-3: bi-directional IC floor — drop nodes below the
  // slider's value. The slider's -1 sentinel disables the filter (since IC
  // can be as low as -1; the legacy 0-floor hid every negative-IC factor).
  if (f.icMin > -1) {
    const ic = Number(n.ic_score);
    if (!Number.isFinite(ic) || ic < f.icMin) return false;
  }
  // P12/C-H1 — paired IC ceiling. The slider's `3` sentinel is the upper end
  // of the input range and effectively disables the cap for any realistic
  // factor IC (real IC is bounded by ±1, but legacy nodes occasionally carry
  // a Sharpe-style score). Only enforce the cap when the operator actually
  // pulled the handle down below the sentinel.
  if (f.icMax < 3) {
    const ic = Number(n.ic_score);
    if (!Number.isFinite(ic) || ic > f.icMax) return false;
  }
  // P6-A12: asset whitelist intersection — match against title (only place
  // the public payload exposes asset hints today).
  if (f.enabledAssets.size > 0) {
    const haystack = (n.title || '').toUpperCase();
    let matched = false;
    f.enabledAssets.forEach((sym) => {
      if (haystack.includes(sym)) matched = true;
    });
    if (!matched) return false;
  }
  return true;
}
