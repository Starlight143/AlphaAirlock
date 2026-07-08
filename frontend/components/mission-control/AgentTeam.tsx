'use client';

import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { UserCog } from 'lucide-react';
import { api, type AgentPersona, type DaemonLogEvent } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { lastVerbForAgent } from '@/lib/agentVerbs';

// P16 A-M10 — defensive hex+alpha concatenation. The earlier inline
// `${agent.color}55` assumed a 6-digit hex string; an upstream change
// that ever emitted `"red"`, `"rgb(...)"`, or `"#abc"` would produce
// malformed CSS and silently drop the colour. Validate the hex and
// fall back to slate. Helper is kept inline (rather than shared via
// a tiny lib module) per planner spec.
function withAlpha(hex: string, alpha: string): string {
  if (/^#[0-9a-fA-F]{6}$/.test(hex)) return hex + alpha;
  return '#475569' + alpha;
}

/**
 * P13 A-H1 — AGENT TEAM rail flattened to a single flex-wrap row of cards,
 * matching reference screenshots (19-42-03 / 19-50-24). The earlier P12
 * Team A / Team B / Ops 3-column partition is dropped because the reference
 * UI shows a continuous wrap of personas with no team-grouping pills above
 * the cards. The `team` field on AgentPersona remains for other consumers.
 */
export default function AgentTeam() {
  const agentsQ = useQuery({
    queryKey: queryKeys.agents,
    queryFn: api.agents,
    // Personas almost never change at runtime — keep them warm but slow-poll.
    staleTime: 60_000,
  });

  // Daemon log is the source of truth for "what is each persona doing right
  // now". Polling it here keeps the active/queue chips fresh independently
  // of the DaemonLog component below.
  const logQ = useQuery({
    queryKey: queryKeys.daemonLog(80),
    queryFn: () => api.daemonLog(80),
    refetchInterval: 4_000,
  });

  const agents = agentsQ.data?.agents ?? [];
  const events: DaemonLogEvent[] = logQ.data?.events ?? [];

  const activeCounts = useMemo(() => {
    const m = new Map<string, number>();
    for (const ev of events) {
      const k = (ev.agent || '').trim();
      if (!k) continue;
      m.set(k, (m.get(k) ?? 0) + 1);
    }
    return m;
  }, [events]);

  return (
    <section className="flex h-full flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/50">
      <header className="flex items-center justify-between border-b border-slate-800 px-4 py-2">
        <h2 className="text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
          Agent Team
        </h2>
        <span className="rounded border border-slate-800 px-2 py-0.5 font-mono text-[9px] font-bold tracking-wider text-cyan-200">
          [{agents.length} {agents.length === 1 ? 'PERSONA' : 'PERSONAS'}]
        </span>
      </header>

      {/* P16 A-M4 — switch from flex-wrap (which clipped the 2nd row on
          a 260px-tall track) to a single horizontal nowrap row that
          scrolls horizontally when the personas overflow. Matches
          reference screenshots showing all 7 personas inline. */}
      <div className="flex flex-1 flex-nowrap content-start gap-2 overflow-x-auto overflow-y-hidden p-3">
        {agentsQ.isLoading ? (
          <div className="flex w-full items-center justify-center text-xs text-slate-500">
            Loading agents…
          </div>
        ) : agents.length === 0 ? (
          <div className="flex w-full flex-col items-center justify-center gap-1 text-xs text-slate-500">
            <span>No personas registered.</span>
            <span className="text-[10px] text-slate-600">
              Start the research daemon, then click <span className="text-emerald-300">INGEST</span> in the header to seed the first pipeline run.
            </span>
          </div>
        ) : (
          agents.map((agent) => (
            <AgentCard
              key={agent.key}
              agent={agent}
              active={activeCounts.get(agent.key) ?? 0}
              lastIdleLabel={lastVerbForAgent(events, agent.key)}
            />
          ))
        )}
      </div>
    </section>
  );
}

function AgentCard({ agent, active, lastIdleLabel }: { agent: AgentPersona; active: number; lastIdleLabel: string }) {
  return (
    <div className="relative flex w-[210px] shrink-0 flex-col rounded-lg border border-slate-800 bg-slate-950/70 p-2">
      {/* Active / queue chip — top-right. */}
      <span
        className="absolute right-1.5 top-1.5 rounded border px-1 py-0.5 font-mono text-[8.5px] leading-none"
        style={{
          borderColor: active > 0 ? withAlpha(agent.color, '55') : '#1e293b',
          color: active > 0 ? agent.color : '#475569',
          background: active > 0 ? withAlpha(agent.color, '11') : '#0f172a',
        }}
        title={
          active > 0
            ? `${active} recent daemon events for ${agent.name}`
            : `${agent.name} is idle in the recent log window`
        }
      >
        {active > 0 ? `active ${active}` : lastIdleLabel}
      </span>

      <div className="mb-2 flex items-center gap-2 pr-12">
        <div
          className="flex h-7 w-7 items-center justify-center rounded-full"
          style={{
            background: withAlpha(agent.color, '22'),
            boxShadow: `inset 0 0 0 1px ${withAlpha(agent.color, '55')}`,
          }}
        >
          <UserCog className="h-3.5 w-3.5" style={{ color: agent.color }} />
        </div>
        <div className="min-w-0 leading-tight">
          <div className="truncate text-[11px] font-bold text-slate-100">
            {agent.name}
          </div>
          <div className="truncate text-[9px] uppercase tracking-wider text-slate-500">
            {agent.role}
          </div>
        </div>
      </div>

      <p className="mb-1.5 line-clamp-3 min-h-[42px] text-[10px] leading-snug text-slate-400">
        {agent.description}
      </p>

      <div className="mt-auto flex flex-wrap gap-1">
        {agent.capabilities.slice(0, 3).map((cap) => (
          <span
            key={cap}
            className="rounded border border-slate-800 bg-slate-900 px-1.5 py-0.5 text-[8px] uppercase tracking-wider"
            style={{ color: agent.color }}
          >
            {cap}
          </span>
        ))}
        {agent.capabilities.length > 3 && (
          <span
            className="rounded border border-slate-800 bg-slate-900 px-1.5 py-0.5 text-[8px] uppercase tracking-wider text-slate-500"
            title={agent.capabilities.slice(3, 11).join(', ') + (agent.capabilities.length > 11 ? `, …+${agent.capabilities.length - 11} more` : '')}
          >
            +{agent.capabilities.length - 3}
          </span>
        )}
      </div>
    </div>
  );
}
