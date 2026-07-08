'use client';

import { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import ReactMarkdown from 'react-markdown';
import { useIsMutating, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import clsx from 'clsx';
import { Sparkles, Plus, Trash2, Paperclip, FlaskConical, Loader2, CheckCircle2 } from 'lucide-react';
import {
  api,
  type ChatMessage,
  type SuggestedTopic,
} from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { markdownComponents, remarkMathPlugins, rehypeMathPlugins } from '@/lib/markdownComponents';
import ChatComposer from '@/components/alpha-lab/ChatComposer';

// P8-FIX/H-15 — chip color lookup. Backend hands us a status string; we map to
// our existing palette so the rail visually matches the reference UI.
// P11-F4-10 — Extended palette to cover the full Suggested Topic chip set the
// backend can emit (concept / goal-needed / decode / reach / idea / etc).
// Input is normalised (lowercased + underscores → hyphens) so e.g. "GOAL_NEEDED"
// and "goal-needed" both hit the same branch. Unknown chips fall through to a
// neutral slate ring rather than the old amber default which used to falsely
// imply "queued".
function chipColor(chip: string): string {
  const c = String(chip || '').toLowerCase().replace(/_/g, '-');
  switch (c) {
    case 'proven': return 'text-emerald-300 ring-emerald-700/40';
    case 'in-progress': return 'text-cyan-300 ring-cyan-700/40';
    case 'concept': return 'text-slate-300 ring-slate-600/60';
    case 'goal-needed': return 'text-amber-300 ring-amber-700/40';
    case 'decode': return 'text-purple-300 ring-purple-700/40';
    case 'reach': return 'text-sky-300 ring-sky-700/40';
    case 'idea': return 'text-fuchsia-300 ring-fuchsia-700/40';
    case 'queued': return 'text-amber-300 ring-amber-700/40';
    default: return 'text-slate-400 ring-slate-700/40';
  }
}

/**
 * /alpha-lab — conversational research workspace.
 *
 *  ┌─────────┬──────────────────────────────┬────────────────┐
 *  │ Sessions│ Active conversation          │ Suggested      │
 *  │  list   │                              │ topics         │
 *  ├─────────┴──────────────────────────────┴────────────────┤
 *  │ Composer (textarea + paperclip + send)                  │
 *  └─────────────────────────────────────────────────────────┘
 */
// P-WIKILINK — Obsidian-style [[concept]] support for Alpha Lab (matches the
// reference walkthrough's research notes). Chat messages render through
// markdown, so we rewrite [[X]] to a `#wiki:` link that markdownComponents
// paints as a concept chip. Suggested-topic titles are plain text, so WikiText
// splits them inline into the same chip styling.
function preprocessWikilinks(text: string): string {
  return String(text ?? '').replace(/\[\[([^\]\n]+)\]\]/g, (_m, c) => {
    const label = String(c).trim();
    return `[${label}](#wiki:${encodeURIComponent(label)})`;
  });
}

function WikiText({ text }: { text: string }): JSX.Element {
  const parts = String(text ?? '').split(/(\[\[[^\]\n]+\]\])/g);
  return (
    <>
      {parts.map((p, i) => {
        const m = /^\[\[([^\]\n]+)\]\]$/.exec(p);
        if (m) {
          return (
            <span
              key={i}
              className="rounded border border-cyan-800/50 bg-cyan-500/10 px-1 font-mono text-[10px] text-cyan-300"
            >
              {m[1].trim()}
            </span>
          );
        }
        return <span key={i}>{p}</span>;
      })}
    </>
  );
}

export default function AlphaLabPage() {
  const qc = useQueryClient();
  const router = useRouter();
  const [activeId, setActiveId] = useState<number | null>(null);
  const [extractError, setExtractError] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // P11-F4-12 — inline 2-step confirm for the Extract Alpha action. First click
  // arms the button (turns rose, label flips to "Click again to confirm" and a
  // 3s timer starts); second click within the window clears the timer and
  // fires the mutation. Replaces the modal `confirm()` dialog which broke
  // keyboard flow on macOS and looked out of place against the cyberpunk UI.
  const [extractArmed, setExtractArmed] = useState(false);
  const extractArmTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // C-L2 — armed-button pattern for Delete chat session. Holds the ID of
  // the session that's been armed for deletion. First click arms it (turns
  // rose, sets a 3s timer); second click within the window fires the
  // deletion. Replaces the native window.confirm() that broke keyboard
  // focus and clashed visually with the cyberpunk UI.
  const [deleteArmedId, setDeleteArmedId] = useState<number | null>(null);
  const deleteArmTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const sessionsQ = useQuery({
    queryKey: queryKeys.chatSessions,
    queryFn: () => api.chatSessions(),
    refetchInterval: 15_000,
  });
  const sessions = sessionsQ.data?.sessions ?? [];

  // P29-F2: depend on newest *id*, not the array (poll re-creates ref every 15s).
  const newestSessionId = sessions[0]?.id;
  useEffect(() => {
    if (activeId == null && newestSessionId != null) {
      setActiveId(newestSessionId);
    }
  }, [activeId, newestSessionId]);

  // P11-F4-12 — disarm the Extract button whenever the active session
  // changes (otherwise the user could arm on session A and accidentally
  // fire on session B after clicking through). Also clears any pending
  // arm timer.
  // F6-1 — also clear delete error on session switch so stale failures
  // from a previous session don't bleed into the newly selected one.
  useEffect(() => {
    setExtractArmed(false);
    setExtractError(null);
    setDeleteError(null);
    if (extractArmTimerRef.current) {
      clearTimeout(extractArmTimerRef.current);
      extractArmTimerRef.current = null;
    }
  }, [activeId]);

  // P11-F4-12 — unmount cleanup so the pending arm timer can't fire on
  // an unmounted component and call setState (React warning).
  useEffect(() => {
    return () => {
      if (extractArmTimerRef.current) {
        clearTimeout(extractArmTimerRef.current);
        extractArmTimerRef.current = null;
      }
      if (deleteArmTimerRef.current) {
        clearTimeout(deleteArmTimerRef.current);
        deleteArmTimerRef.current = null;
      }
    };
  }, []);

  // P30-F1: pause the 4s detail-poll while ANY chat-send is in flight.
  // Broad match — a NEW chat starts with sessionId=null (keyed -1) and
  // flips to the real id mid-flight via onSessionCreated; an exact-match
  // useIsMutating(['chat-send', <newId>]) would return 0 and race the
  // detailQ refetch against the invalidation in ChatComposer's onSuccess.
  const chatSendIsPending = useIsMutating({ mutationKey: ['chat-send'], exact: false }) > 0;
  const detailQ = useQuery({
    queryKey: queryKeys.chatSessionDetail(activeId ?? -1),
    queryFn: () => api.chatSessionDetail(activeId!),
    enabled: activeId != null,
    refetchInterval: () => (chatSendIsPending ? false : 4_000),
  });

  const healthQ = useQuery({
    queryKey: queryKeys.health,
    queryFn: api.health,
    staleTime: 60_000,
  });
  const supportsVision = !!healthQ.data?.llm?.supports_vision;

  // P-MODEL-SEL — Alpha Lab model picker. The backend allowlist always leads
  // with the configured default; extra choices come from the ALPHA_LAB_MODELS
  // env var. The pick is per-message (sent to /api/chat/send) and persisted to
  // localStorage so it survives reloads.
  const modelsQ = useQuery({
    queryKey: ['chat', 'models'],
    queryFn: api.chatModels,
    staleTime: 300_000,
  });
  const availableModels = modelsQ.data?.models ?? [];
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  // Reconcile the selection once the allowlist is known: keep a still-valid
  // current pick, else a still-offered persisted pick, else the server default.
  // Never leaves a model that isn't in the allowlist.
  useEffect(() => {
    if (availableModels.length === 0) return;
    setSelectedModel((prev) => {
      if (prev && availableModels.includes(prev)) return prev;
      const stored =
        typeof window !== 'undefined' ? window.localStorage.getItem('alphaLabModel') : null;
      if (stored && availableModels.includes(stored)) return stored;
      return modelsQ.data?.default ?? availableModels[0] ?? null;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelsQ.data]);
  const onSelectModel = (m: string) => {
    setSelectedModel(m);
    if (typeof window !== 'undefined') window.localStorage.setItem('alphaLabModel', m);
  };

  // P8-FIX/H-15 — Suggested Topics now served directly by the backend so the
  // ranking logic stays in one place. Returns `topics: SuggestedTopic[]` with
  // a pre-computed `chip` status string.
  const suggestedQ = useQuery({
    queryKey: queryKeys.alphaLabSuggested(6),
    queryFn: () => api.alphaLabSuggestedTopics(6),
    staleTime: 60_000,
  });
  const suggestedTopics: SuggestedTopic[] = suggestedQ.data?.topics ?? [];

  const deleteSession = useMutation({
    mutationFn: (id: number) => api.chatSessionDelete(id),
    onSuccess: (_data, deletedId) => {
      qc.invalidateQueries({ queryKey: queryKeys.chatSessions });
      setActiveId((cur) => cur === deletedId ? null : cur);
      // F6-1 — clear any previous delete error on successful deletion.
      setDeleteError(null);
    },
    // F6-1 — route delete failures to deleteError (not extractError) so the
    // sessions sidebar shows the correct contextual banner.
    onError: (e) => {
      setDeleteError(e instanceof Error ? e.message : String(e));
    },
  });

  const extract = useMutation({
    mutationFn: (id: number) => api.chatExtract(id),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: queryKeys.chatSessions });
      if (activeId) qc.invalidateQueries({ queryKey: queryKeys.chatSessionDetail(activeId) });
      qc.invalidateQueries({ queryKey: queryKeys.strategies });
      router.push(`/strategies/${data.strategy_id}`);
    },
    onError: (e) => {
      setExtractError(e instanceof Error ? e.message : String(e));
    },
  });

  const activeSession = detailQ.data?.session ?? null;
  const assistantMessageCount = (detailQ.data?.messages ?? []).filter(
    (m) => m.role === 'assistant',
  ).length;
  const alreadyExtracted = activeSession?.extracted_to_strategy_id ?? null;
  const canExtract = !!activeId && assistantMessageCount >= 1 && !alreadyExtracted && !extract.isPending;

  return (
    <div className="grid h-full w-full grid-cols-12 grid-rows-[1fr_auto] gap-3 overflow-hidden p-3">
      {/* P12/C-M5 — keyframes for the armed Extract Alpha button countdown
          rail. Declared at component scope (styled-jsx global) so the
          animation is available regardless of which button is rendering. */}
      <style jsx global>{`
        @keyframes p12-arm-shrink {
          from { transform: scaleX(1); }
          to { transform: scaleX(0); }
        }
      `}</style>
      {/* Sessions sidebar */}
      <aside className="col-span-3 row-span-1 flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
        <header className="flex items-center justify-between border-b border-slate-800 px-3 py-2">
          <span className="text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
            Sessions
          </span>
          <button
            onClick={() => setActiveId(null)}
            title="Start new chat"
            className="rounded border border-cyan-700/40 px-1.5 py-0.5 text-[9px] font-bold text-cyan-200 hover:bg-cyan-500/10"
          >
            <Plus className="h-3 w-3" />
          </button>
        </header>
        {/* F6-1 — delete error banner: shown inside the sessions sidebar so
            it is contextually associated with the delete action, not the
            extract action. Dismissed automatically on session switch or
            successful delete via the useEffect/onSuccess handlers above. */}
        {deleteError && (
          <div className="border-b border-rose-800/60 bg-rose-500/10 px-3 py-1.5 text-[10px] text-rose-300">
            Delete failed: {deleteError}
          </div>
        )}
        <div className="flex-1 overflow-y-auto p-1">
          {sessions.map((s) => (
            <button
              key={s.id}
              onClick={() => setActiveId(s.id)}
              className={clsx(
                'group block w-full rounded px-2 py-1.5 text-left text-[10px]',
                activeId === s.id
                  ? 'bg-cyan-500/10 text-cyan-200'
                  : 'text-slate-400 hover:bg-slate-900 hover:text-slate-200',
              )}
            >
              <div className="flex items-center justify-between gap-1">
                <span className="line-clamp-1">{s.title || `Chat ${s.id}`}</span>
                {/* C-L2 — armed-button pattern (mirrors the Extract Alpha button
                   above). First click arms (rose, 3s window); second click
                   fires deleteSession. Pre-deletion confirm() removed. */}
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    if (deleteArmedId === s.id) {
                      if (deleteArmTimerRef.current) {
                        clearTimeout(deleteArmTimerRef.current);
                        deleteArmTimerRef.current = null;
                      }
                      setDeleteArmedId(null);
                      deleteSession.mutate(s.id);
                      return;
                    }
                    if (deleteArmTimerRef.current) {
                      clearTimeout(deleteArmTimerRef.current);
                    }
                    setDeleteArmedId(s.id);
                    deleteArmTimerRef.current = setTimeout(() => {
                      setDeleteArmedId(null);
                      deleteArmTimerRef.current = null;
                    }, 3000);
                  }}
                  disabled={deleteSession.isPending}
                  title={deleteArmedId === s.id ? 'Click again within 3s to confirm delete' : 'Delete this conversation'}
                  className={clsx(
                    'transition-opacity',
                    deleteArmedId === s.id
                      ? 'opacity-100'
                      : 'opacity-0 group-hover:opacity-100',
                  )}
                >
                  <Trash2
                    className={clsx(
                      'h-2.5 w-2.5',
                      deleteArmedId === s.id
                        ? 'text-rose-200'
                        : 'text-rose-400 hover:text-rose-300',
                    )}
                  />
                </button>
              </div>
              <div className="text-[9px] text-slate-600">
                {s.message_count} msg · {s.last_msg_at?.slice(0, 16).replace('T', ' ')}
              </div>
            </button>
          ))}
          {sessions.length === 0 && (
            <div className="px-2 py-4 text-center text-[10px] text-slate-600">
              No conversations yet.
            </div>
          )}
        </div>
      </aside>

      {/* Conversation pane */}
      <main className="col-span-6 row-span-1 flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
        <header className="flex items-center justify-between border-b border-slate-800 px-4 py-2">
          <div className="flex items-center gap-2">
            <Sparkles className="h-3.5 w-3.5 text-cyan-400" />
            <span className="text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
              Alpha Lab
            </span>
          </div>
          <div className="flex items-center gap-3">
            {alreadyExtracted ? (
              <Link
                href={`/strategies/${alreadyExtracted}`}
                className="inline-flex items-center gap-1 rounded border border-emerald-700/60 bg-emerald-500/10 px-2 py-1 text-[10px] font-bold uppercase tracking-widest text-emerald-300 hover:bg-emerald-500/20"
              >
                <CheckCircle2 className="h-3 w-3" />
                Extracted → S#{alreadyExtracted}
              </Link>
            ) : (
              activeId != null && (
                <button
                  onClick={() => {
                    setExtractError(null);
                    if (!canExtract) return;
                    // P11-F4-12 — inline 2-step confirm. First click arms (3s
                    // window), second click within the window fires the
                    // mutation. Reading activeId into a local prevents the
                    // arm timer from racing a session switch.
                    if (!extractArmed) {
                      setExtractArmed(true);
                      if (extractArmTimerRef.current) {
                        clearTimeout(extractArmTimerRef.current);
                      }
                      extractArmTimerRef.current = setTimeout(() => {
                        setExtractArmed(false);
                        extractArmTimerRef.current = null;
                      }, 3000);
                      return;
                    }
                    if (extractArmTimerRef.current) {
                      clearTimeout(extractArmTimerRef.current);
                      extractArmTimerRef.current = null;
                    }
                    setExtractArmed(false);
                    extract.mutate(activeId);
                  }}
                  disabled={!canExtract}
                  title={
                    !canExtract
                      ? 'Send at least one message and wait for the assistant to reply first.'
                      : extractArmed
                      ? 'Click again within 3s to confirm extracting this conversation into a new alpha strategy.'
                      : 'Synthesize the chat into a structured alpha story and kick the pipeline.'
                  }
                  className={`relative overflow-hidden inline-flex items-center gap-1 rounded border px-2 py-1 text-[10px] font-bold uppercase tracking-widest transition disabled:cursor-not-allowed disabled:opacity-40 ${
                    extractArmed
                      ? 'border-rose-600 bg-rose-500/20 text-rose-100 hover:bg-rose-500/30'
                      : 'border-amber-700/60 bg-amber-500/10 text-amber-200 hover:bg-amber-500/20'
                  }`}
                >
                  {extract.isPending ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    <FlaskConical className="h-3 w-3" />
                  )}
                  {extract.isPending
                    ? 'Extracting…'
                    : extractArmed
                    ? 'Click again to confirm'
                    : 'Extract Alpha'}
                  {/* P12/C-M5 — visual countdown rail on the armed branch.
                      A 3-second linear scaleX from 1→0 mirrors the JS arm
                      timer above so the operator can see how long they have
                      left to confirm. `aria-hidden` because the live timer
                      isn't useful to a screen reader; the button label
                      already announces the armed state. */}
                  {extractArmed && (
                    <span
                      aria-hidden
                      className="pointer-events-none absolute inset-x-0 bottom-0 h-0.5 origin-left bg-rose-300"
                      style={{ animation: 'p12-arm-shrink 3s linear forwards' }}
                    />
                  )}
                </button>
              )
            )}
            {/* P-MODEL-SEL — model picker. With ≥2 allowlisted models this is a
                live <select> that sets the model for the NEXT message; with a
                single model (default config) it degrades to a read-only pill so
                the UI is unchanged until the operator sets ALPHA_LAB_MODELS. */}
            {availableModels.length > 1 ? (
              <label
                className="flex items-center gap-1"
                title="Model used for the next message. Saved locally."
              >
                <span className="font-mono text-[9px] uppercase tracking-wider text-slate-600">
                  model
                </span>
                <select
                  value={selectedModel ?? ''}
                  onChange={(e) => onSelectModel(e.target.value)}
                  className="max-w-[190px] cursor-pointer rounded border border-slate-800 bg-slate-950 px-1.5 py-0.5 font-mono text-[9px] text-slate-300 outline-none hover:border-slate-600 focus:border-cyan-700"
                >
                  {availableModels.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
                {supportsVision ? <span title="Configured model supports vision">👁</span> : null}
              </label>
            ) : (
              <div
                className="cursor-help rounded border border-slate-800 bg-slate-950 px-2 py-0.5 font-mono text-[9px] text-slate-500"
                title="Single configured model. Set ALPHA_LAB_MODELS (comma-separated provider model ids) to enable switching."
              >
                {selectedModel ?? healthQ.data?.llm?.model ?? '?'}
                {supportsVision ? ' · 👁' : ''}
              </div>
            )}
          </div>
        </header>
        {extractError && (
          <div className="border-b border-rose-800/60 bg-rose-500/10 px-4 py-2 text-[11px] text-rose-300">
            Extract failed: {extractError}
          </div>
        )}
        <div className="flex-1 overflow-y-auto">
          {activeId == null ? (
            <WelcomePane supportsVision={supportsVision} />
          ) : detailQ.isLoading ? (
            <div className="p-6 text-xs text-slate-500">Loading conversation…</div>
          ) : (
            <MessageList messages={detailQ.data?.messages ?? []} />
          )}
        </div>
      </main>

      {/* Suggested topics rail */}
      <aside className="col-span-3 row-span-1 flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
        <header className="border-b border-slate-800 px-3 py-2">
          <span className="text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
            Suggested Topics
          </span>
        </header>
        <div className="flex-1 space-y-1.5 overflow-y-auto p-2">
          {suggestedQ.isLoading && suggestedTopics.length === 0 && (
            <div className="px-2 py-3 text-center text-[10px] text-slate-600">
              Loading suggestions…
            </div>
          )}
          {!suggestedQ.isLoading && suggestedTopics.length === 0 && (
            <div className="px-2 py-3 text-center text-[10px] text-slate-600">
              No suggestions available.
            </div>
          )}
          {suggestedTopics.map((t) => (
            <div
              key={t.id}
              className="rounded-md border border-slate-800 bg-slate-950/50 p-2"
            >
              {/* P6-L11: prefix the topic line with the reference's
                  ``<<<status>>>:`` triple-bracket marker. Reuses the existing
                  chip palette so visual consistency is preserved without
                  painting two badges side by side. */}
              <span
                className={`mr-2 inline-block rounded px-1.5 py-0.5 font-mono text-[9px] font-bold uppercase tracking-wider ring-1 ring-inset ${chipColor(t.chip)}`}
              >
                &lt;&lt;&lt;{t.chip}&gt;&gt;&gt;:
              </span>
              <span className="text-[10px] leading-snug text-slate-300">
                <WikiText text={t.title} />
              </span>
            </div>
          ))}
        </div>
      </aside>

      {/* Composer spans full middle column */}
      <div className="col-span-12">
        <ChatComposer
          sessionId={activeId}
          supportsVision={supportsVision}
          model={selectedModel}
          onSessionCreated={(s) => setActiveId(s.id)}
        />
      </div>
    </div>
  );
}

function WelcomePane({ supportsVision }: { supportsVision: boolean }) {
  return (
    <div className="flex h-full flex-col items-center justify-center p-8 text-center">
      <Sparkles className="mb-3 h-10 w-10 text-cyan-400" />
      <h1 className="mb-2 text-lg font-bold text-slate-100">Welcome to Alpha Lab</h1>
      <p className="max-w-md text-[12px] leading-relaxed text-slate-400">
        {/* P12/C-M2 — copy demotion. The previous welcome line over-promised
            ("query Clickhouse data") on capabilities the chat tool surface
            doesn't actually expose to the user-facing path, so it was
            replaced with a grounded description of what Alpha Lab is for
            today. The vision-disabled caveat is preserved. */}
        Ask about alpha ideas, brainstorm factors, or sketch trading strategies.
        Attach chart screenshots when supported.
        {supportsVision
          ? null
          : ' (Image attachment disabled — current model does not support vision.)'}
      </p>
    </div>
  );
}

function MessageList({ messages }: { messages: ChatMessage[] }) {
  return (
    <div className="space-y-3 p-4">
      {messages.map((m) => (
        <MessageBubble key={m.id} msg={m} />
      ))}
    </div>
  );
}

function MessageBubble({ msg }: { msg: ChatMessage }) {
  const isUser = msg.role === 'user';
  return (
    <div className={clsx('flex', isUser ? 'justify-end' : 'justify-start')}>
      <div
        className={clsx(
          'max-w-[85%] rounded-lg border p-3 text-xs',
          isUser
            ? 'border-cyan-700/40 bg-cyan-500/10 text-cyan-100'
            : 'border-slate-700 bg-slate-950 text-slate-200',
        )}
      >
        <div className="mb-1 flex items-center gap-2 text-[9px] uppercase tracking-wider text-slate-500">
          {isUser ? 'You' : 'Alpha Lab'}
          <span className="text-slate-700">{msg.ts?.slice(11, 16)}</span>
        </div>
        {msg.image_paths.length > 0 && (
          <div className="mb-2 space-y-1">
            {msg.image_paths.map((p, i) => (
              <div key={i} className="flex items-center gap-1 rounded border border-cyan-700/40 bg-cyan-500/10 px-2 py-1 text-[10px] text-cyan-200">
                <Paperclip className="h-3 w-3" />
                <span className="font-mono">{p}</span>
              </div>
            ))}
          </div>
        )}
        {/* P11-F4-11 — tool-call attribution chips. Only rendered on assistant
            bubbles; gives operators a quick audit trail of which datasets the
            model touched while answering (rows + latency surfaced on hover). */}
        {!isUser && msg.tool_calls && msg.tool_calls.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1">
            {msg.tool_calls.map((tc, i) => (
              <span
                key={i}
                className="inline-flex items-center gap-1 rounded border border-purple-700/40 bg-purple-500/10 px-1.5 py-0.5 font-mono text-[9px] text-purple-200"
                title={tc.duration_ms != null ? `${tc.tool} → ${tc.target} · ${tc.duration_ms}ms` : `${tc.tool} → ${tc.target}`}
              >
                fetched
                {tc.rows != null ? ` ${tc.rows.toLocaleString()}` : ''} from
                {' '}
                <span className="text-purple-300">{tc.target}</span>
                {' '}
                <span className="text-purple-500">[{tc.tool}]</span>
              </span>
            ))}
          </div>
        )}
        <article className="prose prose-invert prose-sm max-w-none text-[12px] leading-relaxed text-inherit prose-code:text-emerald-300">
          {/* P19 F5 — apply shared markdownComponents so chat messages inherit
              the same safe img / external-link / code-block handling as KB
              Explorer and Sources. Prior to P19 this ReactMarkdown call
              omitted `components`, so `data:` images, `javascript:` URLs and
              fenced YAML blocks rendered un-sanitised / un-styled. */}
          <ReactMarkdown components={markdownComponents} remarkPlugins={remarkMathPlugins} rehypePlugins={rehypeMathPlugins}>{preprocessWikilinks(msg.content)}</ReactMarkdown>
        </article>
      </div>
    </div>
  );
}
