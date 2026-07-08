'use client';

import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import { MessageSquare, Activity } from 'lucide-react';
import { api, type DaemonLogEvent } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import AgentDialogue from './AgentDialogue';

/**
 * F3 — bottom-row Mission Control feed.
 *
 * Two modes (toggled via a single icon button in the header):
 *   - "Stream"   — the original colour-coded daemon log, now filterable by
 *                  clickable severity Pills (info / warn / error). The Pill
 *                  state is a `Set` so the operator can stack filters.
 *   - "Dialogue" — `<AgentDialogue />` (P8-FIX/H-7 agent-to-agent transcript).
 */
const STATUS_COLORS: Record<string, string> = {
  INTAKE: '#22D3EE',
  STORY_GEN: '#22C55E',
  CODE_GEN: '#A855F7',
  BACKTESTING: '#3B82F6',
  CRITIC_LOOP: '#F59E0B',
  APPROVED: '#10B981',
  PAPER_TRADE: '#F97316',
  SMALL_CAPITAL: '#FB923C',
  LIVE: '#10B981',
  REJECTED: '#EF4444',
  GRAVEYARD: '#6B7280',
};

type Sev = 'info' | 'warn' | 'error';
const ALL_SEVS: Sev[] = ['info', 'warn', 'error'];
const SEV_COLOR: Record<Sev, string> = {
  info: '#22D3EE',
  warn: '#F59E0B',
  error: '#EF4444',
};

const ERROR_RE = /\b(fatal|error|rejected)\b/i;
const WARN_RE = /\b(warn|retry|failed)\b/i;

function severity(ev: { status?: string; line: string }): Sev {
  // ev.status is the canonical signal — pipeline writes REJECTED here.
  const status = (ev.status || '').toUpperCase();
  if (status === 'REJECTED') return 'error';
  const line = ev.line || '';
  if (ERROR_RE.test(line)) return 'error';
  if (WARN_RE.test(line)) return 'warn';
  return 'info';
}

const SOURCE_RE = /^\s*\[([^\]]+)\]/;
function sourceOf(ev: { agent?: string; line: string }): string {
  const agent = (ev.agent || '').trim();
  if (agent) return agent;
  const m = SOURCE_RE.exec(ev.line || '');
  return (m?.[1] || 'system').trim();
}

export default function DaemonLog() {
  // Severity filter — default ON for all three so the panel renders unchanged
  // until the operator clicks one off.
  const [sevFilter, setSevFilter] = useState<Set<Sev>>(
    () => new Set(ALL_SEVS),
  );
  // F3 — source filter. Empty Set means "All Sources" (no filter applied).
  const [sourceFilter, setSourceFilter] = useState<string>('all');
  const [showDialogue, setShowDialogue] = useState(false);

  // P16 A-M8 — keep the daemon-log polling on regardless of the
  // dialogue view toggle. The previous `enabled: !showDialogue` gate
  // gave the illusion of saving round-trips, but `AgentTeam.tsx`
  // already polls the same `queryKeys.daemonLog(80)` every 4s, so the
  // network traffic never actually paused. Dropping the gate also
  // means the daemon-log cache stays fresh for any other consumer
  // that mounts while the dialogue view is open.
  // P29-F7: align with HeaderBarV2's 4s cadence on the same queryKey.
  // Shared cache uses fastest interval globally — 1.5s here DoS'd the backend.
  const q = useQuery({
    queryKey: queryKeys.daemonLog(80),
    queryFn: () => api.daemonLog(80),
    refetchInterval: 4_000,
  });

  const events: DaemonLogEvent[] = useMemo(
    () => q.data?.events ?? [],
    [q.data],
  );

  const allSources = useMemo(() => {
    const s = new Set<string>();
    for (const ev of events) s.add(sourceOf(ev));
    return Array.from(s).sort();
  }, [events]);

  const visibleEvents = useMemo(() => {
    let passed =
      sevFilter.size === ALL_SEVS.length
        ? events
        : events.filter((ev) => sevFilter.has(severity(ev)));
    if (sourceFilter !== 'all') {
      passed = passed.filter((ev) => sourceOf(ev) === sourceFilter);
    }
    return passed;
  }, [events, sevFilter, sourceFilter]);

  function togglePill(s: Sev) {
    setSevFilter((prev) => {
      const next = new Set(prev);
      if (next.has(s)) {
        next.delete(s);
        // Never let the operator filter every severity out — re-open the
        // previously-removed one if they would otherwise leave it empty.
        if (next.size === 0) next.add(s);
      } else {
        next.add(s);
      }
      return next;
    });
  }

  return (
    <section className="flex h-full flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/50">
      <header className="flex items-center justify-between border-b border-slate-800 px-4 py-2">
        {/* P13 A-L1 — keep the panel title stable as "Research Daemon Live
            Stream"; mode toggle is reflected in a subtitle suffix so the
            operator's eye can anchor on a constant identifier. */}
        <h2 className="text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
          Research Daemon Live Stream
          {showDialogue && (
            <span className="ml-2 text-slate-500 normal-case tracking-normal">
              · agent dialogue
            </span>
          )}
        </h2>
        <div className="flex items-center gap-2">
          {!showDialogue && (
            <div className="flex items-center gap-1 rounded-md border border-slate-800 bg-slate-950/40 px-1 py-0.5">
              {ALL_SEVS.map((s) => (
                <PillButton
                  key={s}
                  label={s}
                  color={SEV_COLOR[s]}
                  active={sevFilter.has(s)}
                  onClick={() => togglePill(s)}
                />
              ))}
            </div>
          )}
          {!showDialogue && (
            <select
              value={sourceFilter}
              onChange={(e) => setSourceFilter(e.target.value)}
              className="rounded border border-slate-800 bg-slate-950 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-slate-300"
              title="Filter daemon log by source"
            >
              <option value="all">All Sources</option>
              {allSources.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          )}
          <button
            onClick={() => setShowDialogue((v) => !v)}
            className={clsx(
              'inline-flex items-center gap-1 rounded border px-2 py-0.5 text-[9px] font-bold uppercase tracking-wider transition',
              showDialogue
                ? 'border-purple-600 bg-purple-500/15 text-purple-200 hover:bg-purple-500/25'
                : 'border-slate-700 bg-slate-900 text-slate-400 hover:border-slate-600 hover:text-slate-200',
            )}
            title={
              showDialogue
                ? 'Switch back to the raw daemon log stream'
                : 'Switch to the agent-to-agent dialogue transcript'
            }
          >
            {showDialogue ? (
              <>
                <Activity className="h-3 w-3" /> Show Log
              </>
            ) : (
              <>
                <MessageSquare className="h-3 w-3" /> Show Dialogue
              </>
            )}
          </button>
        </div>
      </header>

      {showDialogue ? (
        <AgentDialogue />
      ) : (
        <div className="flex-1 overflow-y-auto px-3 py-2 font-mono text-[10px] leading-relaxed">
          {visibleEvents.length === 0 ? (
            <div className="flex h-full items-center justify-center text-slate-600">
              {q.isLoading
                ? 'Connecting to research daemon…'
                : events.length === 0
                ? 'No active pipelines — kick one off via the INGEST button.'
                : 'All matching events filtered out — toggle a severity pill back on.'}
            </div>
          ) : (
            visibleEvents.map((ev, i) => (
              // P30-F12 (amended): key is content-based to avoid tearing down
              // stable rows on every poll (backend sorts newest-first so index
              // alone shifts on every prepend). The index suffix `i` is a
              // tiebreaker only: for any given poll snapshot, two rows at
              // different positions cannot share the same key even when their
              // content is identical (e.g. repeated heartbeat lines). Across
              // consecutive polls, a row that did not shift position keeps the
              // same key → React reuses the DOM node. A new prepended entry
              // gets index 0; previously-rendered rows shift by 1 but their
              // content-hash prefix already differs from any other row at that
              // new index, so React still avoids full teardown for the tail.
              <LogRow
                key={`${ev.strategy_id}-${(ev.status ?? '')}-${(ev.agent ?? '')}-${(ev.line ?? '').slice(0, 60)}-${i}`}
                ev={ev}
              />
            ))
          )}
        </div>
      )}
    </section>
  );
}

function LogRow({ ev }: { ev: DaemonLogEvent }) {
  const sev = severity(ev);
  const sevColor = SEV_COLOR[sev];
  const statusColor = STATUS_COLORS[(ev.status || '').toUpperCase()] ?? '#64748B';
  return (
    <div
      className={clsx(
        'group flex items-start gap-2 border-b border-slate-900/60 py-0.5',
        sev === 'error' && 'bg-rose-500/5',
      )}
    >
      <span className="shrink-0" style={{ color: sevColor }}>
        ●
      </span>
      <span className="min-w-[3rem] shrink-0 text-slate-600">S#{ev.strategy_id}</span>
      <span
        className="w-20 shrink-0 truncate text-[9px] font-bold uppercase tracking-wider"
        style={{ color: statusColor }}
      >
        {ev.status}
      </span>
      <span className="w-16 shrink-0 truncate text-slate-500">{ev.agent}</span>
      <span className="flex-1 whitespace-pre-wrap break-all text-slate-300">
        {ev.line}
      </span>
    </div>
  );
}

function PillButton({
  label,
  color,
  active,
  onClick,
}: {
  label: string;
  color: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={clsx(
        'flex items-center gap-1 rounded border px-1.5 py-0.5 text-[9px] uppercase tracking-wider transition',
        active
          ? 'border-slate-700 bg-slate-900 text-slate-200'
          : 'border-slate-800 bg-slate-950/40 text-slate-600 hover:text-slate-400',
      )}
      title={`Toggle ${label} severity ${active ? 'off' : 'on'}`}
      aria-pressed={active}
    >
      <span
        className="inline-block h-1.5 w-1.5 rounded-full"
        style={{ background: active ? color : '#334155' }}
      />
      {label}
    </button>
  );
}
