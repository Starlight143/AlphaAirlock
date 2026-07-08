'use client';

import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import {
  api,
  type AgentPersona,
  type DialogueIntent,
  type DialogueTurn,
} from '@/lib/api';
import { queryKeys } from '@/lib/query';

// P16 A-M10 — persona colours stored on the backend default to
// `#RRGGBB`, but a future configurable colour input could emit
// `"red"` or `"rgb(...)"` and would silently break any
// `style={{ color }}` consumer downstream. Validate and fall back to
// slate. (Sibling files HeaderBarV2.tsx and AgentTeam.tsx use a
// `withAlpha` variant for the `${color}NN` concatenation pattern.)
function validHex(hex: string): string {
  return /^#[0-9a-fA-F]{6}$/.test(hex) ? hex : '#94A3B8';
}

/**
 * F3 — read-only stream of the latest agent-to-agent transcript.
 *
 * Backs the "Show Dialogue" toggle on DaemonLog. Each row is rendered as:
 *   `[ts] from → to · intent · payload`
 * with the from/to agent names coloured according to the persona palette
 * already exposed by /api/agents.
 *
 * Persona colours are cached separately from the dialogue payload so a
 * single ring-buffer roundtrip doesn't repaint every persona swatch.
 */
const INTENT_COLORS: Record<DialogueIntent, string> = {
  question: '#22D3EE',
  answer: '#22C55E',
  handoff: '#A78BFA',
  critique: '#F472B6',
  approval: '#10B981',
  veto: '#EF4444',
  note: '#94A3B8',
};

const INTENT_ORDER: DialogueIntent[] = [
  'question',
  'answer',
  'handoff',
  'critique',
  'approval',
  'veto',
  'note',
];

export default function AgentDialogue() {
  const turnsQ = useQuery({
    queryKey: queryKeys.agentDialogue(undefined),
    queryFn: () => api.agentDialogue(undefined, 200),
    refetchInterval: 4_000,
  });

  // Persona palette — slow-poll because the personas list is effectively
  // static at runtime. Used only for from/to colourisation.
  const personasQ = useQuery({
    queryKey: queryKeys.agents,
    queryFn: api.agents,
    staleTime: 60_000,
  });

  const colorByAgent = useMemo(() => {
    const m = new Map<string, string>();
    for (const p of (personasQ.data?.agents ?? []) as AgentPersona[]) {
      // P16 A-M10 — guard against non-hex persona colours so the
      // downstream `style={{ color: ... }}` never receives invalid CSS.
      const safe = validHex(p.color);
      m.set(p.key, safe);
      // Also key by display name — payload `from_agent` sometimes uses the
      // display name (researcher → Researcher) depending on backend caller.
      m.set(p.name, safe);
    }
    return m;
  }, [personasQ.data]);

  const turns: DialogueTurn[] = turnsQ.data?.turns ?? [];

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex items-center justify-between border-b border-slate-800 bg-slate-950/40 px-3 py-1.5 text-[9px] uppercase tracking-wider">
        <div className="flex items-center gap-3 text-slate-500">
          <span>
            <span className="text-cyan-300">{turns.length}</span> turns
          </span>
          <span>
            buffer{' '}
            <span className="text-slate-300">
              {turnsQ.data?.buffer_size ?? '—'}
            </span>
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-[8.5px]">
          {INTENT_ORDER.map((k) => (
            <IntentSwatch key={k} intent={k} />
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-2 font-mono text-[10px] leading-relaxed">
        {turns.length === 0 ? (
          <div className="flex h-full items-center justify-center text-slate-600">
            {turnsQ.isLoading
              ? 'Connecting to agent dialogue buffer…'
              : 'No dialogue turns yet — click INGEST in the header to kick off a pipeline run.'}
          </div>
        ) : (
          turns.map((t) => (
            // P31-F1: (strategy_id, turn, from_agent, to_agent) is unique
            // per dialogue row — the array-index suffix shifted every key
            // on each 2s poll, defeating DOM reuse (same anti-pattern P30-F12
            // fixed in DaemonLog).
            // F14-3: append ts to guard against turn-counter reset after a
            // daemon restart, where the ring-buffer reuses low sequence numbers
            // producing duplicate (strategy_id, turn, from_agent, to_agent)
            // tuples within the same 200-entry snapshot window.
            <DialogueRow
              key={`${t.strategy_id ?? 'sys'}-${t.turn}-${t.from_agent}-${t.to_agent}-${t.ts}`}
              turn={t}
              colorByAgent={colorByAgent}
            />
          ))
        )}
      </div>
    </div>
  );
}

function DialogueRow({
  turn,
  colorByAgent,
}: {
  turn: DialogueTurn;
  colorByAgent: Map<string, string>;
}) {
  const fromColor = colorByAgent.get(turn.from_agent) ?? '#94A3B8';
  const toColor = colorByAgent.get(turn.to_agent) ?? '#94A3B8';
  const intentColor =
    INTENT_COLORS[turn.intent as DialogueIntent] ?? '#94A3B8';
  const ts = formatTs(turn.ts);
  return (
    <div className="group flex items-baseline gap-2 border-b border-slate-900/60 py-0.5">
      <span className="w-16 shrink-0 text-slate-600">{ts}</span>
      {/* P16 A-M11 — strategy_id may legitimately be null for system-wide
          dialogue entries; render an em-dash instead of "S#null". */}
      <span className="w-10 shrink-0 text-slate-600">
        {turn.strategy_id != null ? `S#${turn.strategy_id}` : '—'}
      </span>
      <span
        className="shrink-0 font-bold"
        style={{ color: fromColor }}
        title={turn.from_agent}
      >
        {turn.from_agent}
      </span>
      <span className="shrink-0 text-slate-600">→</span>
      <span
        className="shrink-0 font-bold"
        style={{ color: toColor }}
        title={turn.to_agent}
      >
        {turn.to_agent}
      </span>
      <span className="shrink-0 text-slate-700">·</span>
      <span
        className={clsx('shrink-0 uppercase tracking-wider')}
        style={{ color: intentColor }}
      >
        {turn.intent}
      </span>
      <span className="shrink-0 text-slate-700">·</span>
      <span className="flex-1 truncate text-slate-300 group-hover:whitespace-normal">
        {turn.payload}
      </span>
    </div>
  );
}

function IntentSwatch({ intent }: { intent: DialogueIntent }) {
  return (
    <span className="flex items-center gap-1 uppercase tracking-wider text-slate-500">
      <span
        className="inline-block h-1.5 w-1.5 rounded-full"
        style={{ background: INTENT_COLORS[intent] }}
      />
      {intent}
    </span>
  );
}

function formatTs(iso: string): string {
  // ts may be ISO or a unix-string. Pure-best-effort: render HH:MM:SS in the
  // viewer's local time zone, falling back to the raw last-8 substring.
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(-8);
  const h = String(d.getHours()).padStart(2, '0');
  const m = String(d.getMinutes()).padStart(2, '0');
  const s = String(d.getSeconds()).padStart(2, '0');
  return `${h}:${m}:${s}`;
}
