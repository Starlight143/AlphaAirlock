'use client';

import { useEffect, useMemo, useRef } from 'react';

import type { CointegrationResponse } from '@/lib/api';
import { edgeColorForPValue } from '@/lib/cointegration';

type Props = {
  /** Backend `/api/cointegration/pairs` payload (or undefined while loading). */
  data: CointegrationResponse | undefined;
};

/**
 * Full-bleed cointegration constellation. Matches the reference screenshot's
 * star-field layout: high-degree centre nodes, faint edges, dark background.
 *
 * Implementation notes:
 *   - Reuses the project's existing vis-network dep (no new packages).
 *   - Uses vis-data DataSet so future filter UI can mutate visibility in
 *     place (same pattern as FactorGraph.tsx).
 *   - Physics tuned for SPARSER layout vs the factor network: stronger
 *     gravity, longer springs — visually "starry" rather than "clumped".
 *   - Node label only shows on hover via the tooltip path, not as a permanent
 *     overlay — that's how the reference image keeps the canvas clean.
 *   - The component re-binds the vis-network instance whenever `data` changes,
 *     mirroring the FactorGraph teardown so swapping lookback / p-threshold
 *     doesn't leak the previous Network into the DOM.
 */
export default function CointegrationField({ data }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const networkRef = useRef<any>(null);

  const safeData = useMemo(() => {
    const assets = Array.isArray(data?.assets) ? data!.assets : [];
    const pairs = Array.isArray(data?.pairs) ? data!.pairs : [];
    return { assets, pairs };
  }, [data?.assets, data?.pairs]);

  useEffect(() => {
    let cancelled = false;
    let cleanup = () => {};

    async function boot() {
      if (!containerRef.current) return;
      if (safeData.assets.length === 0) return;
      const { Network } = await import('vis-network/standalone');
      const { DataSet } = await import('vis-data/standalone');

      const degree: Record<string, number> = Object.fromEntries(
        safeData.assets.map((a) => [a, 0]),
      );
      for (const p of safeData.pairs) {
        if (p?.src && degree[p.src] !== undefined) degree[p.src] += 1;
        if (p?.dst && degree[p.dst] !== undefined) degree[p.dst] += 1;
      }
      const maxDegree = Math.max(1, ...Object.values(degree));

      const nodes = new DataSet(
        safeData.assets.map((sym) => {
          const d = degree[sym] ?? 0;
          // High-degree centre nodes get a halo + emerald rim; periphery stays
          // dim cyan dots so the eye lands on the cluster centre. P16/B-M13 —
          // dropped the absolute `>= 4` floor so sparse scans (max degree
          // 2 or 3) can still surface their relative hubs; the relative-only
          // ratio of `>= 0.5` keeps single-degree noise from masquerading as
          // centres.
          const isCenter = d > 0 && maxDegree > 0 && d / maxDegree >= 0.5;
          return {
            id: sym,
            label: sym,
            shape: 'dot',
            size: 4 + Math.min(20, d) * 1.5,
            color: {
              background: isCenter ? '#22d3ee' : '#0ea5b7',
              border: isCenter ? '#34d399' : '#0f172a',
              highlight: { background: '#34d399', border: '#fafafa' },
            },
            borderWidth: isCenter ? 3 : 1,
            font: { color: '#64748b', size: 8, face: 'monospace' },
            title: `${sym} · degree ${d}`,
          };
        }),
      );

      const edges = new DataSet(
        safeData.pairs
          .filter((p) => degree[p?.src] !== undefined && degree[p?.dst] !== undefined)
          .map((p, i) => {
          const pValue = Number.isFinite(p?.p_value) ? Number(p.p_value) : 1;
          const beta = Number.isFinite(p?.beta) ? Number(p.beta).toFixed(3) : 'n/a';
          const halfLife = p?.half_life_bars != null && Number.isFinite(p.half_life_bars)
            ? `${Number(p.half_life_bars).toFixed(1)} bars`
            : 'n/a';
          return {
            id: `e_${i}`,
            from: p.src,
            to: p.dst,
            color: { color: edgeColorForPValue(pValue), opacity: 0.35, highlight: '#fafafa' },
            // P16/B-M7 — split the legacy 2-tier width ladder into 4 tiers
            // so the middle p-bucket (0.025 ≤ p < 0.05) is distinguishable
            // from the weak / fallback bucket. Previously p=0.04 rendered
            // at the same width as p=0.99, hiding statistical significance.
            width: pValue < 0.01 ? 1.6 : pValue < 0.025 ? 1.1 : pValue < 0.05 ? 0.8 : 0.5,
            smooth: { enabled: true, type: 'continuous', roundness: 0.25 },
            title: `${p.src} ↔ ${p.dst} · p ${pValue.toFixed(3)} · β ${beta} · half-life ${halfLife}`,
          };
        }),
      );

      const options = {
        autoResize: true,
        physics: {
          stabilization: { iterations: 320 },
          barnesHut: {
            gravitationalConstant: -7000,
            springLength: 35,
            springConstant: 0.025,
            centralGravity: 0.15,
          },
        },
        interaction: { hover: true, tooltipDelay: 80, hoverConnectedEdges: true },
        layout: { improvedLayout: true },
      };

      if (cancelled || !containerRef.current) return;
      const network = new Network(
        containerRef.current,
        { nodes: nodes, edges: edges } as any,
        options,
      );
      networkRef.current = network;
      // P16/B-L10 — guard every hover handler against the race where the
      // effect's cleanup ran (`cancelled = true` / network destroyed) while
      // a vis-network hover event was still in flight. Without this, a
      // late-firing handler could `.update()` a destroyed DataSet and crash
      // the canvas. We also stash the unsubscribe hooks so cleanup detaches
      // listeners deterministically before the network is destroyed.
      const onHoverNode = (params: { node: string }) => {
        if (cancelled) return;
        nodes.update({ id: params.node, font: { color: '#f8fafc', size: 12 } } as any);
      };
      const onBlurNode = (params: { node: string }) => {
        if (cancelled) return;
        nodes.update({ id: params.node, font: { color: '#64748b', size: 8 } } as any);
      };
      // P13/B-L5 — extend the brighten-on-hover affordance to edges: when the
      // operator hovers a connecting line we light up both endpoint labels so
      // the pair is legible without forcing them to chase individual dots.
      // vis-network's `hoverEdge` payload includes the edge id; we look up the
      // raw pair via the DataSet (cheaper than re-querying the network).
      const onHoverEdge = (params: { edge: string }) => {
        if (cancelled) return;
        const edge: any = edges.get(params.edge);
        if (!edge) return;
        nodes.update([
          { id: edge.from, font: { color: '#f8fafc', size: 12 } },
          { id: edge.to, font: { color: '#f8fafc', size: 12 } },
        ] as any);
      };
      const onBlurEdge = (params: { edge: string }) => {
        if (cancelled) return;
        const edge: any = edges.get(params.edge);
        if (!edge) return;
        nodes.update([
          { id: edge.from, font: { color: '#64748b', size: 8 } },
          { id: edge.to, font: { color: '#64748b', size: 8 } },
        ] as any);
      };
      network.on('hoverNode', onHoverNode);
      network.on('blurNode', onBlurNode);
      network.on('hoverEdge', onHoverEdge);
      network.on('blurEdge', onBlurEdge);
      cleanup = () => {
        network.off('hoverNode', onHoverNode);
        network.off('blurNode', onBlurNode);
        network.off('hoverEdge', onHoverEdge);
        network.off('blurEdge', onBlurEdge);
        network.destroy();
      };
    }

    boot();
    return () => {
      cancelled = true;
      cleanup();
      networkRef.current = null;
    };
  }, [safeData]);

  if (safeData.assets.length === 0) {
    return (
      <div className="absolute inset-0 flex items-center justify-center bg-[#020617] px-6 text-center">
        <div className="text-[11px] uppercase tracking-widest text-slate-500">
          No pairs scanned — adjust lookback / p-threshold or hit Refresh
        </div>
      </div>
    );
  }
  return <div ref={containerRef} className="absolute inset-0 bg-[#020617]" />;
}
