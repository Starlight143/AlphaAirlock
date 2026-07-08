'use client';

import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Plus, Wifi, WifiOff } from 'lucide-react';
import { api, type DaemonLogEvent } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { useIngestDialog } from './IngestDialogProvider';
import { navItemFor } from './nav';
import { usePathname, useSearchParams } from 'next/navigation';
import { verbOfActive } from '@/lib/agentVerbs';

// P16 A-M10 — defensive hex+alpha concatenation (see AgentTeam.tsx).
// Kept inline per planner spec; if a third call site appears, promote
// to a shared lib/colors module.
function withAlpha(hex: string, alpha: string): string {
  if (/^#[0-9a-fA-F]{6}$/.test(hex)) return hex + alpha;
  return '#475569' + alpha;
}

/**
 * Slim, reference-faithful header.
 *
 * The original (HeaderBar.tsx) stuffed KPI metric cards into the header. The
 * YouTuber's reference UI keeps the header almost empty (title + LIVE pill +
 * search + clock) and pushes KPIs into the left rail of Mission Control.
 *
 * This V2 follows that. The legacy HeaderBar remains untouched so we can A/B
 * during the refactor; it will be removed when the strategies/[id] page lands
 * a dedicated header.
 */
export default function HeaderBarV2() {
  const pathname = usePathname() || '';
  const item = navItemFor(pathname);

  const healthQ = useQuery({
    queryKey: queryKeys.health,
    queryFn: api.health,
    refetchInterval: 6_000,
  });

  const healthy = healthQ.data?.status === 'ok';
  const llm = healthQ.data?.llm;
  const ingest = useIngestDialog();

  // Live wall-clock — null on first render (both server + client) so SSR HTML
  // matches the first client hydration. After mount, useEffect kicks the
  // interval and the placeholder swaps to the real time. This avoids the
  // classic Next.js "Text content does not match server-rendered HTML" error
  // that fires when a server-rendered timestamp differs from the client one
  // by even a single second.
  const [clock, setClock] = useState<string | null>(null);
  useEffect(() => {
    setClock(clockNow());
    const iv = setInterval(() => setClock(clockNow()), 1000);
    return () => clearInterval(iv);
  }, []);

  // P16 A-M9 — reactive to URL changes. The earlier mount-only
  // `useEffect` snapshotted `?debug=1` once at hydrate; client-side
  // route changes (Next.js client nav) wouldn't update the flag.
  // `useSearchParams()` re-runs on every URL change.
  const searchParams = useSearchParams();
  const debugMode = searchParams?.get('debug') === '1';

  return (
    <>
      <header className="flex h-16 items-center justify-between border-b border-slate-800 bg-slate-900/60 px-5 backdrop-blur">
        <div className="flex items-center gap-3">
          <span
            className={`inline-block h-2.5 w-2.5 rounded-full ${
              healthy
                ? 'bg-emerald-400 shadow-[0_0_8px_#34D399]'
                : healthQ.isError
                ? 'bg-rose-500'
                : 'bg-slate-500'
            }`}
            aria-label={healthy ? 'operational' : 'offline'}
          />
          <span className="text-[10px] font-bold uppercase tracking-[0.2em] text-emerald-300">
            {healthy ? 'LIVE' : healthQ.isError ? 'OFFLINE' : 'CONNECTING'}
          </span>
          {/* P16 A-M5 — icon is decorative; the adjacent LIVE/OFFLINE
              text already conveys the status to assistive tech. */}
          {healthy ? (
            <Wifi className="h-3.5 w-3.5 text-emerald-400" aria-hidden="true" />
          ) : (
            <WifiOff className="h-3.5 w-3.5 text-rose-500" aria-hidden="true" />
          )}

          <span className="ml-3 text-[11px] font-bold tracking-widest text-slate-100">
            {(item?.label ?? deriveFallbackLabel(pathname)).toUpperCase()}
          </span>

          {llm?.resolved && (
            <span
              className={`ml-2 rounded border px-2 py-0.5 text-[9px] font-bold uppercase tracking-wider ${
                llm.configured
                  ? 'border-emerald-700/60 bg-emerald-500/10 text-emerald-300'
                  : 'border-amber-700/60 bg-amber-500/10 text-amber-300'
              }`}
              title={`provider: ${llm.resolved} | model: ${llm.model ?? '?'} | key env: ${llm.key_env_var ?? '?'}`}
            >
              LLM: {llm.resolved}
              {llm.model ? ` / ${llm.model}` : ''}
            </span>
          )}
        </div>

        <div className="flex items-center gap-3">
          <span
            className="font-mono text-[11px] tabular-nums text-slate-300"
            suppressHydrationWarning
          >
            {clock ?? '--:--:-- UTC'}
          </span>

          <button
            onClick={ingest.open}
            title={
              llm?.configured
                ? `Ingest a raw market commentary | provider: ${llm.resolved} | model: ${llm.model ?? '?'}`
                : `LLM key missing (${llm?.key_env_var ?? '?'}). Click for setup instructions.`
            }
            className={`flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-[11px] font-bold tracking-wide transition ${
              llm?.configured
                ? 'border-emerald-700/60 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20'
                : 'border-amber-700/60 bg-amber-500/10 text-amber-300 hover:bg-amber-500/20'
            }`}
          >
            <Plus className="h-3 w-3" />
            INGEST
          </button>
        </div>
      </header>

      <CyclerRow />

      {healthQ.data && llm && !llm.configured && (
        <div className="border-b border-amber-700/60 bg-amber-500/10 px-6 py-1.5 text-[10px] text-amber-200">
          <span className="font-bold tracking-wider uppercase">
            LLM not configured.
          </span>{' '}
          {debugMode ? (
            <>
              Backend reports{' '}
              <code className="text-amber-100">{llm.key_env_var ?? '?'}</code> is
              missing (resolved provider:{' '}
              <code className="text-amber-100">{llm.resolved ?? '?'}</code>). Put it
              in <code className="text-amber-100">.env</code> at the project root,
              then restart the backend.
            </>
          ) : (
            <>Contact your administrator.</>
          )}
        </div>
      )}
    </>
  );
}

function clockNow(): string {
  const d = new Date();
  return [d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds()]
    .map((n) => String(n).padStart(2, '0'))
    .join(':') + ' UTC';
}

/**
 * P15 A-H11 — When the active route has no entry in nav.ts (e.g. /strategies/47),
 * fall back to a humanised version of the first path segment so the header
 * still names the page instead of defaulting to the global brand title.
 */
function deriveFallbackLabel(pathname: string): string {
  const first = (pathname || '/').split('/').filter(Boolean)[0];
  if (!first) return 'AGENTIC ALPHA';
  return first.replace(/[-_]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * P11-F2-01 / P12 A-H2 — CyclerRow: horizontal pill row underneath the header
 * showing each registered persona with a verb extracted from its most-recent
 * daemon-log line (e.g. "Quincy is researching…"). Hidden when no personas
 * are registered (parity with reference UI's idle state).
 *
 * P16 A-M7 — verb extraction moved to `lib/agentVerbs.ts` so the
 * cycler row and the AgentTeam rail share a single keyword table.
 */

function CyclerRow() {
  const agentsQ = useQuery({
    queryKey: queryKeys.agents,
    queryFn: api.agents,
    staleTime: 60_000,
  });
  const logQ = useQuery({
    queryKey: queryKeys.daemonLog(80),
    queryFn: () => api.daemonLog(80),
    refetchInterval: 4_000,
  });

  const agents = agentsQ.data?.agents ?? [];
  const events: DaemonLogEvent[] = logQ.data?.events ?? [];

  // P13 A-M1 — Render an empty-state pill instead of nothing so the row's
  // bottom border still anchors the layout (avoids a jarring vertical jump
  // when personas register asynchronously).
  if (agents.length === 0) {
    return (
      <div className="flex h-9 items-center gap-2 border-b border-slate-800 bg-slate-950/40 px-3 text-[9px] uppercase tracking-wider text-slate-600">
        <span className="inline-block h-1.5 w-1.5 rounded-full bg-slate-700" />
        No personas registered — start the daemon to populate the cycler row.
      </div>
    );
  }

  // Newest-first → first hit wins per persona key. Backend already orders
  // /api/daemon-log most-recent-first, but iterate defensively in case the
  // contract loosens later.
  const latest = new Map<string, string>();
  for (const ev of events) {
    const k = (ev.agent || '').trim();
    if (!k || latest.has(k)) continue;
    latest.set(k, ev.line || '');
  }

  return (
    <div className="flex h-9 items-center gap-1.5 overflow-x-auto border-b border-slate-800 bg-slate-950/40 px-3">
      {agents.map((agent) => {
        const line = latest.get(agent.key);
        const isActive = latest.has(agent.key);
        const verb = isActive ? verbOfActive(line || '') : '';
        return (
          <span
            key={agent.key}
            className="inline-flex max-w-[140px] shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 font-mono text-[9px] uppercase tracking-wider"
            style={{
              borderColor: isActive ? withAlpha(agent.color, '66') : '#1e293b',
              color: isActive ? agent.color : '#64748b',
              background: isActive ? withAlpha(agent.color, '11') : '#0f172a80',
            }}
            title={
              isActive
                ? `${agent.name} is ${verb}: ${line}`
                : `${agent.name} idle in recent log window`
            }
          >
            <span
              className="inline-block h-1.5 w-1.5 rounded-full"
              style={{
                background: isActive ? agent.color : '#475569',
                boxShadow: isActive ? `0 0 4px ${agent.color}` : 'none',
              }}
            />
            <span className="truncate">
              {agent.name}
              {isActive ? ` is ${verb}…` : ' · idle'}
            </span>
          </span>
        );
      })}
    </div>
  );
}
