'use client';

import { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import { Telescope, Search, X } from 'lucide-react';
import { api, type AlphaStrategy, type KnowledgeNode, type NodeKind } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { icColorClass } from '@/lib/icColor';
import { safeExternalUrl } from '@/lib/safeUrl';

type SortKey = 'ic' | 'updated' | 'usage';

const KIND_LABELS: Record<NodeKind, string> = {
  concept: 'Concept',
  past_alpha: 'Past Alpha',
  active: 'Active',
  postmortem: 'Postmortem',
};

const KIND_COLOR: Record<NodeKind, string> = {
  concept: 'text-emerald-300 border-emerald-700/40',
  past_alpha: 'text-rose-300 border-rose-700/40',
  active: 'text-cyan-300 border-cyan-700/40',
  postmortem: 'text-purple-300 border-purple-700/40',
};

// P12/C-L2 — fallback chip style for any KnowledgeNode whose `kind` lands
// outside the declared NodeKind union (e.g. a future backend value the
// frontend hasn't been redeployed for). Prevents the table rendering a
// `text-undefined border-undefined/40` class soup, which used to wipe the
// chip background entirely and make the cell look broken.
const KIND_FALLBACK_COLOR = 'text-slate-300 border-slate-700/40';

const IC_SLIDER = { min: -1, max: 1, step: 0.05 } as const;

// P-perf — mirror kb-explorer: render at most PAGE_SIZE rows at once with a
// client-side "Load Next" affordance. /api/knowledge is server-capped at
// 1000 rows, so this bounds the DOM at 200 until the operator opts in.
const PAGE_SIZE = 200;

/**
 * /factor-explorer — IC slider + category filter + sortable factor library.
 * P18-C6 — Renamed user-visible "Factor Catalogue" header (and JSDoc copy)
 * to "Factor Library" to match the transcript naming convention.
 * Pure client-side composition of /api/knowledge + /api/strategies (no new
 * backend route).
 */
export default function FactorExplorerPage() {
  const [icMin, setIcMin] = useState(-1);
  const [icMax, setIcMax] = useState(1);
  const [search, setSearch] = useState('');
  const [activeKinds, setActiveKinds] = useState<Set<NodeKind>>(new Set());
  const [activeCats, setActiveCats] = useState<Set<string>>(new Set());
  const [sort, setSort] = useState<SortKey>('ic');
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [visibleCount, setVisibleCount] = useState<number>(PAGE_SIZE);

  const knowledgeQ = useQuery({ queryKey: queryKeys.knowledge, queryFn: api.knowledge });
  const strategiesQ = useQuery({ queryKey: queryKeys.strategies, queryFn: api.strategies });

  const nodes = knowledgeQ.data?.nodes ?? [];
  const strategies = strategiesQ.data?.strategies ?? [];

  // Build node-id → strategies-that-use-it index for the "Used By" column.
  const usageIndex = useMemo(() => {
    const map = new Map<number, AlphaStrategy[]>();
    for (const s of strategies) {
      const srcs = (s.config?.source_node_ids as number[] | undefined) ?? [];
      for (const nid of srcs) {
        if (!map.has(nid)) map.set(nid, []);
        map.get(nid)!.push(s);
      }
    }
    return map;
  }, [strategies]);

  const categories = useMemo(() => {
    const set = new Set<string>();
    for (const n of nodes) if (n.category) set.add(n.category);
    return Array.from(set).sort();
  }, [nodes]);

  const filtered = useMemo(() => {
    let rows = nodes.filter((n) => {
      const hasIc = Number.isFinite(n.ic_score); if (hasIc && (n.ic_score < icMin || n.ic_score > icMax)) return false;
      if (activeKinds.size > 0 && !activeKinds.has(n.kind)) return false;
      if (activeCats.size > 0 && !activeCats.has(n.category || 'unclassified')) return false;
      if (search.trim()) {
        const needle = search.trim().toLowerCase();
        const hay = `${n.title} ${n.tags.join(' ')} ${n.content ?? ''}`.toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      return true;
    });
    rows = rows.slice().sort((a, b) => {
      if (sort === 'usage') {
        return (usageIndex.get(b.id)?.length ?? 0) - (usageIndex.get(a.id)?.length ?? 0);
      }
      if (sort === 'updated') {
        return (b.created_at || '').localeCompare(a.created_at || '');
      }
      // ic desc — treat NaN IC as -Infinity so non-finite rows sink to the
      // bottom deterministically (NaN comparators make Array.sort unstable).
      return (Number.isFinite(b.ic_score) ? b.ic_score : -Infinity) - (Number.isFinite(a.ic_score) ? a.ic_score : -Infinity);
    });
    return rows;
  }, [nodes, icMin, icMax, activeKinds, activeCats, search, sort, usageIndex]);

  const paged = useMemo(() => filtered.slice(0, visibleCount), [filtered, visibleCount]);

  // F9-2 fix: total node count for the header badge denominator.
  // Using nodes.length (not nodes.filter(isFinite(ic_score)).length) guarantees
  // filtered.length <= totalCount always holds, because the IC-range filter
  // only excludes nodes that *have* a finite ic_score outside [icMin,icMax] —
  // nodes without a finite ic_score always pass the filter and are counted in
  // filtered.length but were previously excluded from the denominator.
  const totalCount = useMemo(() => nodes.length, [nodes]);

  // Reset the pagination cursor whenever the filtered set changes (any filter,
  // search, or sort change), so the operator always starts at the first page.
  useEffect(() => {
    setVisibleCount(PAGE_SIZE);
  }, [icMin, icMax, activeKinds, activeCats, search, sort, usageIndex]);

  const selected = selectedId != null ? nodes.find((n) => n.id === selectedId) ?? null : null;

  return (
    <div className="grid h-full w-full grid-cols-12 gap-3 overflow-hidden p-3">
      {/* Left rail — filters */}
      <aside aria-label="Factor filters" className="col-span-3 flex flex-col gap-3 overflow-y-auto rounded-xl border border-slate-800 bg-slate-900/40 p-3">
        <h1 className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
          <Telescope className="h-3.5 w-3.5 text-cyan-400" />
          Factor Explorer
        </h1>

        <div>
          <div className="mb-1 text-[10px] uppercase tracking-widest text-slate-500">Search</div>
          <div className="flex items-center gap-1 rounded-md border border-slate-700 bg-slate-950 px-2 py-1">
            <Search className="h-3 w-3 text-slate-500" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="title or tag…"
              aria-label="Search factors by title or tag"
              className="w-full bg-transparent text-[11px] text-slate-100 outline-none"
            />
            {search && (
              <button
                type="button"
                onClick={() => setSearch('')}
                aria-label="Clear search"
                title="Clear search"
                className="shrink-0 rounded p-0.5 text-slate-500 transition hover:text-slate-200"
              >
                <X className="h-3 w-3" />
              </button>
            )}
          </div>
        </div>

        <div>
          <div className="mb-1 text-[10px] uppercase tracking-widest text-slate-500">IC Range</div>
          <div className="flex items-center gap-2 text-[10px] text-slate-400">
            <input
              type="range"
              min={IC_SLIDER.min}
              max={IC_SLIDER.max}
              step={IC_SLIDER.step}
              value={icMin}
              onChange={(e) => setIcMin(Math.min(Number(e.target.value), icMax))}
              aria-label="Minimum IC"
              className="flex-1 accent-cyan-500"
            />
            <span className="w-12 font-mono">{icMin.toFixed(2)}</span>
          </div>
          <div className="mt-1 flex items-center gap-2 text-[10px] text-slate-400">
            <input
              type="range"
              min={IC_SLIDER.min}
              max={IC_SLIDER.max}
              step={IC_SLIDER.step}
              value={icMax}
              onChange={(e) => setIcMax(Math.max(Number(e.target.value), icMin))}
              aria-label="Maximum IC"
              className="flex-1 accent-cyan-500"
            />
            <span className="w-12 font-mono">{icMax.toFixed(2)}</span>
          </div>
        </div>

        <div>
          <div className="mb-1 text-[10px] uppercase tracking-widest text-slate-500">
            Kind {activeKinds.size === 0 ? '· all kinds shown' : `· ${activeKinds.size} selected`}
          </div>
          <div className="flex flex-wrap gap-1">
            {(Object.keys(KIND_LABELS) as NodeKind[]).map((k) => {
              // P13/B-M2 — show explicit selection state only. The empty-set
              // "all kinds shown" condition is communicated by the hint label
              // above, not by lighting every chip (which used to invert the
              // moment the operator made their first click and looked broken).
              const on = activeKinds.has(k);
              return (
                <button
                  key={k}
                  onClick={() => {
                    const next = new Set(activeKinds);
                    if (next.has(k)) next.delete(k);
                    else next.add(k);
                    setActiveKinds(next);
                  }}
                  className={clsx(
                    'rounded border px-2 py-0.5 text-[10px] font-mono',
                    on ? KIND_COLOR[k] : 'border-slate-800 bg-slate-950 text-slate-600',
                  )}
                >
                  {KIND_LABELS[k]}
                </button>
              );
            })}
          </div>
        </div>

        <div>
          <div className="mb-1 text-[10px] uppercase tracking-widest text-slate-500">
            Category {activeCats.size > 0 && `· ${activeCats.size} selected`}
          </div>
          <div className="flex flex-wrap gap-1">
            {categories.length === 0 && (
              <span className="text-[10px] text-slate-600">— no categories yet —</span>
            )}
            {categories.map((c) => {
              const on = activeCats.has(c);
              return (
                <button
                  key={c}
                  onClick={() => {
                    const next = new Set(activeCats);
                    if (next.has(c)) next.delete(c);
                    else next.add(c);
                    setActiveCats(next);
                  }}
                  className={clsx(
                    'rounded border px-2 py-0.5 text-[10px] font-mono',
                    on
                      ? 'border-cyan-700 bg-cyan-500/10 text-cyan-200'
                      : 'border-slate-800 bg-slate-950 text-slate-500',
                  )}
                >
                  {c}
                </button>
              );
            })}
          </div>
        </div>

        <div>
          <div className="mb-1 text-[10px] uppercase tracking-widest text-slate-500">Sort By</div>
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as SortKey)}
            aria-label="Sort factors by"
            className="w-full rounded-md border border-slate-700 bg-slate-950 px-2 py-1 text-[11px] text-slate-200 outline-none focus:border-cyan-600"
          >
            <option value="ic">IC (descending)</option>
            <option value="updated">Created (newest)</option>
            <option value="usage">Used by N strategies</option>
          </select>
        </div>
      </aside>

      {/* Center — factor table */}
      <section className="col-span-6 flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
        <header className="flex items-center justify-between border-b border-slate-800 px-4 py-2">
          <h2 className="text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
            Factor Library
          </h2>
          <span className="text-[10px] text-slate-500">{filtered.length} of {totalCount} {totalCount === 1 ? 'node' : 'nodes'}</span>
        </header>
        <div className="flex-1 overflow-y-auto">
          <table className="w-full border-separate border-spacing-0 text-[11px]">
            <thead className="sticky top-0 z-10 bg-slate-900 text-[9px] uppercase tracking-wider text-slate-500">
              <tr>
                <Th>Factor</Th>
                <Th className="w-24">Category</Th>
                <Th className="w-20 text-right">IC</Th>
                <Th className="w-24">Kind</Th>
                <Th className="w-24 text-right">Used By</Th>
                <Th className="w-32">Updated</Th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {paged.map((n) => {
                const uses = usageIndex.get(n.id) ?? [];
                return (
                  <tr
                    key={n.id}
                    onClick={() => setSelectedId(selectedId === n.id ? null : n.id)}
                    aria-selected={selectedId === n.id}
                    className={clsx(
                      'cursor-pointer border-b border-slate-900/40 hover:bg-slate-950/40',
                      selectedId === n.id && 'bg-cyan-500/5',
                    )}
                  >
                    <Td>
                      <span className="text-cyan-300">K#{n.id}</span>
                      <span className="ml-2 text-slate-200">{n.title}</span>
                    </Td>
                    <Td className="text-slate-400">{n.category ?? '—'}</Td>
                    <Td className="text-right">
                      {/* P16/B-H4 + B-L9 — IC colour ladder moved to
                          `@/lib/icColor` so the negative side mirrors
                          the positive intensity buckets (strong
                          negative IC stayed visually weak under the
                          legacy single-bucket rose fallback). */}
                      <span className={icColorClass(n.ic_score)}>
                        {Number.isFinite(n.ic_score) ? n.ic_score.toFixed(2) : '—'}
                      </span>
                    </Td>
                    <Td>
                      {/* P12/C-L2 — defensive fallbacks for unknown kinds.
                          A KnowledgeNode arriving with an unexpected `kind`
                          value would otherwise crash to `undefined` for
                          both the colour class and label; we now degrade
                          gracefully to a neutral slate chip carrying the
                          raw kind string. */}
                      <span className={clsx('rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase', KIND_COLOR[n.kind] ?? KIND_FALLBACK_COLOR)}>
                        {KIND_LABELS[n.kind] ?? n.kind}
                      </span>
                    </Td>
                    <Td className="text-right text-slate-300">{uses.length}</Td>
                    <Td className="text-slate-500">{(n.created_at ?? '').slice(0, 10) || '—'}</Td>
                  </tr>
                );
              })}
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-xs text-slate-600">
                    <div>No factors match the current filters.</div>
                    <button
                      onClick={() => {
                        setIcMin(-1);
                        setIcMax(1);
                        setSearch('');
                        setActiveKinds(new Set());
                        setActiveCats(new Set());
                        // P16/B-M8 — also reset the sort dropdown so "Reset
                        // Filters" returns to a fully-pristine state. The
                        // legacy version left the operator's last sort
                        // choice in place, which surprised testers.
                        setSort('ic');
                      }}
                      className="mt-3 rounded border border-slate-700 px-3 py-1 text-[10px] uppercase tracking-widest text-slate-300 hover:bg-slate-800 hover:text-cyan-300"
                    >
                      Reset Filters
                    </button>
                  </td>
                </tr>
              )}
              {filtered.length > visibleCount && (
                <tr>
                  <td colSpan={6} className="px-4 py-3 text-center">
                    <button
                      onClick={() => setVisibleCount((c) => c + PAGE_SIZE)}
                      className="rounded border border-slate-700 px-3 py-1 text-[10px] uppercase tracking-widest text-slate-300 hover:bg-slate-800 hover:text-cyan-300"
                    >
                      Load Next {Math.min(PAGE_SIZE, filtered.length - visibleCount)}
                    </button>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* Right — detail */}
      <aside aria-label="Factor detail" className="col-span-3 flex flex-col gap-3 overflow-y-auto">
        <DetailPane node={selected} uses={selected ? usageIndex.get(selected.id) ?? [] : []} />
      </aside>
    </div>
  );
}

function Th({ children, className }: { children?: React.ReactNode; className?: string }) {
  return <th scope="col" className={clsx('border-b border-slate-800 px-3 py-2 text-left', className)}>{children}</th>;
}
function Td({ children, className }: { children?: React.ReactNode; className?: string }) {
  return <td className={clsx('px-3 py-1.5', className)}>{children}</td>;
}

function DetailPane({ node, uses }: { node: KnowledgeNode | null; uses: AlphaStrategy[] }) {
  if (!node) {
    return (
      <div className="flex h-32 items-center justify-center rounded-xl border border-slate-800 bg-slate-900/40 p-4 text-center text-xs text-slate-500">
        Select a factor to view its details and downstream strategies.
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
        <div className="text-[10px] uppercase tracking-widest text-slate-500">Factor</div>
        <div className="text-sm font-bold text-slate-100">{node.title}</div>
        <div className="mt-2 grid grid-cols-2 gap-2 text-[10px]">
          <KV label="K#" value={node.id.toString()} />
          <KV label="IC" value={Number.isFinite(node.ic_score) ? node.ic_score.toFixed(3) : '—'} />
          <KV label="Kind" value={node.kind} />
          <KV label="Category" value={node.category ?? '—'} />
        </div>
        {node.source_url && (() => {
          // P29-F11: render anchor only for http(s)/mailto schemes.
          const safe = safeExternalUrl(node.source_url);
          if (!safe) {
            return (
              <span
                className="mt-3 inline-block truncate text-[10px] font-mono text-slate-500"
                title={`Blocked URL: ${node.source_url}`}
              >
                [blocked URL] {node.source_url.slice(0, 38)}…
              </span>
            );
          }
          return (
            <a
              href={safe}
              target="_blank"
              rel="noreferrer noopener"
              className="mt-3 inline-block truncate text-[10px] text-cyan-300 hover:underline"
            >
              {safe.slice(0, 38)}…
            </a>
          );
        })()}
        {node.tags.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1">
            {node.tags.map((t) => (
              <span key={t} className="rounded border border-slate-700 bg-slate-900 px-1.5 py-0.5 text-[9px] text-slate-400">
                {t}
              </span>
            ))}
          </div>
        )}
        {node.content && (
          <pre className="mt-3 max-h-44 overflow-y-auto rounded border border-slate-800 bg-slate-950 p-2 font-mono text-[10px] leading-relaxed text-slate-300 whitespace-pre-wrap">
            {node.content.slice(0, 800)}
          </pre>
        )}
      </div>

      <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
        <div className="text-[10px] uppercase tracking-widest text-slate-500">
          Used by {uses.length} strategies
        </div>
        {uses.length === 0 ? (
          <div className="text-[11px] text-slate-600">No downstream usage yet.</div>
        ) : (
          <ul className="mt-2 space-y-1">
            {uses.map((s) => (
              <li key={s.id} className="text-[11px]">
                <Link
                  href={`/strategies/${s.id}`}
                  className="text-cyan-300 hover:underline"
                >
                  S#{s.id}
                </Link>{' '}
                <span className="text-slate-300">{s.slug ?? s.name}</span>
                <span className="ml-1 text-slate-600">[{s.status}]</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function KV({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[8px] uppercase tracking-widest text-slate-600">{label}</div>
      <div className="font-mono text-[12px] text-slate-200">{value}</div>
    </div>
  );
}
