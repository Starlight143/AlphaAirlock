'use client';

import { Suspense, useEffect, useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import clsx from 'clsx';
import { FileText, Tag, ExternalLink, Layers, Search, Network, FolderTree } from 'lucide-react';
import { api, type KnowledgeNode } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { markdownComponents, remarkMathPlugins, rehypeMathPlugins } from '@/lib/markdownComponents';
import KnowledgeGraph from '@/components/KnowledgeGraph';
import { safeExternalUrl } from '@/lib/safeUrl';

/**
 * /kb-explorer — 3-pane Knowledge Sources browser (P6-A10/A11 overhaul).
 *
 *  ┌────────────┬────────────────────────────────┬────────────────────────┐
 *  │ Categories │ Tabs + Search + File list      │ Article preview        │
 *  │ (19 items) │ (ALL / INDEX / OUTPUT / D&D... │ (markdown + meta)      │
 *  └────────────┴────────────────────────────────┴────────────────────────┘
 *
 * P6 additions:
 *   - 5-tab segment (ALL FILES / INDEX / OUTPUT / D&D / DEAD) that filters the
 *     middle list by kind/duplicate-status.
 *   - Title-and-tag search input above the file list.
 *   - Multi-select checkbox on each row (selected count surfaced in the
 *     section heading; future bulk-delete / re-tag actions will hook here).
 *   - Client-side pagination — show 200 at a time, "Load more" extends.
 *   - Expanded KindChip palette (output / index / paper / datasource).
 *   - Preview pane H2 rose (was cyan) — matches the reference screenshot.
 *   - Left rail footer shows the grand-total node count (P5 → P6 fold-in).
 *
 * P8-FIX additions:
 *   - C-1: Graph view toggle — mounts <KnowledgeGraph> overlay; clicking a
 *     knowledge node flips back to files view + selects it.
 *   - H-2: Postmortem tab + reverse-link rail in the preview pane (queries
 *     /api/knowledge/{id}/postmortems for past_alpha nodes).
 *   - M-2: ReactMarkdown rendered via shared safe img / external-link
 *     transformers (markdownComponents).
 */

// P11-F3-10 — added 'dead' tab to surface graveyard / postmortem nodes.
type KbTab = 'all' | 'index' | 'output' | 'dnd' | 'dead';
type ViewMode = 'graph' | 'files';

const TAB_DEFS: { id: KbTab; label: string }[] = [
  { id: 'all', label: 'All Files' },
  { id: 'index', label: 'Index' },
  { id: 'output', label: 'Output' },
  { id: 'dnd', label: 'D&D' },
  { id: 'dead', label: 'Retired' },
];

const PAGE_SIZE = 200;
// P11-F3-09 — persisted view-mode key (graph vs files).
const VIEW_MODE_STORAGE_KEY = 'kb-explorer.viewMode';


export default function KbExplorerPage() {
  // Next.js 14 requires every useSearchParams() call to live inside a Suspense
  // boundary, otherwise the production build (`next build`) fails to prerender
  // this route. Mirrors the pattern used by /backtest-lab and the (shell) layout.
  return (
    <Suspense fallback={null}>
      <KbExplorerInner />
    </Suspense>
  );
}

function KbExplorerInner() {
  const catsQ = useQuery({
    queryKey: queryKeys.kbCategories,
    queryFn: api.kbCategories,
    refetchInterval: 30_000,
  });

  // P11-F3-13 — Next router for programmatic navigation (replaces the
  // window.location.href deep link used by handleGraphNodeClick).
  const router = useRouter();
  // P11-EXT-1 — read ?node=<id> so callers like P4-F4-06's "Open in KB"
  // button can preselect the article.
  const searchParams = useSearchParams();
  const presetNodeId = useMemo(() => {
    const raw = searchParams?.get('node');
    const n = raw ? Number(raw) : NaN;
    return Number.isFinite(n) ? n : null;
  }, [searchParams]);

  const [category, setCategory] = useState<string>('all');
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [tab, setTab] = useState<KbTab>('all');
  const [search, setSearch] = useState('');
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [visibleCount, setVisibleCount] = useState<number>(PAGE_SIZE);
  // P8-FIX/C-1 — view-mode toggle. P16/B-M12 — lazy initializer reads
  // localStorage during the initial render so the operator never sees a
  // flash of the wrong view (the legacy version defaulted to 'graph' then
  // rehydrated to 'files' inside a mount effect, fighting both the
  // localStorage preference AND the `?node=` deep-link redirect). Falls
  // back to 'files' during SSR / private-mode browsers / invalid stored
  // values.
  const [viewMode, setViewMode] = useState<ViewMode>(() => {
    if (typeof window === 'undefined') return 'files';
    try {
      const v = window.localStorage.getItem(VIEW_MODE_STORAGE_KEY);
      return v === 'graph' || v === 'files' ? v : 'files';
    } catch {
      return 'files';
    }
  });
  // A-H3 — top-level mode switch: KB Explorer (default) vs Code Files.
  const [topMode, setTopMode] = useState<'kb' | 'code'>('kb');
  // Bumped on intake / refresh so KnowledgeGraph can rebuild without parent
  // remount.
  const [graphRefreshKey, setGraphRefreshKey] = useState(0);

  // P11-F3-09 — persist whenever viewMode changes. P16/B-M12 removed the
  // mount-time rehydrate effect; the initial value now comes from the lazy
  // `useState` initializer above, eliminating the flash-of-graph race.
  useEffect(() => {
    try {
      window.localStorage.setItem(VIEW_MODE_STORAGE_KEY, viewMode);
    } catch {
      // ignore
    }
  }, [viewMode]);

  // P11-EXT-1 — if a ?node=<id> is supplied, flip to the file list so the
  // preselected article is actually visible in the preview pane.
  useEffect(() => {
    if (presetNodeId != null) setViewMode('files');
  }, [presetNodeId]);

  // P12-B-M6 — when the URL carries ?node=<id>, fetch the node up-front so we
  // know its category. That lets us flip the left-rail category to the right
  // bucket before the per-category list ever loads — otherwise the preset
  // node might fall outside the default 'all' slice and never appear in the
  // middle list.
  const presetNodeQ = useQuery({
    queryKey: queryKeys.knowledgeOne(presetNodeId ?? 0),
    queryFn: () => api.knowledgeOne(presetNodeId!),
    enabled: presetNodeId != null,
    staleTime: 60_000,
  });

  // P12-B-M6 — once the preset node payload arrives: switch category to its
  // bucket (if any), pin the selection, and ensure we're in 'files' mode.
  useEffect(() => {
    const node = presetNodeQ.data;
    if (!node) return;
    setCategory((curr) => node.category ?? curr);
    setSelectedId(node.id);
    setViewMode('files');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [presetNodeQ.data]);

  // Pull a generous slice so client-side pagination can extend without
  // a backend roundtrip until the user crosses 1000 nodes per category.
  const filesQ = useQuery({
    queryKey: queryKeys.kbByCategory(category, 1000),
    queryFn: () => api.kbByCategory(category, 1000),
    enabled: !!category,
  });

  // Reset checked selection + pagination cursor when filters change.
  // Also clear selectedId when tab/search changes and the currently-selected
  // node is no longer visible in allFiltered (prevents invisible highlight).
  // We do NOT clear on category change because the category useEffect below
  // already re-selects the first node for the new category.
  useEffect(() => {
    setChecked(new Set());
    setVisibleCount(PAGE_SIZE);
  }, [category, tab, search]);

  // Auto-select first file when category changes (only when nothing chosen).
  // P11-EXT-1 — honors `?node=<id>` so deep links from /strategies/:id preview
  // panes (P4-F4-06) land directly on the requested article.
  useEffect(() => {
    if (!filesQ.data) return;
    setSelectedId((curr) => {
      if (curr != null) return curr;
      if (
        presetNodeId != null &&
        filesQ.data?.nodes?.some((n) => n.id === presetNodeId)
      ) {
        return presetNodeId;
      }
      return filesQ.data?.nodes?.[0]?.id ?? null;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filesQ.data?.category, presetNodeId]);

  const allFiltered = useMemo(() => {
    const nodes = filesQ.data?.nodes ?? [];
    const hashCount = new Map<string, number>();
    for (const m of nodes) if (m.content_hash) hashCount.set(m.content_hash, (hashCount.get(m.content_hash) ?? 0) + 1);
    const tabFiltered = nodes.filter((n) => tabMatches(n, tab, nodes, hashCount));
    const lower = search.trim().toLowerCase();
    if (!lower) return tabFiltered;
    return tabFiltered.filter((n) => {
      const haystack = `${n.title || ''} ${(n.tags || []).join(' ')}`.toLowerCase();
      return haystack.includes(lower);
    });
  }, [filesQ.data, tab, search]);

  const paged = useMemo(() => allFiltered.slice(0, visibleCount), [allFiltered, visibleCount]);

  // If selectedId is set but not present in the current allFiltered result
  // (e.g. a tab/search filter excluded it), clear the selection so the
  // preview pane and list stay consistent. Placed AFTER allFiltered/paged
  // so the memo it depends on is initialized first (TS temporal-dead-zone).
  useEffect(() => {
    if (selectedId == null) return;
    if (allFiltered.length === 0 && filesQ.isLoading) return; // still loading — not zero results
    const stillVisible = allFiltered.some((n) => n.id === selectedId);
    if (!stillVisible) {
      setSelectedId(null);
    }
  }, [allFiltered, selectedId, filesQ.isLoading]);

  const selected = useMemo(
    () => allFiltered.find((n) => n.id === selectedId) ?? null,
    [allFiltered, selectedId],
  );

  const categories = catsQ.data?.categories ?? [];
  const totalNodes = catsQ.data?.total ?? 0;
  const allNodes = filesQ.data?.nodes ?? [];

  const toggleChecked = (id: number) => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // P8-FIX/C-1 — KnowledgeGraph callback.
  //   - knowledge nodes flip back to file list + select the node so the
  //     ArticlePreview rail repopulates immediately.
  //   - strategy nodes deep-link to /strategies/:id (full detail view).
  // P11-F3-13 — replaced `window.location.href` (full page reload) with the
  // Next router so navigation stays inside the app shell and React Query
  // cache survives the transition.
  const handleGraphNodeClick = (kind: 'knowledge' | 'strategy', id: number) => {
    if (kind === 'strategy') {
      router.push(`/strategies/${id}`);
      return;
    }
    setSelectedId(id);
    setViewMode('files');
  };

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      {/* A-H3 — top-level mode switch */}
      <div className="mb-1 flex items-center gap-1 border-b border-slate-800 px-1">
        <ModeButton active={topMode === 'kb'} onClick={() => setTopMode('kb')}>KB EXPLORER</ModeButton>
        <ModeButton active={topMode === 'code'} onClick={() => setTopMode('code')}>CODE FILES</ModeButton>
      </div>
      {topMode === 'code' && <CodeFilesView />}
      {topMode === 'kb' && (
    <div className="grid h-full w-full grid-cols-12 gap-3 overflow-hidden p-3">
      {/* Left: category tree */}
      <aside aria-label="Knowledge categories" className="col-span-2 flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
        <header className="flex items-center justify-between border-b border-slate-800 px-3 py-2">
          <span className="text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
            Knowledge Sources
          </span>
          <span className="text-[9px] text-slate-500">{totalNodes} total</span>
        </header>
        <nav className="flex-1 overflow-y-auto p-1 text-[11px]">
          <CategoryRow
            label="All Files"
            count={totalNodes}
            active={category === 'all'}
            onClick={() => setCategory('all')}
          />
          {categories.map((c) => (
            <CategoryRow
              key={c.key}
              label={c.label}
              count={c.count}
              active={category === c.key}
              onClick={() => setCategory(c.key)}
            />
          ))}
        </nav>
        {/* L10: Total stats footer */}
        <footer className="border-t border-slate-800 px-3 py-2 text-[10px] uppercase tracking-widest text-slate-500">
          Total: <span className="font-mono text-slate-300">{totalNodes}</span>
        </footer>
      </aside>

      {/* Middle: file list with tabs + search + pagination — OR — graph view */}
      <section className="col-span-10 flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
        <header className="flex items-center justify-between border-b border-slate-800 px-3 py-2">
          <div className="flex items-center gap-1">
            <ViewModeButton
              active={viewMode === 'graph'}
              onClick={() => setViewMode('graph')}
              icon={<Network className="h-3 w-3" />}
            >
              Graph
            </ViewModeButton>
            <ViewModeButton
              active={viewMode === 'files'}
              onClick={() => setViewMode('files')}
              icon={<FolderTree className="h-3 w-3" />}
            >
              Files
            </ViewModeButton>
          </div>
          {viewMode === 'graph' && (
            <button
              onClick={() => setGraphRefreshKey((k) => k + 1)}
              className="rounded border border-slate-700 px-2 py-0.5 text-[10px] uppercase tracking-widest text-slate-300 hover:bg-slate-800"
            >
              Refresh Graph
            </button>
          )}
        </header>

        {viewMode === 'graph' ? (
          <div className="flex flex-1 flex-col overflow-hidden">
            <KnowledgeGraph
              onNodeClick={handleGraphNodeClick}
              refreshKey={graphRefreshKey}
            />
          </div>
        ) : (
          <div className="grid flex-1 grid-cols-10 overflow-hidden">
            {/* Middle list */}
            <section className="col-span-4 flex flex-col overflow-hidden border-r border-slate-800">
              <header className="border-b border-slate-800">
                {/* Row 1: tabs */}
                <div className="flex items-center gap-1 px-2 py-1">
                  {TAB_DEFS.map((t) => (
                    <button
                      key={t.id}
                      onClick={() => setTab(t.id)}
                      className={clsx(
                        'rounded-sm px-2 py-1 font-mono text-[10px] font-bold uppercase tracking-widest transition-colors',
                        tab === t.id
                          ? 'bg-cyan-500/15 text-cyan-300'
                          : 'text-slate-500 hover:bg-slate-900 hover:text-slate-300',
                      )}
                      // F5-3: D&D duplicate detection is scoped to the active
                      // category slice — surface this limitation in the label
                      // so operators are not misled into thinking cross-category
                      // duplicates are surfaced when a specific category is
                      // selected.
                      title={
                        t.id === 'dnd' && category !== 'all'
                          ? 'Duplicate & Dangling — scoped to current category only. Switch to "All" to detect cross-category duplicates.'
                          : undefined
                      }
                    >
                      {t.id === 'dnd' && category !== 'all' ? 'D&D (cat)' : t.label}
                    </button>
                  ))}
                </div>
                {/* Row 2: search + count */}
                <div className="flex items-center justify-between gap-2 border-t border-slate-800 px-3 py-1.5">
                  <div className="flex flex-1 items-center gap-1.5">
                    <Search className="h-3 w-3 text-slate-500" />
                    <input
                      type="text"
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                      placeholder="Search title or tags…"
                      className="min-w-0 flex-1 bg-transparent text-[11px] text-slate-200 placeholder:text-slate-600 focus:outline-none"
                    />
                  </div>
                  <span className="text-[10px] text-slate-500">
                    {paged.length}/{allFiltered.length} files
                    {checked.size > 0 && (
                      <span className="ml-2 rounded border border-cyan-700/40 bg-cyan-500/10 px-1.5 py-0.5 text-cyan-300">
                        {checked.size} selected
                      </span>
                    )}
                  </span>
                </div>
              </header>
              <div className="flex-1 overflow-y-auto">
                {filesQ.isLoading && (
                  <div className="p-3 text-xs text-slate-500">Loading…</div>
                )}
                {filesQ.isError && (
                  <div className="p-3 text-xs text-rose-400">
                    Failed to load files:{' '}
                    {(filesQ.error as Error)?.message ?? 'Unknown error'}
                  </div>
                )}
                {paged.map((n) => (
                  <div
                    key={n.id}
                    className={clsx(
                      'flex items-start gap-2 border-b border-slate-900 px-3 py-2',
                      selectedId === n.id && 'bg-cyan-500/10',
                    )}
                  >
                    <input
                      type="checkbox"
                      checked={checked.has(n.id)}
                      onChange={() => toggleChecked(n.id)}
                      onClick={(e) => e.stopPropagation()}
                      className="mt-1 h-3 w-3 shrink-0 accent-cyan-500"
                    />
                    <button
                      onClick={() => setSelectedId(n.id)}
                      className="block min-w-0 flex-1 text-left text-[11px] hover:opacity-80"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="line-clamp-1 text-slate-200">
                          <FileText className="mr-1 inline h-3 w-3 text-cyan-400" />
                          {filename(n)}
                        </span>
                        <KindChip kind={n.kind} />
                      </div>
                      <div className="mt-0.5 line-clamp-1 text-[10px] text-slate-500">
                        {n.title}
                      </div>
                    </button>
                  </div>
                ))}
                {filesQ.data && allFiltered.length === 0 && (
                  <div className="p-6 text-center text-xs text-slate-500">
                    {search
                      ? 'No files match this search query.'
                      : 'No files in this category yet. Add a source via /sources or use the +INGEST button.'}
                  </div>
                )}
                {allFiltered.length > visibleCount && (
                  <div className="p-3 text-center">
                    <button
                      onClick={() => setVisibleCount((c) => c + PAGE_SIZE)}
                      className="rounded border border-slate-700 px-3 py-1 text-[10px] uppercase tracking-widest text-slate-300 hover:bg-slate-800"
                    >
                      Load Next {Math.min(PAGE_SIZE, allFiltered.length - visibleCount)}
                    </button>
                  </div>
                )}
              </div>
            </section>

            {/* Right: preview */}
            <section className="col-span-6 flex flex-col overflow-hidden">
              {selected ? (
                <ArticlePreview
                  node={selected}
                  allNodes={allNodes}
                  onJump={setSelectedId}
                />
              ) : (
                <div className="flex h-full items-center justify-center text-xs text-slate-500">
                  Click a file to preview its content
                </div>
              )}
            </section>
          </div>
        )}
      </section>
    </div>
      )}
    </div>
  );
}


function tabMatches(n: KnowledgeNode, tab: KbTab, all: KnowledgeNode[], hashCount?: Map<string, number>): boolean {
  switch (tab) {
    case 'all':
      return true;
    case 'index':
      // P14/B-H5 — include 'index'-typed nodes alongside 'concept'.
      return n.kind === 'concept' || String(n.kind) === 'index';
    case 'output':
      // OUTPUT = active strategies only (postmortems moved to their own tab
      // in P8-FIX/H-2).
      return n.kind === 'active';
    case 'dnd': {
      // Duplicates & Dangling — same content_hash appearing >1 time, OR no
      // outbound links AND zero IC (i.e. nothing references this node).
      if (n.content_hash) {
        const dupCount = hashCount != null ? (hashCount.get(n.content_hash) ?? 0) : all.filter((m) => m.content_hash === n.content_hash).length;
        if (dupCount > 1) return true;
      }
      const isDangling =
        (n.links?.length ?? 0) === 0 && (n.ic_score ?? 0) === 0;
      if (!isDangling) return false;
      // P12-B-M8 — fresh nodes (< 24h old) haven't had a chance to be linked
      // by the indexer yet. Suppress them from D&D so the operator's queue
      // isn't flooded with bootstrap noise that auto-clears within a day.
      if (
        n.created_at &&
        Date.now() - Date.parse(n.created_at) < 86_400_000
      ) {
        return false;
      }
      return true;
    }
    case 'dead': {
      // P11-F3-10 — retired / postmortem / graveyard nodes. Matches when the
      // node kind itself is past_alpha/postmortem OR any of the well-known
      // "dead" tags appear on the node.
      if (n.kind === 'past_alpha' || n.kind === 'postmortem') return true;
      const tags = (n.tags ?? []).map((t) => t.toLowerCase());
      return tags.some((t) => t === 'postmortem' || t === 'graveyard' || t === 'retired');
    }
    default:
      return true;
  }
}


function CategoryRow({
  label,
  count,
  active,
  onClick,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={clsx(
        'flex w-full items-center justify-between rounded px-2 py-1 text-left',
        active
          ? 'bg-cyan-500/10 text-cyan-200'
          : 'text-slate-400 hover:bg-slate-900 hover:text-slate-200',
      )}
    >
      <span className="truncate">{label}</span>
      <span className="font-mono text-[9px] text-slate-600">{count}</span>
    </button>
  );
}


function ViewModeButton({
  active,
  onClick,
  icon,
  children,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
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


// P6-A10 expanded palette — adds output / index / paper / datasource colours
// for upcoming backend kind enum extensions.
const KIND_STYLE: Record<string, string> = {
  past_alpha: 'text-rose-300 ring-rose-700/40',
  postmortem: 'text-fuchsia-300 ring-fuchsia-700/40',
  active: 'text-cyan-300 ring-cyan-700/40',
  concept: 'text-emerald-300 ring-emerald-700/40',
  output: 'text-amber-300 ring-amber-700/40',
  index: 'text-sky-300 ring-sky-700/40',
  paper: 'text-violet-300 ring-violet-700/40',
  datasource: 'text-orange-300 ring-orange-700/40',
};


function KindChip({ kind }: { kind: string }) {
  const color = KIND_STYLE[kind] ?? 'text-slate-400 ring-slate-700/40';
  return (
    <span
      className={`shrink-0 rounded px-1 py-0.5 text-[8px] font-bold uppercase tracking-wider ring-1 ring-inset ${color}`}
    >
      {kind}
    </span>
  );
}


function filename(n: KnowledgeNode): string {
  const ts = n.ingested_at || n.created_at;
  const date = ts ? ts.slice(0, 10) : 'undated';
  // P12-B-L6 + P16/B-L4 — preserve CJK characters in slugs. The legacy range
  // (U+4E00..U+9FFF, hiragana/katakana, hangul) missed CJK Unified
  // Ideographs Extension A (U+3400..U+4DBF) and Extension B+ (U+20000+).
  // Widening the range to U+3400..U+9FFF and merging the kana band gives
  // full coverage for rare/historical Chinese characters without
  // re-collapsing to "----".
  const slug = (n.title || `node-${n.id}`)
    .toLowerCase()
    .replace(/[^a-z0-9㐀-鿿぀-ヿ가-힯]+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 60);
  return `${date}-${slug}`;
}


function ArticlePreview({
  node,
  allNodes,
  onJump,
}: {
  node: KnowledgeNode;
  allNodes: KnowledgeNode[];
  onJump: (id: number) => void;
}) {
  // P8-FIX/H-2 — only fetch postmortems for past_alpha nodes (the reverse
  // link only makes sense for retired strategies).
  const postmortemsQ = useQuery({
    queryKey: queryKeys.knowledgePostmortems(node.id),
    queryFn: () => api.knowledgePostmortems(node.id),
    enabled: node.kind === 'past_alpha',
    staleTime: 60_000,
  });

  // Title lookup table for Related Concepts chips — falls back to K#id when
  // the linked node is not in the currently-loaded category slice.
  const titleById = useMemo(() => {
    const m = new Map<number, string>();
    for (const n of allNodes) m.set(n.id, n.title || `K#${n.id}`);
    return m;
  }, [allNodes]);

  const links = node.links ?? [];
  const derivedSid = node.auto_pipeline_strategy_id ?? null;
  const postmortems = postmortemsQ.data?.postmortems ?? [];
  const postmortemStrategyIds = postmortemsQ.data?.strategy_ids ?? [];

  return (
    <>
      <header className="border-b border-slate-800 px-5 py-3">
        <div className="flex items-center justify-between">
          <div className="flex items-baseline gap-2">
            <Layers className="h-3.5 w-3.5 text-cyan-400" />
            <span className="text-[10px] uppercase tracking-widest text-slate-500">
              {node.category ?? 'unclassified'}
            </span>
            <span className="font-mono text-[10px] text-slate-600">
              K#{node.id}
            </span>
          </div>
          {node.source_url && (() => {
            // P29-F11: sanitize source_url scheme.
            const safe = safeExternalUrl(node.source_url);
            if (!safe) {
              return (
                <span
                  className="flex items-center gap-1 text-[10px] font-mono text-slate-500"
                  title={`Blocked URL: ${node.source_url}`}
                >
                  <ExternalLink className="h-3 w-3" />
                  [blocked]
                </span>
              );
            }
            return (
              <a
                href={safe}
                target="_blank"
                rel="noreferrer noopener"
                title={safe}
                className="flex items-center gap-1 text-[10px] text-cyan-300 hover:underline"
              >
                <ExternalLink className="h-3 w-3" />
                source
              </a>
            );
          })()}
        </div>
        <h1 className="mt-1 text-base font-bold text-slate-100">{node.title}</h1>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {node.tags.map((t) => (
            <span
              key={t}
              className="flex items-center gap-1 rounded border border-slate-800 bg-slate-950 px-1.5 py-0.5 text-[9px] text-slate-400"
            >
              <Tag className="h-2.5 w-2.5" />
              {t}
            </span>
          ))}
        </div>
      </header>

      <div className="flex-1 overflow-y-auto">
        <article className="prose prose-invert prose-sm max-w-none p-5 text-slate-200 prose-headings:text-slate-100 prose-h1:text-base prose-h2:text-sm prose-h2:font-bold prose-h2:tracking-wider prose-h2:uppercase prose-h2:text-rose-300 prose-h3:text-amber-300 prose-h4:text-cyan-300 prose-code:text-emerald-300">
          <ReactMarkdown components={markdownComponents} remarkPlugins={remarkMathPlugins} rehypePlugins={rehypeMathPlugins}>
            {node.content || '_(no body)_'}
          </ReactMarkdown>
        </article>

        {/* P8-FIX/H-2 + related rails */}
        <div className="space-y-3 border-t border-slate-800 bg-slate-950/30 px-5 py-4">
          {links.length > 0 && (
            <section>
              <h3 className="mb-1.5 text-[10px] font-bold uppercase tracking-[0.2em] text-cyan-300">
                Related Concepts
              </h3>
              <div className="flex flex-wrap gap-1.5">
                {links.map((linkId) => {
                  const title = titleById.get(linkId) ?? `K#${linkId}`;
                  return (
                    <button
                      key={linkId}
                      onClick={() => onJump(linkId)}
                      className="inline-flex max-w-[240px] items-baseline rounded border border-slate-700 bg-slate-900 px-2 py-0.5 text-[10px] text-slate-200 hover:border-cyan-600 hover:text-cyan-200"
                      title={`Jump to K#${linkId}`}
                    >
                      <span className="font-mono text-[9px] text-slate-500">K#{linkId}</span>
                      <span className="ml-1 line-clamp-1 inline-block max-w-[180px] align-bottom">
                        {title}
                      </span>
                    </button>
                  );
                })}
              </div>
            </section>
          )}

          {derivedSid != null && (
            <section>
              <h3 className="mb-1.5 text-[10px] font-bold uppercase tracking-[0.2em] text-amber-300">
                Derived Code
              </h3>
              <Link
                href={`/strategies/${derivedSid}`}
                className="inline-flex items-center gap-1.5 rounded border border-amber-700/40 bg-amber-500/10 px-2 py-1 text-[10px] text-amber-200 hover:bg-amber-500/20"
              >
                <ExternalLink className="h-3 w-3" />
                Strategy S#{derivedSid}
              </Link>
            </section>
          )}

          {node.kind === 'past_alpha' && (
            <section>
              <h3 className="mb-1.5 text-[10px] font-bold uppercase tracking-[0.2em] text-fuchsia-300">
                Postmortems
              </h3>
              {postmortemsQ.isLoading ? (
                <div className="text-[10px] text-slate-500">Loading…</div>
              ) : postmortemsQ.isError ? (
                <div className="text-[10px] text-rose-300">
                  Failed to load: {(postmortemsQ.error as Error).message}
                </div>
              ) : postmortems.length === 0 ? (
                <div className="text-[10px] text-slate-500">
                  No postmortem write-ups attached yet.
                </div>
              ) : (
                <div className="space-y-1.5">
                  {postmortems.map((pm) => (
                    <button
                      key={pm.id}
                      onClick={() => onJump(pm.id)}
                      className="block w-full rounded border border-fuchsia-700/40 bg-fuchsia-500/5 px-2 py-1 text-left text-[10px] text-fuchsia-200 hover:bg-fuchsia-500/15"
                    >
                      <div className="flex items-center justify-between">
                        <span className="font-mono text-[9px] text-fuchsia-400">
                          K#{pm.id}
                        </span>
                        <span className="text-[9px] uppercase tracking-widest text-fuchsia-400/70">
                          postmortem
                        </span>
                      </div>
                      <div className="mt-0.5 line-clamp-2 text-[11px] text-slate-100">
                        {pm.title}
                      </div>
                    </button>
                  ))}
                  {postmortemStrategyIds.length > 0 && (
                    <div className="pt-1 text-[10px] text-slate-500">
                      Sources:{' '}
                      {postmortemStrategyIds.map((sid, idx) => (
                        <span key={sid}>
                          <Link
                            href={`/strategies/${sid}`}
                            className="text-cyan-300 hover:underline"
                          >
                            S#{sid}
                          </Link>
                          {idx < postmortemStrategyIds.length - 1 ? ', ' : ''}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </section>
          )}
        </div>
      </div>
    </>
  );
}


// A-H3 — top-level mode switch button (KB EXPLORER | CODE FILES)
function ModeButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={clsx(
        'border-b-2 px-3 py-2 text-[11px] font-bold uppercase tracking-wider transition',
        active
          ? 'border-cyan-400 text-cyan-200'
          : 'border-transparent text-slate-500 hover:text-slate-200',
      )}
    >
      {children}
    </button>
  );
}


// A-H3 — Strategy code files browser. Lists every strategy with a non-empty
// formula_code and renders it in a read-only panel.
function CodeFilesView() {
  const q = useQuery({ queryKey: queryKeys.strategies, queryFn: api.strategies });
  const items = (q.data?.strategies ?? []).filter((s) => (s.formula_code ?? '').trim() !== '');
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const selected = items.find((s) => s.id === selectedId) ?? items[0] ?? null;

  // P18-C3 — group by status so the file list mirrors the folder hierarchy
  // the reference screenshot suggests. The previous flat list scaled badly
  // past ~30 strategies; status buckets give the operator one-click visual
  // separation between LIVE / PAPER_TRADE / APPROVED tiers.
  const grouped = useMemo(() => {
    const order = ['LIVE', 'SMALL_CAPITAL', 'PAPER_TRADE', 'APPROVED'];
    const map = new Map<string, typeof items>();
    for (const s of items) {
      const key = s.status || 'OTHER';
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(s);
    }
    return Array.from(map.entries()).sort((a, b) => {
      const ai = order.indexOf(a[0]);
      const bi = order.indexOf(b[0]);
      if (ai === -1 && bi === -1) return a[0].localeCompare(b[0]);
      if (ai === -1) return 1;
      if (bi === -1) return -1;
      return ai - bi;
    });
  }, [items]);

  return (
    <div className="grid h-full grid-cols-[280px_1fr] gap-3 p-3">
      <aside aria-label="Strategy code files" className="overflow-y-auto rounded-xl border border-slate-800 bg-slate-900/40">
        <div className="border-b border-slate-800 px-3 py-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">
          Strategy Code Files ({items.length})
        </div>
        {items.length === 0 ? (
          <div className="px-3 py-4 text-xs text-slate-500">No strategy code files.</div>
        ) : (
          grouped.map(([status, group]) => (
            <section key={status}>
              <header className="border-b border-slate-800 bg-slate-950/60 px-3 py-1 text-[9px] font-bold uppercase tracking-[0.2em] text-slate-500">
                {status} <span className="font-mono text-slate-600">({group.length})</span>
              </header>
              <ul className="divide-y divide-slate-800/60">
                {group.map((s) => (
                  <li key={s.id}>
                    <button
                      onClick={() => setSelectedId(s.id)}
                      className={clsx(
                        'block w-full px-3 py-2 text-left font-mono text-[11px] hover:bg-slate-800/30',
                        selected?.id === s.id ? 'bg-slate-800/50 text-cyan-200' : 'text-slate-300',
                      )}
                    >
                      <div className="truncate">{s.slug ?? s.name}</div>
                      <div className="text-[9px] uppercase tracking-wider text-slate-500">
                        S#{s.id}
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ))
        )}
      </aside>
      <section className="overflow-y-auto rounded-xl border border-slate-800 bg-slate-950/40 p-3">
        {selected ? (
          <>
            <header className="mb-2 flex items-baseline justify-between">
              <h3 className="font-mono text-[12px] font-bold uppercase tracking-widest text-cyan-300">
                {selected.slug ?? selected.name}
              </h3>
              <span className="text-[9px] uppercase tracking-widest text-slate-600">
                S#{selected.id}
              </span>
            </header>
            <pre className="overflow-x-auto rounded border border-slate-800 bg-slate-950 p-3 text-[11px] leading-relaxed text-emerald-300">
{(selected.formula_code ?? '').trim()}
            </pre>
          </>
        ) : (
          <div className="text-xs text-slate-500">Select a strategy to view its formula code.</div>
        )}
      </section>
    </div>
  );
}
