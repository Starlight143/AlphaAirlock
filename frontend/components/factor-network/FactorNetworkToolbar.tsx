'use client';

import { useMemo } from 'react';
import clsx from 'clsx';
import { RotateCcw, Layers, BoxSelect, Diamond, GitBranch, Hash } from 'lucide-react';
import type { FactorGraphResponse } from '@/lib/api';

export type FactorNetworkFilters = {
  enabledCategories: Set<string>;
  shape: 'dot' | 'box' | 'diamond';
  grangerDag: boolean;
  scope: 'all' | 'factors' | 'strategies';
  // P6-A12 additions
  factorPairMode: boolean;          // Factor×Factor view — hides strategy edges
  enabledAssets: Set<string>;       // intersection filter on derived assets
  // P8-FIX/H-3 — IC floor is now bi-directional (range -1 .. +3) because real
  // factor IC can be negative (mean-reversion / inverted signals). The legacy
  // 0..3 "Sharpe" slider hid roughly half the population.
  // P11-F4-03 — Clarification: this slider acts as a bi-directional IC floor
  // ONLY (drops nodes whose ic_score < icMin). It is NOT a Granger DAG toggle —
  // Granger DAG mode is the separate "Granger DAG" button on row 1, which
  // toggles `grangerDag` independently. The -1 sentinel value disables the
  // filter entirely (since real IC bottoms out at -1 by definition).
  icMin: number;
  // P12/C-H1 — IC range ceiling. Paired with `icMin` to form a two-handle band
  // filter. `3` is the upper sentinel (matches the slider's `max` attribute and
  // the previous single-handle range), effectively meaning "no upper bound" for
  // any realistic factor IC.
  icMax: number;
};

export const DEFAULT_FACTOR_FILTERS: FactorNetworkFilters = {
  enabledCategories: new Set<string>(),
  shape: 'dot',
  grangerDag: false,
  scope: 'all',
  factorPairMode: false,
  enabledAssets: new Set<string>(),
  icMin: -1,
  icMax: 3,
};


// Whitelist of common crypto symbols we'll surface as asset filter pills.
// The set is conservative on purpose — random uppercase tokens in titles
// shouldn't pollute the asset bar.
export const ASSET_WHITELIST = [
  'BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'ADA', 'DOGE', 'AVAX', 'MATIC', 'DOT',
  'LINK', 'UNI', 'ATOM', 'LTC', 'NEAR', 'APT', 'ARB', 'OP', 'SUI', 'TIA',
];

type Props = {
  data: FactorGraphResponse | undefined;
  filters: FactorNetworkFilters;
  onChange: (next: FactorNetworkFilters) => void;
  onReset: () => void;
};

export default function FactorNetworkToolbar({
  data,
  filters,
  onChange,
  onReset,
}: Props) {
  const categories = useMemo(() => {
    if (!data) return [] as string[];
    const set = new Set<string>();
    for (const n of data.nodes) {
      if (n.category) set.add(n.category);
    }
    return Array.from(set).sort();
  }, [data]);

  // Detect which whitelisted assets appear in node titles. Only show pills
  // for assets that actually intersect the dataset.
  const visibleAssets = useMemo(() => {
    if (!data) return [] as string[];
    const present = new Set<string>();
    for (const n of data.nodes) {
      const haystack = (n.title || '').toUpperCase();
      for (const sym of ASSET_WHITELIST) {
        if (haystack.includes(sym)) present.add(sym);
      }
    }
    return ASSET_WHITELIST.filter((s) => present.has(s));
  }, [data]);

  const palette = data?.kind_palette ?? {};
  // P16/B-L7 — build a per-render colour assignment that walks the visible
  // categories in sorted order and resolves hash collisions by stepping to
  // the next free palette slot. The legacy `hash % 16` picker silently
  // collided whenever two categories landed in the same modulo bucket,
  // making the chip row visually ambiguous.
  const categoryColorMap = useMemo(() => {
    const fallback = [
      '#22D3EE', '#10B981', '#A855F7', '#F59E0B', '#F43F5E', '#D946EF', '#3B82F6',
      '#84CC16', '#06B6D4', '#EF4444', '#EC4899', '#14B8A6', '#F97316', '#8B5CF6',
      '#FB7185', '#0EA5E9',
    ];
    const seen = new Set<string>();
    const out: Record<string, string> = {};
    for (const cat of categories) {
      const pinned = palette[cat];
      if (pinned) {
        out[cat] = pinned;
        seen.add(pinned);
        continue;
      }
      let h = 0;
      for (let i = 0; i < cat.length; i++) h = (h * 31 + cat.charCodeAt(i)) >>> 0;
      let idx = h % fallback.length;
      // Walk forward until we find an unseen slot (worst case wraps once).
      for (let step = 0; step < fallback.length; step++) {
        const candidate = fallback[(idx + step) % fallback.length];
        if (!seen.has(candidate)) {
          out[cat] = candidate;
          seen.add(candidate);
          break;
        }
      }
      // Fallback if every slot is taken (more categories than palette
      // entries) — accept the hashed colour as a duplicate rather than
      // returning undefined.
      if (!out[cat]) out[cat] = fallback[idx];
    }
    return out;
  }, [categories, palette]);
  const categoryColor = (cat: string): string =>
    categoryColorMap[cat] ?? '#64748b';

  const setCategory = (cat: string, on: boolean) => {
    const next = new Set(filters.enabledCategories);
    if (on) next.add(cat);
    else next.delete(cat);
    onChange({ ...filters, enabledCategories: next });
  };
  const setAsset = (sym: string, on: boolean) => {
    const next = new Set(filters.enabledAssets);
    if (on) next.add(sym);
    else next.delete(sym);
    onChange({ ...filters, enabledAssets: next });
  };

  const isAllCategories = filters.enabledCategories.size === 0;
  const isAllAssets = filters.enabledAssets.size === 0;

  return (
    <div className="flex flex-col gap-2 border-b border-slate-800 bg-slate-950/30 px-3 py-2">
      {/* Row 1 — scope + shape + view-mode toggles */}
      <div className="flex flex-wrap items-center gap-2">
        <ScopeButton
          active={filters.scope === 'all'}
          onClick={() => onChange({ ...filters, scope: 'all' })}
        >
          All Nodes
        </ScopeButton>
        <ScopeButton
          active={filters.scope === 'factors'}
          onClick={() => onChange({ ...filters, scope: 'factors' })}
        >
          Factors Only
        </ScopeButton>
        <ScopeButton
          active={filters.scope === 'strategies'}
          onClick={() => onChange({ ...filters, scope: 'strategies', factorPairMode: false })}
        >
          Strategies Only
        </ScopeButton>

        <span className="mx-2 text-slate-700">|</span>

        <ScopeButton
          active={filters.shape === 'dot'}
          onClick={() => onChange({ ...filters, shape: 'dot' })}
          icon={<Layers className="h-3 w-3" />}
        >
          Dots
        </ScopeButton>
        <ScopeButton
          active={filters.shape === 'box'}
          onClick={() => onChange({ ...filters, shape: 'box' })}
          icon={<BoxSelect className="h-3 w-3" />}
        >
          Boxes
        </ScopeButton>
        <ScopeButton
          active={filters.shape === 'diamond'}
          onClick={() => onChange({ ...filters, shape: 'diamond' })}
          icon={<Diamond className="h-3 w-3" />}
        >
          Diamonds
        </ScopeButton>

        <span className="mx-2 text-slate-700">|</span>

        <ScopeButton
          active={filters.factorPairMode}
          onClick={() => onChange({ ...filters, factorPairMode: !filters.factorPairMode })}
          icon={<Hash className="h-3 w-3" />}
        >
          Factor×Factor
        </ScopeButton>
        <ScopeButton
          active={filters.grangerDag}
          onClick={() => onChange({ ...filters, grangerDag: !filters.grangerDag })}
          icon={<GitBranch className="h-3 w-3" />}
        >
          Granger DAG
        </ScopeButton>

        <button
          onClick={() => {
            onReset();
            // P11-F4-05 — also tell the FactorGraph to fit() its viewport so
            // the camera resets along with the filter state. The graph listens
            // for this custom event on `window`.
            if (typeof window !== 'undefined') {
              window.dispatchEvent(new CustomEvent('factor-network:reset-view'));
            }
          }}
          className="ml-auto flex items-center gap-1 rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-[10px] uppercase tracking-widest text-slate-400 hover:text-slate-200"
        >
          <RotateCcw className="h-3 w-3" /> Reset View
        </button>
      </div>

      {/* Row 2 — IC range band (P12/C-H1: bi-directional two-handle slider).
          The legacy single-handle IC≥ slider was promoted to a true band so
          operators can isolate e.g. only mean-reversion (negative IC) factors
          without also having to wade through high-IC outliers, or vice versa.
          The inert Strangler placeholder slider was removed (P12/C-H3) — edge
          weights still aren't wired through, and the disabled control was
          confusing testers who expected it to do something. */}
      <div className="flex flex-wrap items-center gap-3 text-[10px] font-mono uppercase tracking-wider text-slate-400">
        <label className="flex items-center gap-2">
          <span className="text-slate-500">IC ≥</span>
          <input
            type="range"
            min={-1}
            max={3}
            step={0.05}
            value={filters.icMin}
            onChange={(e) => {
              const next = Number(e.target.value);
              // Clamp so the min handle never crosses the max handle.
              onChange({ ...filters, icMin: Math.min(next, filters.icMax) });
            }}
            className="h-1 w-32 accent-cyan-500"
          />
          <span className="w-10 text-right text-cyan-300">{filters.icMin.toFixed(2)}</span>
        </label>
        <label className="flex items-center gap-2">
          <span className="text-slate-500">IC ≤</span>
          <input
            type="range"
            min={-1}
            max={3}
            step={0.05}
            value={filters.icMax}
            onChange={(e) => {
              const next = Number(e.target.value);
              // Clamp so the max handle never drops below the min handle.
              onChange({ ...filters, icMax: Math.max(next, filters.icMin) });
            }}
            className="h-1 w-32 accent-cyan-500"
          />
          <span className="w-10 text-right text-cyan-300">{filters.icMax.toFixed(2)}</span>
        </label>
      </div>

      {/* Row 3 — asset filter pills (only show when whitelist has matches) */}
      {visibleAssets.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 text-[10px] font-mono">
          <ScopeButton
            active={isAllAssets}
            onClick={() => onChange({ ...filters, enabledAssets: new Set() })}
          >
            ALL
          </ScopeButton>
          {visibleAssets.map((sym) => {
            const on = filters.enabledAssets.has(sym);
            return (
              <button
                key={sym}
                onClick={() => setAsset(sym, !on)}
                className={clsx(
                  'rounded-md border px-2 py-0.5 transition',
                  on
                    ? 'border-cyan-600 bg-cyan-500/15 text-cyan-200'
                    : 'border-slate-800 bg-slate-950 text-slate-500 hover:border-slate-600 hover:text-slate-200',
                )}
              >
                {sym}
              </button>
            );
          })}
        </div>
      )}


      {/* Row 4 — category chips */}
      {categories.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <ScopeButton
            active={isAllCategories}
            onClick={() => onChange({ ...filters, enabledCategories: new Set() })}
          >
            All Categories
          </ScopeButton>
          {categories.map((cat) => {
            const on = filters.enabledCategories.has(cat);
            const color = categoryColor(cat);
            return (
              <button
                key={cat}
                onClick={() => setCategory(cat, !filters.enabledCategories.has(cat))}
                className={clsx(
                  'rounded-md border px-2 py-0.5 text-[10px] font-mono transition',
                  on
                    ? 'border-slate-600 bg-slate-900 text-slate-100'
                    : 'border-slate-800 bg-slate-950 text-slate-600',
                )}
                style={on ? { borderColor: color, color } : undefined}
              >
                <span
                  className="mr-1 inline-block h-2 w-2 rounded-full align-middle"
                  style={{ background: color, opacity: on ? 1 : 0.4 }}
                />
                {cat}
              </button>
            );
          })}
        </div>
      )}

      {filters.grangerDag && (
        <div className="rounded border border-amber-700/40 bg-amber-500/10 px-2 py-1 text-[10px] text-amber-300">
          Granger DAG mode requires <code className="text-amber-100">GRANGER_ENABLED=1</code> + a populated
          IC history. Edges are dimmed when no Granger p-value is available.
        </div>
      )}

      {/* P17/D-L2 — large-dataset hint. vis-network barnes-hut starts to feel
          laggy past ~300 nodes; nudge the operator toward filters before the
          UI feels broken. The filter chips above already cover scope /
          category / asset / IC band so the path forward is one click away. */}
      {data && data.nodes.length > 300 && (
        <div className="rounded border border-amber-700/40 bg-amber-500/10 px-2 py-1 text-[10px] text-amber-300">
          Large dataset ({data.nodes.length} nodes) — apply scope, category or
          IC filters to reduce physics load.
        </div>
      )}
    </div>
  );
}


function ScopeButton({
  active,
  onClick,
  icon,
  children,
}: {
  active: boolean;
  onClick: () => void;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={clsx(
        'flex items-center gap-1 rounded-md border px-2 py-1 text-[10px] font-bold uppercase tracking-wider transition',
        active
          ? 'border-cyan-700 bg-cyan-500/15 text-cyan-200'
          : 'border-slate-700 bg-slate-900 text-slate-400 hover:text-slate-200',
      )}
    >
      {icon}
      {children}
    </button>
  );
}
