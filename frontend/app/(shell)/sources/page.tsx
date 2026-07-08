'use client';

import { Suspense, useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Plus, Power, AlertCircle, Activity, MessageSquare, Hash } from 'lucide-react';
import { api, type IngestSource, type KnowledgeNode, type SourceType } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import SourceCard from '@/components/sources/SourceCard';
import AddSourceModal from '@/components/sources/AddSourceModal';
import SourcesTopTabs from '@/components/sources/SourcesTopTabs';
import SourceTypeFilter from '@/components/sources/SourceTypeFilter';
import SourceUrlQuickAdd from '@/components/sources/SourceUrlQuickAdd';
import SourceFilesDrawer from '@/components/sources/SourceFilesDrawer';
import { sourceTypeLabel, type SourceCategory } from '@/lib/sourceTypes';

/**
 * /sources — P5 layout (reference UI parity).
 *
 *  ┌───────────────────────────────────────────────────────────────┐
 *  │ Header: title + scheduler banner + KPI strip (8 tiles)        │
 *  ├───────────────────────────────────────────────────────────────┤
 *  │ Top tabs: All / Apps / YouTube / DPS / ... (categorical)      │
 *  ├───────────────────────────────────────────────────────────────┤
 *  │ Toolbar: source-type filter dropdown + ADD SOURCE button       │
 *  ├───────────────────────────────────────────────────────────────┤
 *  │ Grouped cards (section heading per source type, e.g.           │
 *  │ "RSS Feeds (12)") in a responsive grid.                        │
 *  └───────────────────────────────────────────────────────────────┘
 */
export default function SourcesPage() {
  // Next.js 14 requires every useSearchParams() call to live inside a Suspense
  // boundary, otherwise the production build (`next build`) fails to prerender
  // this route. Mirrors the pattern used by /backtest-lab and the (shell) layout.
  return (
    <Suspense fallback={null}>
      <SourcesInner />
    </Suspense>
  );
}

function SourcesInner() {
  const [modalOpen, setModalOpen] = useState(false);
  const [tab, setTab] = useState<SourceCategory | 'all'>('all');
  const [typeFilter, setTypeFilter] = useState<SourceType | 'all'>('all');
  // P11-F4-07 — "Only Failing" filter toggle. When true, the source list is
  // narrowed to entries with consecutive_failures > 0 OR enabled === false
  // (paused). Sits in the header action row next to ADD SOURCE.
  const [onlyFailing, setOnlyFailing] = useState(false);
  // P6-A2 — file drawer state (null when closed). Holds the entire source so
  // the drawer doesn't need to refetch the IngestSource row.
  const [drawerSource, setDrawerSource] = useState<IngestSource | null>(null);
  // P16 A-M9 — reactive to URL changes. The earlier mount-only
  // `useEffect` snapshotted `?debug=1` once at hydrate; client-side
  // navigation to/from `?debug=1` wouldn't update the flag.
  const searchParams = useSearchParams();
  const debugMode = searchParams?.get('debug') === '1';

  const sourcesQ = useQuery({
    queryKey: queryKeys.sources,
    queryFn: api.sources,
    refetchInterval: 6_000,
  });
  const schedQ = useQuery({
    queryKey: queryKeys.schedulerStatus,
    queryFn: api.schedulerStatus,
    refetchInterval: 10_000,
  });
  // P8-FIX/H-17 + C-5 — Bot Inbound card sources.
  const tgIntakeQ = useQuery({
    queryKey: queryKeys.telegramRecentIntakes(5),
    queryFn: () => api.telegramRecentIntakes(5),
    refetchInterval: 15_000,
  });
  const dcIntakeQ = useQuery({
    queryKey: queryKeys.discordRecentIntakes(5),
    queryFn: () => api.discordRecentIntakes(5),
    refetchInterval: 15_000,
  });

  const allSources: IngestSource[] = sourcesQ.data?.sources ?? [];
  const supportedTypes = sourcesQ.data?.supported_types ?? [];

  // P103 — Keep the drawer's IngestSource snapshot in sync with the latest
  // poll result. Without this, `consecutive_failures`, `disabled_until`, and
  // other mutable fields shown in the drawer would be stale until the operator
  // closes and reopens it. The effect only mutates when the drawer is open
  // (drawerSource !== null) and a matching fresh entry exists in allSources.
  // It never nullifies drawerSource, so it cannot accidentally close the drawer.
  useEffect(() => {
    if (!drawerSource) return;
    const fresh = allSources.find((s) => s.id === drawerSource.id);
    if (fresh) {
      setDrawerSource(fresh);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allSources]);

  const filtered = useMemo(() => {
    return allSources.filter((s) => {
      if (tab !== 'all' && s.category !== tab) return false;
      if (typeFilter !== 'all' && s.source_type !== typeFilter) return false;
      // P11-F4-07 — "Only Failing" filter. A source is considered "failing"
      // when it has consecutive backend failures (>0) OR has been paused
      // (enabled === false). Both states need operator attention.
      if (onlyFailing) {
        const failing = s.consecutive_failures > 0 || !s.enabled;
        if (!failing) return false;
      }
      return true;
    });
  }, [allSources, tab, typeFilter, onlyFailing]);

  const totals = useMemo(() => {
    const active = allSources.filter((s) => s.enabled).length;
    const failing = allSources.filter((s) => s.consecutive_failures > 0 || !s.enabled).length;
    const stub = allSources.filter((s) => s.is_stub).length;
    const na = allSources.filter((s) => !s.category).length;
    const events24 = allSources.reduce((acc, s) => acc + (s.events_24h ?? 0), 0);
    const subscriptions = allSources.filter((s) => s.source_type === 'patreon' || s.source_type === 'substack').length;
    // P12-B-M2 — sources currently held open by the circuit-breaker after
    // repeated failures. Surfaced as its own KPI tile so the operator can
    // distinguish "consecutive_failures > 0" (will retry) from "tripped"
    // (sleeping until disabled_until expires).
    const inCircuitBreaker = allSources.filter(
      (s) => s.disabled_until && new Date(s.disabled_until).getTime() > Date.now(),
    ).length;
    return { active, failing, stub, na, events24, subscriptions, inCircuitBreaker };
  }, [allSources]);

  const grouped = useMemo(() => {
    const map = new Map<string, IngestSource[]>();
    for (const s of filtered) {
      if (!map.has(s.source_type)) map.set(s.source_type, []);
      map.get(s.source_type)!.push(s);
    }
    return Array.from(map.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  }, [filtered]);

  return (
    <div className="flex h-full w-full flex-col gap-3 overflow-hidden p-3">
      <header className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-sm font-bold tracking-widest text-slate-100">
              SOURCES
            </h1>
            <p className="mt-0.5 text-[11px] text-slate-500">
              Subscribed feeds the Research Daemon polls in the background.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <SourceUrlQuickAdd />
            <SourceTypeFilter
              value={typeFilter}
              onChange={setTypeFilter}
              supported={supportedTypes}
            />
            {/* P11-F4-07 — Only Failing toggle. Lights up rose when armed and
                shows the failing count for quick triage. */}
            <button
              onClick={() => setOnlyFailing((v) => !v)}
              title="Show only sources with consecutive failures or paused state"
              className={`flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-[11px] font-bold tracking-wide transition ${
                onlyFailing
                  ? 'border-rose-600 bg-rose-500/15 text-rose-200 hover:bg-rose-500/25'
                  : 'border-slate-700 bg-transparent text-slate-300 hover:bg-slate-800'
              }`}
            >
              <AlertCircle className="h-3.5 w-3.5" />
              {`Only Failing (${totals.failing})`}
            </button>
            <button
              onClick={() => setModalOpen(true)}
              className="flex items-center gap-1.5 rounded-md border border-purple-600 bg-transparent px-3 py-1.5 text-[11px] font-bold tracking-wide text-purple-300 hover:bg-purple-500/10"
            >
              <Plus className="h-3.5 w-3.5" />
              ADD SOURCE
            </button>
          </div>
        </div>

        {/* P13 A-M6 — KPI strip reduced to 7 tiles to match reference UI
            (screenshot 20-03-22). The P12-B-M2 "In CB" tile is dropped
            because it doesn't appear in the reference; the
            `inCircuitBreaker` total is still computed (cheap) for future
            tooltip use. Grid shifts from 8-col to 7-col on md+. */}
        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4 md:grid-cols-7">
          <Kpi label="Total" value={allSources.length.toString()} color="text-cyan-200" />
          <Kpi label="Active" value={totals.active.toString()} color="text-emerald-300" icon={<Power className="h-3 w-3" />} />
          <Kpi label="Subs" value={totals.subscriptions.toString()} color="text-cyan-200" />
          <Kpi label="Failing" value={totals.failing.toString()} color={totals.failing ? 'text-rose-300' : 'text-slate-500'} icon={<AlertCircle className="h-3 w-3" />} />
          <Kpi label="Stub" value={totals.stub.toString()} color={totals.stub ? 'text-amber-300' : 'text-slate-500'} />
          <Kpi label="N/A" value={totals.na.toString()} color="text-slate-400" />
          <Kpi label="Events 24h" value={totals.events24.toString()} color="text-emerald-300" icon={<Activity className="h-3 w-3" />} />
        </div>

        <div className="mt-3 flex items-center gap-3 text-[10px] text-slate-500">
          {schedQ.data?.enabled ? (
            schedQ.data.running ? (
              <span className="text-emerald-300" aria-label="scheduler running">
                <span aria-hidden="true">●</span> scheduler running · tick every {schedQ.data.tick_seconds}s
              </span>
            ) : (
              <span className="text-amber-300" aria-label="scheduler enabled but not running">
                <span aria-hidden="true">●</span> scheduler enabled but not running — check backend logs
              </span>
            )
          ) : (
            <span className="text-amber-300" aria-label="scheduler off">
              {/* P11-F4-09 — user-facing copy now points to the administrator
                  instead of dumping internal env-var instructions on every
                  non-technical viewer. The env-var hint is still surfaced when
                  the page is opened with ?debug=1 for ops debugging. */}
              <span aria-hidden="true">●</span> scheduler OFF — auto-ingest is paused — contact your administrator to enable polling
              {debugMode && (
                <>
                  {' '}
                  (set <code className="text-amber-100">ALPHA_INGEST_ENABLED=1</code> in .env and restart backend)
                </>
              )}
            </span>
          )}
        </div>
      </header>

      <div className="rounded-xl border border-slate-800 bg-slate-900/40">
        <SourcesTopTabs sources={allSources} active={tab} onChange={setTab} />
      </div>

      <main className="flex-1 overflow-y-auto pr-1">
        {sourcesQ.isLoading ? (
          <div className="py-8 text-center text-xs text-slate-500">Loading sources…</div>
        ) : filtered.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-700 bg-slate-900/30 py-12 text-center">
            <Plus className="mx-auto mb-2 h-8 w-8 text-slate-600" />
            <div className="text-sm text-slate-400">
              {allSources.length === 0
                ? 'No sources registered yet.'
                : 'No sources match the current filter.'}
            </div>
            <div className="mt-1 text-[11px] text-slate-600">
              {allSources.length === 0
                ? 'Click + ADD SOURCE to wire up your first RSS / Substack / YouTube feed.'
                : 'Clear filters or pick a different tab.'}
            </div>
          </div>
        ) : (
          <div className="space-y-5">
            {grouped.map(([type, items]) => (
              <section key={type}>
                <h2 className="mb-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">
                  {sourceTypeLabel(type)}{' '}
                  <span className="font-mono text-slate-600">({items.length})</span>
                </h2>
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                  {items.map((s) => (
                    <SourceCard key={s.id} source={s} onOpen={setDrawerSource} />
                  ))}
                </div>
              </section>
            ))}
          </div>
        )}
      </main>

      {/* A-L1 — Bot Inbound moved into a collapsible accordion below the main list */}
      <details className="mt-3 rounded-xl border border-slate-800 bg-slate-900/40">
        <summary className="cursor-pointer px-3 py-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400 hover:text-slate-200">
          Bots & Inbound
        </summary>
        <div className="border-t border-slate-800 p-3">
          <BotInboundCard
            telegram={schedQ.data?.telegram_inbound}
            discord={schedQ.data?.discord_inbound}
            telegramIntakes={tgIntakeQ.data?.items ?? []}
            discordIntakes={dcIntakeQ.data?.items ?? []}
          />
        </div>
      </details>

      <AddSourceModal open={modalOpen} onClose={() => setModalOpen(false)} />
      {drawerSource && (
        <SourceFilesDrawer
          source={drawerSource}
          onClose={() => setDrawerSource(null)}
        />
      )}
    </div>
  );
}

function Kpi({
  label,
  value,
  color,
  icon,
}: {
  label: string;
  value: string;
  color: string;
  icon?: React.ReactNode;
}) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-950/50 p-2">
      <div className={`flex items-center gap-1 font-mono text-base leading-none ${color}`}>
        {icon}
        {value}
      </div>
      <div className="mt-1 text-[9px] uppercase tracking-wider text-slate-500">
        {label}
      </div>
    </div>
  );
}

// P8-FIX/H-17 + C-5 ----------------------------------------------------------
// Bot Inbound — surfaces the Telegram + Discord /intake handlers so the
// operator can verify the bot heard the message and watch the last few items
// land in the KB. Reads from /api/scheduler/status (configured + running) and
// the two recent-intake endpoints (most recent K# rows the bot wrote).

type TelegramInboundBlock = {
  enabled: boolean;
  running: boolean;
  allowed_chats_count?: number;
  rate_interval_seconds?: number;
  last_update_id?: number;
  error?: string;
};
type DiscordInboundBlock = {
  enabled: boolean;
  running: boolean;
  allowed_channels_count?: number;
  last_event_at?: string | null;
  error?: string;
};

function BotInboundCard({
  telegram,
  discord,
  telegramIntakes,
  discordIntakes,
}: {
  telegram: TelegramInboundBlock | undefined;
  discord: DiscordInboundBlock | undefined;
  telegramIntakes: KnowledgeNode[];
  discordIntakes: KnowledgeNode[];
}) {
  // Merge + sort by ingested_at desc, keep top 5.
  const merged = useMemo(() => {
    const rows: { node: KnowledgeNode; bus: 'telegram' | 'discord' }[] = [
      ...telegramIntakes.map((n) => ({ node: n, bus: 'telegram' as const })),
      ...discordIntakes.map((n) => ({ node: n, bus: 'discord' as const })),
    ];
    rows.sort((a, b) => {
      const ta = a.node.ingested_at || a.node.created_at || '';
      const tb = b.node.ingested_at || b.node.created_at || '';
      return tb.localeCompare(ta);
    });
    return rows.slice(0, 5);
  }, [telegramIntakes, discordIntakes]);

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
      <header className="mb-2 flex items-center justify-between">
        <h2 className="text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
          Bot Inbound
        </h2>
        <span className="text-[10px] text-slate-500">
          Send <code className="text-cyan-300">/intake &lt;text&gt;</code> in an allowed channel
        </span>
      </header>

      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
        <BotRow
          icon={<MessageSquare className="h-3.5 w-3.5 text-cyan-300" />}
          label="Telegram /intake"
          summary={renderTelegramSummary(telegram)}
          status={telegramStatus(telegram)}
        />
        <BotRow
          icon={<Hash className="h-3.5 w-3.5 text-indigo-300" />}
          label="Discord /intake"
          summary={renderDiscordSummary(discord)}
          status={discordStatus(discord)}
        />
      </div>

      <div className="mt-3 border-t border-slate-800 pt-2">
        <div className="mb-1.5 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">
          Recent intakes
        </div>
        {merged.length === 0 ? (
          <div className="rounded border border-dashed border-slate-800 px-3 py-2 text-[10px] text-slate-500">
            No intakes received yet — once the bot writes its first node it will appear here.
          </div>
        ) : (
          <ul className="divide-y divide-slate-800/60">
            {merged.map(({ node, bus }) => (
              <li key={`${bus}-${node.id}`} className="flex items-center justify-between gap-2 py-1.5">
                <div className="flex min-w-0 items-center gap-2">
                  {bus === 'telegram' ? (
                    <MessageSquare className="h-3 w-3 shrink-0 text-cyan-300" />
                  ) : (
                    <Hash className="h-3 w-3 shrink-0 text-indigo-300" />
                  )}
                  <Link
                    href={`/kb-explorer?node=${node.id}`}
                    className="line-clamp-1 min-w-0 flex-1 text-[11px] text-slate-200 hover:text-cyan-200"
                    title={node.title}
                  >
                    <span className="font-mono text-[9px] text-slate-500">K#{node.id}</span>{' '}
                    {node.title || '(untitled)'}
                  </Link>
                </div>
                <span className="shrink-0 font-mono text-[9px] text-slate-500">
                  {formatTimeAgo(node.ingested_at || node.created_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

function BotRow({
  icon,
  label,
  summary,
  status,
}: {
  icon: React.ReactNode;
  label: string;
  summary: string;
  status: { dot: string; text: string };
}) {
  return (
    <div className="flex items-start gap-2 rounded-md border border-slate-800 bg-slate-950/50 p-2">
      <div className="mt-0.5">{icon}</div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span role="img" aria-label={status.text} className={`text-base leading-none ${status.dot}`}>●</span>
          <span className="text-[11px] font-bold text-slate-100">{label}</span>
          <span className={`text-[10px] uppercase tracking-wider ${status.dot}`}>
            {status.text}
          </span>
        </div>
        <div className="mt-1 font-mono text-[10px] text-slate-400">{summary}</div>
      </div>
    </div>
  );
}

function renderTelegramSummary(tg: TelegramInboundBlock | undefined): string {
  if (!tg) return '— not configured';
  const parts: string[] = [];
  if (tg.allowed_chats_count != null) {
    parts.push(`${tg.allowed_chats_count} allowed chat${tg.allowed_chats_count === 1 ? '' : 's'}`);
  }
  if (tg.last_update_id != null) {
    parts.push(`last_update_id=${tg.last_update_id}`);
  }
  if (tg.rate_interval_seconds != null) {
    parts.push(`poll ${tg.rate_interval_seconds}s`);
  }
  if (tg.error) {
    parts.push(`error: ${tg.error}`);
  }
  return parts.length > 0 ? parts.join(' · ') : '—';
}

function renderDiscordSummary(dc: DiscordInboundBlock | undefined): string {
  if (!dc) return '— not configured';
  const parts: string[] = [];
  if (dc.allowed_channels_count != null) {
    parts.push(`${dc.allowed_channels_count} channel${dc.allowed_channels_count === 1 ? '' : 's'}`);
  }
  if (dc.last_event_at) {
    parts.push(`last event ${formatTimeAgo(dc.last_event_at)}`);
  }
  if (dc.error) {
    parts.push(`error: ${dc.error}`);
  }
  return parts.length > 0 ? parts.join(' · ') : '—';
}

function telegramStatus(tg: TelegramInboundBlock | undefined): { dot: string; text: string } {
  if (!tg || !tg.enabled) return { dot: 'text-slate-500', text: 'disabled' };
  if (tg.error) return { dot: 'text-rose-300', text: 'error' };
  if (tg.running) return { dot: 'text-emerald-300', text: 'running' };
  return { dot: 'text-amber-300', text: 'enabled' };
}

function discordStatus(dc: DiscordInboundBlock | undefined): { dot: string; text: string } {
  if (!dc || !dc.enabled) return { dot: 'text-slate-500', text: 'disabled' };
  if (dc.error) return { dot: 'text-rose-300', text: 'error' };
  if (dc.running) return { dot: 'text-emerald-300', text: 'running' };
  return { dot: 'text-amber-300', text: 'enabled' };
}

function formatTimeAgo(ts: string | null | undefined): string {
  if (!ts) return '—';
  const t = Date.parse(ts);
  if (!Number.isFinite(t)) return '—';
  const diffMs = Date.now() - t;
  if (diffMs < 0) return 'in future';
  const sec = Math.floor(diffMs / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
}
