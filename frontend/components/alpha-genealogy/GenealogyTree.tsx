'use client';

import { useEffect, useMemo, useRef } from 'react';
import { ZoomIn, ZoomOut, Maximize2 } from 'lucide-react';

import type { AgTreeNode } from '@/lib/api';

type Props = {
  trees: AgTreeNode[];
  onSelect?: (node: AgTreeNode) => void;
};

const STATUS_COLOR: Record<string, string> = {
  APPROVED: '#F59E0B',
  PAPER_TRADE: '#F97316',
  SMALL_CAPITAL: '#FB923C',
  LIVE: '#10B981',
  REJECTED: '#6B7280',
  GRAVEYARD: '#475569',
  PAUSED: '#64748b',
  INTAKE: '#A855F7',
  STORY_GEN: '#A855F7',
  CODE_GEN: '#A855F7',
  BACKTESTING: '#A855F7',
  CRITIC_LOOP: '#A855F7',
};

export default function GenealogyTree({ trees, onSelect }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  // P18 A5 — keep a handle to the live vis-network instance so the
  // floating zoom/fit controls can drive it. Mouse-wheel zoom and
  // click-drag pan are already enabled by vis-network defaults.
  const networkRef = useRef<any>(null);
  const flat = useMemo(() => flatten(trees), [trees]);
  // Perf — index nodes by id once (O(n)) so edge construction can resolve
  // parents in O(1) instead of an O(n) flat.find per non-root node (O(n²)).
  const byId = useMemo(() => {
    const m = new Map<number, AgTreeNode>();
    for (const n of flat) m.set(n.id, n);
    return m;
  }, [flat]);

  const onSelectRef = useRef(onSelect);
  useEffect(() => {
    onSelectRef.current = onSelect;
  }, [onSelect]);

  useEffect(() => {
    if (!ref.current) return;
    let cancelled = false;
    let network: any = null;
    (async () => {
      const { Network } = await import('vis-network/standalone');
      const { DataSet } = await import('vis-data/standalone');
      if (cancelled || !ref.current) return;
      const nodes = new DataSet<any>(
        flat.map((n) => ({
          id: n.id,
          label: `S#${n.id}\n${(n.name ?? '').slice(0, 24)}\nIC ${n.ic_score != null && Number.isFinite(n.ic_score) ? n.ic_score.toFixed(2) : '—'} · Sharpe ${n.sharpe != null && Number.isFinite(n.sharpe) ? n.sharpe.toFixed(2) : '—'}`,
          shape: 'box',
          color: { background: STATUS_COLOR[n.status] ?? '#475569', border: n.badges.includes('cycle') ? '#ef4444' : '#1e293b' },
          font: { face: 'monospace', size: 10, color: '#0f172a' },
          borderWidth: n.badges.includes('cycle') ? 3 : 1,
          opacity: n.badges.includes('barren') ? 0.4 : 1,
        })),
      );
      const edges = new DataSet<any>(
        flat
          .filter((n) => n.parent_id != null)
          .map((n) => {
            const parent = byId.get(n.parent_id as number);
            // P16/B-M1 — only compute a colour delta when BOTH parent and
            // child have a finite Sharpe. Previously a child still pending
            // backtest (`sharpe == null`) collapsed to `-Infinity`, which
            // automatically painted the edge rose ("worse than parent")
            // even though no comparison was possible yet.
            const childSh =
              n.sharpe != null && Number.isFinite(n.sharpe) ? n.sharpe : null;
            const parentSh =
              parent?.sharpe != null && Number.isFinite(parent.sharpe)
                ? parent.sharpe
                : null;
            let color = '#475569';
            if (childSh != null && parentSh != null) {
              // Relative threshold: at |parentSh|=1 (typical) this is ±0.1,
              // but a parent with Sharpe 3 needs a bigger delta to count as
              // "meaningful improvement".
              const delta = Math.max(0.1, Math.abs(parentSh) * 0.1);
              if (childSh > parentSh + delta) color = '#22c55e';
              else if (childSh < parentSh - delta) color = '#ef4444';
            }
            return {
              from: n.parent_id,
              to: n.id,
              arrows: { to: { enabled: true, scaleFactor: 0.4 } },
              color: { color, opacity: 0.7 },
              dashes: parent?.status === 'REJECTED' || parent?.status === 'GRAVEYARD',
            };
          }),
      );
      network = new Network(
        ref.current,
        { nodes, edges },
        {
          autoResize: true,
          layout: {
            hierarchical: {
              enabled: true, direction: 'UD', sortMethod: 'directed',
              levelSeparation: 130, nodeSpacing: 180, treeSpacing: 240,
            },
          },
          physics: { enabled: false },
          interaction: { hover: true, dragNodes: false },
          edges: { smooth: { enabled: true, type: 'cubicBezier', forceDirection: 'vertical', roundness: 0.4 } },
        },
      );
      network.on('selectNode', (params: any) => {
        const id = params.nodes[0];
        const node = flat.find((n) => n.id === id);
        if (node && onSelectRef.current) onSelectRef.current(node);
      });
      networkRef.current = network;
    })();
    return () => {
      cancelled = true;
      networkRef.current = null;
      network?.destroy();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flat]);

  // P18 A5 — UI controls for zoom/fit. vis-network exposes `getScale()`,
  // `moveTo()`, and `fit()`; all three guard for null in case the network
  // is mid-init or unmounting.
  const zoom = (factor: number) => {
    const n = networkRef.current;
    if (!n) return;
    const scale = n.getScale?.() ?? 1;
    n.moveTo?.({
      scale: scale * factor,
      animation: { duration: 200, easingFunction: 'easeOutQuad' },
    });
  };
  const fitView = () => {
    networkRef.current?.fit?.({
      animation: { duration: 350, easingFunction: 'easeOutQuad' },
    });
  };

  if (!trees.length) {
    return (
      <div className="flex h-full min-h-[400px] items-center justify-center text-center text-[11px] text-slate-500">
        Genealogy populates as strategies fail and spawn derivatives. No postmortem-derived strategies yet.
      </div>
    );
  }
  return (
    <div className="relative h-full w-full">
      <div ref={ref} className="absolute inset-0" />
      <Legend />
      <div className="absolute bottom-2 right-2 z-10 flex flex-col gap-1 rounded-md border border-slate-800 bg-slate-950/80 p-1 backdrop-blur">
        <button
          type="button"
          onClick={() => zoom(1.25)}
          className="rounded p-1 text-slate-300 transition hover:bg-slate-800 hover:text-cyan-300"
          aria-label="Zoom in"
          title="Zoom in"
        >
          <ZoomIn className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          onClick={() => zoom(0.8)}
          className="rounded p-1 text-slate-300 transition hover:bg-slate-800 hover:text-cyan-300"
          aria-label="Zoom out"
          title="Zoom out"
        >
          <ZoomOut className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          onClick={fitView}
          className="rounded p-1 text-slate-300 transition hover:bg-slate-800 hover:text-cyan-300"
          aria-label="Fit graph to view"
          title="Fit graph to view"
        >
          <Maximize2 className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}

/**
 * Canvas legend for the genealogy graph. Mirrors the kb-explorer graph legend
 * (KnowledgeGraph.tsx) so the two network views share one visual language.
 * Status swatches reuse the exact STATUS_COLOR palette the nodes are painted
 * with; the five in-progress sub-statuses (INTAKE/STORY_GEN/CODE_GEN/
 * BACKTESTING/CRITIC_LOOP) all share one purple, so they collapse into a single
 * "In-progress" row. Edge swatches mirror the parent→child Sharpe-delta colours
 * derived in the edge builder above (green = child improved vs parent, red =
 * child worse, slate = no finite comparison / incomplete data). Pinned
 * bottom-left so it never collides with the zoom controls (bottom-right).
 */
function Legend() {
  const STATUS_ITEMS: { label: string; color: string }[] = [
    { label: 'In-progress', color: STATUS_COLOR.INTAKE },
    { label: 'Approved', color: STATUS_COLOR.APPROVED },
    { label: 'Paper Trade', color: STATUS_COLOR.PAPER_TRADE },
    { label: 'Small Capital', color: STATUS_COLOR.SMALL_CAPITAL },
    { label: 'Live', color: STATUS_COLOR.LIVE },
    { label: 'Rejected', color: STATUS_COLOR.REJECTED },
    { label: 'Graveyard', color: STATUS_COLOR.GRAVEYARD },
  ];
  const EDGE_ITEMS: { label: string; color: string }[] = [
    { label: 'Child improved', color: '#22c55e' },
    { label: 'Child worse', color: '#ef4444' },
    { label: 'No comparison', color: '#475569' },
  ];
  return (
    <div className="absolute bottom-2 left-2 z-10 flex max-w-[160px] flex-col gap-1.5 rounded-md border border-slate-800 bg-slate-950/80 px-2 py-1.5 backdrop-blur">
      <div>
        <div className="mb-0.5 text-[9px] font-bold uppercase tracking-widest text-slate-500">
          Status
        </div>
        <div className="flex flex-col gap-0.5">
          {STATUS_ITEMS.map((it) => (
            <div key={it.label} className="flex items-center gap-2 text-[10px] text-slate-300">
              <span className="h-2.5 w-2.5 rounded-sm" style={{ background: it.color }} />
              {it.label}
            </div>
          ))}
        </div>
      </div>
      <div className="border-t border-slate-800 pt-1">
        <div className="mb-0.5 text-[9px] font-bold uppercase tracking-widest text-slate-500">
          Edge
        </div>
        <div className="flex flex-col gap-0.5">
          {EDGE_ITEMS.map((it) => (
            <div key={it.label} className="flex items-center gap-2 text-[10px] text-slate-300">
              <span className="inline-block h-[2px] w-3 rounded-full" style={{ background: it.color }} />
              {it.label}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function flatten(trees: AgTreeNode[]): AgTreeNode[] {
  const out: AgTreeNode[] = [];
  const walk = (n: AgTreeNode) => {
    out.push(n);
    n.children.forEach(walk);
  };
  trees.forEach(walk);
  return out;
}
