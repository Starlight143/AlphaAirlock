'use client';

import { useEffect, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Radio,
  Play,
  Pause,
  RefreshCw,
  Trash2,
  AlertCircle,
  Clock,
  CheckCircle2,
} from 'lucide-react';
import { api, cryptoUuid, type IngestSource } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { categoryLabel, sourceTypeLabel } from '@/lib/sourceTypes';

type Props = {
  source: IngestSource;
  /** P6-A2: called when the user clicks the Open button. Parent owns drawer state. */
  onOpen?: (source: IngestSource) => void;
};

const TYPE_COLOR: Record<string, string> = {
  rss: '#22D3EE',
  patreon: '#F97316',
  medium: '#22C55E',
  substack: '#A855F7',
  reddit: '#EF4444',
  twitter_tag: '#3B82F6',
  twitter_article: '#3B82F6',
  youtube_video: '#EF4444',
  tiktok: '#D946EF',
  arxiv: '#8B5CF6',
  // P12-B-M3 — glassnode shares the orange family with patreon (subscription
  // paywalled research) but in a lighter shade to keep them visually distinct.
  glassnode: '#FB923C',
  manual: '#64748B',
};

export default function SourceCard({ source, onOpen }: Props) {
  const qc = useQueryClient();

  const [cardError, setCardError] = useState<string | null>(null);
  // Armed-button pattern for the destructive Delete action — mirrors the
  // alpha-lab delete-session button. First click arms (rose, 3s window);
  // second click within the window fires remove.mutate(). Replaces the
  // native window.confirm() that broke keyboard focus and clashed with the
  // cyberpunk UI (and silently no-ops in embeds where confirm is suppressed).
  const [deleteArmed, setDeleteArmed] = useState(false);
  const deleteArmTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // R6/FE-24 — optimistic guard against the toggle double-write window. After a
  // successful toggle, `source.enabled` (a prop) stays stale until the parent's
  // ~6s poll refetch lands; without this guard the button is clickable in that
  // gap and a second click re-reads the stale value, either double-writing the
  // same direction or silently dropping an intended toggle-back. `pendingEnabled`
  // holds the value we toggled TO; the toggle stays disabled until the prop
  // catches up (or the mutation errors). NOTE: do NOT use `toggle.isSuccess` for
  // this — TanStack v5 never auto-resets it, which would deadlock the button.
  const [pendingEnabled, setPendingEnabled] = useState<boolean | null>(null);
  useEffect(() => {
    return () => {
      if (deleteArmTimerRef.current) {
        clearTimeout(deleteArmTimerRef.current);
        deleteArmTimerRef.current = null;
      }
    };
  }, []);
  // Clear the optimistic toggle guard once the refetched prop reflects the new
  // state, re-enabling the button for the next intentional toggle.
  useEffect(() => {
    if (pendingEnabled !== null && source.enabled === pendingEnabled) {
      setPendingEnabled(null);
    }
  }, [source.enabled, pendingEnabled]);
  // P34b — reuse a per-action Idempotency-Key across retries (mirrors the
  // sessionStorage pattern in paper-trade/live-trade pages): a flaky-network
  // retry or rapid double-click replays the cached response instead of minting
  // a fresh UUID. Cleared onSuccess so the next intentional click is its own op.
  const idemKey = (kind: string): string => {
    const sk = `source-${kind}-${source.id}-key`;
    let k = sessionStorage.getItem(sk);
    if (!k) { k = cryptoUuid(); sessionStorage.setItem(sk, k); }
    return k;
  };
  const clearKey = (kind: string): void => {
    sessionStorage.removeItem(`source-${kind}-${source.id}-key`);
  };
  const toggle = useMutation({
    onMutate: () => { setPendingEnabled(!source.enabled); },
    mutationFn: () => {
      const direction = !source.enabled ? 'enable' : 'disable';
      return api.sourceUpdate(source.id, { enabled: !source.enabled }, { idempotencyKey: idemKey(`toggle-${direction}`) });
    },
    onSuccess: () => { clearKey('toggle-enable'); clearKey('toggle-disable'); setCardError(null); qc.invalidateQueries({ queryKey: queryKeys.sources }); },
    onError: (e) => { setPendingEnabled(null); setCardError(e instanceof Error ? e.message : String(e)); },
  });
  const pollNow = useMutation({
    mutationFn: () => api.sourcePollNow(source.id, { idempotencyKey: idemKey('poll') }),
    onSuccess: () => { clearKey('poll'); setCardError(null); qc.invalidateQueries({ queryKey: queryKeys.sources }); },
    onError: (e) => setCardError(e instanceof Error ? e.message : String(e)),
  });
  const remove = useMutation({
    mutationFn: () => api.sourceDelete(source.id, { idempotencyKey: idemKey('delete') }),
    onSuccess: () => { clearKey('delete'); setCardError(null); qc.invalidateQueries({ queryKey: queryKeys.sources }); },
    onError: (e) => setCardError(e instanceof Error ? e.message : String(e)),
  });

  const accent = TYPE_COLOR[source.source_type] ?? '#64748B';
  const inCircuitBreaker = source.disabled_until
    ? new Date(source.disabled_until).getTime() > Date.now()
    : false;

  return (
    <div
      className="flex flex-col gap-2 rounded-lg border border-slate-800 bg-slate-950/60 p-3"
      style={{ boxShadow: `inset 0 0 0 1px ${accent}22` }}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          <Radio className="h-3.5 w-3.5" style={{ color: accent }} />
          <div className="leading-tight">
            <div className="text-xs font-bold text-slate-100">{source.name}</div>
            <div className="text-[9px] uppercase tracking-wider text-slate-500">
              {sourceTypeLabel(source.source_type)}
            </div>
          </div>
        </div>
        <div className="flex flex-col items-end gap-1">
          <span
            className={`rounded px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider ring-1 ring-inset ${
              source.enabled
                ? 'bg-emerald-500/10 text-emerald-300 ring-emerald-700/40'
                : 'bg-slate-800 text-slate-500 ring-slate-700/40'
            }`}
          >
            {source.enabled ? 'ACTIVE' : 'PAUSED'}
          </span>
          {source.is_stub && (
            <span
              title="This source type's fetcher is a stub — no content will be ingested."
              className="rounded border border-amber-700/40 bg-amber-500/10 px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider text-amber-300"
            >
              STUB
            </span>
          )}
        </div>
      </div>

      {(source.category || typeof source.events_24h === 'number') && (
        <div className="flex flex-wrap items-center gap-1.5 text-[9px] uppercase tracking-wider">
          {source.category && (
            <span
              className="rounded border border-slate-700 bg-slate-900 px-1.5 py-0.5 text-slate-300"
              title="Categorical tab assignment"
            >
              {categoryLabel(source.category)}
            </span>
          )}
          {typeof source.events_24h === 'number' && (
            <span
              className={`rounded border px-1.5 py-0.5 ${
                source.events_24h > 0
                  ? 'border-cyan-700/40 bg-cyan-500/10 text-cyan-300'
                  : 'border-slate-800 bg-slate-950 text-slate-600'
              }`}
              title="Events ingested in the last 24h"
            >
              {source.events_24h} ev/24h
            </span>
          )}
        </div>
      )}

      <div className="line-clamp-1 break-all font-mono text-[10px] text-slate-500">
        {source.url}
      </div>

      <div className="flex items-center gap-3 text-[9px] text-slate-500">
        <span className="flex items-center gap-1">
          <Clock className="h-3 w-3" />
          every {source.cadence_minutes}m
        </span>
        {source.last_success_at ? (
          <span className="flex items-center gap-1 text-emerald-300">
            <CheckCircle2 className="h-3 w-3" />
            ok {timeAgo(source.last_success_at)}
          </span>
        ) : source.last_polled_at ? (
          <span className="flex items-center gap-1 text-slate-500">
            polled {timeAgo(source.last_polled_at)}
          </span>
        ) : (
          <span className="text-slate-600">never polled</span>
        )}
        {source.consecutive_failures > 0 && (
          <span
            className="flex cursor-help items-center gap-1 text-rose-300"
            title={
              source.last_error_message
                ? source.last_error_message
                : `${source.consecutive_failures} consecutive failure(s) — no error message captured`
            }
          >
            <AlertCircle className="h-3 w-3" />
            {source.consecutive_failures} fails
          </span>
        )}
      </div>

      {inCircuitBreaker && source.disabled_until && (
        <div className="rounded border border-amber-700/60 bg-amber-500/5 px-2 py-1 text-[10px] text-amber-300">
          Circuit-breaker open until {fmtAbs(source.disabled_until)}
        </div>
      )}

      {source.last_error_message && (
        <div className="line-clamp-2 rounded border border-rose-700/60 bg-rose-500/5 px-2 py-1 text-[10px] text-rose-300">
          {source.last_error_message}
        </div>
      )}

      {/* P34: render the captured mutation error — toggle/poll/delete failures
          were set into cardError but never shown to the operator. */}
      {cardError && (
        <div className="rounded border border-rose-700/60 bg-rose-500/10 px-2 py-1 text-[10px] text-rose-300">
          {cardError}
        </div>
      )}

      <div className="mt-auto flex items-center gap-1.5 pt-1">
        <button
          onClick={() => toggle.mutate()}
          disabled={toggle.isPending || pollNow.isPending || pendingEnabled !== null}
          title={source.enabled ? 'Pause polling' : 'Resume polling'}
          aria-label={source.enabled ? 'Pause polling' : 'Resume polling'}
          className="rounded border border-slate-700 px-2 py-1 text-[10px] text-slate-300 hover:bg-slate-800"
        >
          {source.enabled ? (
            <Pause className="h-3 w-3" />
          ) : (
            <Play className="h-3 w-3" />
          )}
        </button>
        <button
          onClick={() => pollNow.mutate()}
          disabled={pollNow.isPending || toggle.isPending}
          title="Poll once now"
          aria-label="Poll once now"
          className="rounded border border-cyan-700/40 bg-cyan-500/10 px-2 py-1 text-[10px] text-cyan-300 hover:bg-cyan-500/20"
        >
          <RefreshCw
            className={`h-3 w-3 ${pollNow.isPending ? 'animate-spin' : ''}`}
          />
        </button>
        {onOpen && (
          <button
            onClick={() => onOpen(source)}
            title="Open file drawer"
            className="rounded border border-purple-700/40 bg-purple-500/10 px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-purple-200 hover:bg-purple-500/20"
          >
            Open
          </button>
        )}
        <button
          onClick={() => {
            if (remove.isPending) return;
            if (deleteArmed) {
              if (deleteArmTimerRef.current) {
                clearTimeout(deleteArmTimerRef.current);
                deleteArmTimerRef.current = null;
              }
              setDeleteArmed(false);
              remove.mutate();
              return;
            }
            if (deleteArmTimerRef.current) {
              clearTimeout(deleteArmTimerRef.current);
            }
            setDeleteArmed(true);
            deleteArmTimerRef.current = setTimeout(() => {
              setDeleteArmed(false);
              deleteArmTimerRef.current = null;
            }, 3000);
          }}
          disabled={remove.isPending}
          title={
            deleteArmed
              ? `Click again within 3s to permanently delete "${source.name}" and all its events`
              : `Delete source "${source.name}" and all its events`
          }
          aria-label={deleteArmed ? 'Confirm delete source' : 'Delete source'}
          className={`ml-auto inline-flex items-center gap-1 rounded border px-2 py-1 text-[10px] transition disabled:cursor-not-allowed disabled:opacity-40 ${
            deleteArmed
              ? 'border-rose-600 bg-rose-500/20 text-rose-100 hover:bg-rose-500/30'
              : 'border-rose-700/40 text-rose-300 hover:bg-rose-500/10'
          }`}
        >
          <Trash2 className="h-3 w-3" />
          {deleteArmed ? 'Confirm?' : null}
        </button>
      </div>
    </div>
  );
}

function timeAgo(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

function fmtAbs(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
  } catch {
    return iso;
  }
}
