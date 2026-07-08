'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import clsx from 'clsx';
import { ClipboardCheck, ShieldCheck, AlertTriangle, Loader2 } from 'lucide-react';
import { api, cryptoUuid, type AlphaStrategy } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import GateCriteria from '@/components/backtest-panel/GateCriteria';
import { qualityLabel, qualityLabelClasses } from '@/lib/derive';

/**
 * /gate-review — manual promotion workflow (P5-FE-18).
 *   - Top: shared GateCriteria checklist (mirrored on /backtest-panel).
 *   - Middle: APPROVED strategies queue awaiting promotion to PAPER_TRADE.
 *   - Each row exposes "Promote → Paper Trade" using existing api.strategyPromote.
 */
export default function GateReviewPage() {
  const qc = useQueryClient();
  // P31-F8: per-strategy error map. The previous single actionError shared one
  // slot across every promote row, so a failure on S#5 was silently overwritten
  // as soon as the operator clicked Promote on S#7. Mirrors P30-F3's per-uid
  // error map in trading-terminal.
  const [promoteErrors, setPromoteErrors] = useState<Record<number, string>>({});
  // C-L3 — armed-button pattern, mirrors the alpha-lab Extract Alpha button.
  // First click on Promote arms the row (rose, 3s window); second click
  // fires promote.mutate. Replaces the native window.confirm() which
  // clashed with the cyberpunk UI and broke keyboard focus.
  const [promoteArmedId, setPromoteArmedId] = useState<number | null>(null);
  const promoteArmTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // P16 A-M2 parity — track *which* row is currently promoting so the
  // spinner + disabled state apply only to that button. The shared
  // promote.isPending previously froze every queued row during one click.
  const [promotingId, setPromotingId] = useState<number | null>(null);

  useEffect(() => {
    return () => {
      if (promoteArmTimerRef.current) {
        clearTimeout(promoteArmTimerRef.current);
        promoteArmTimerRef.current = null;
      }
    };
  }, []);

  const sq = useQuery({
    queryKey: queryKeys.strategies,
    queryFn: api.strategies,
    refetchInterval: 8_000,
  });

  const promote = useMutation({
    mutationFn: (s: AlphaStrategy) => {
      // D-H5/P13 — persist idempotency key in sessionStorage so a network
      // retry replays the same key rather than minting a fresh UUID and
      // double-transitioning the strategy. Mirrors live-trade deploy pattern.
      const storageKey = `promote-paper-${s.id}-key`;
      let key = sessionStorage.getItem(storageKey);
      if (!key) {
        key = cryptoUuid();
        sessionStorage.setItem(storageKey, key);
      }
      return api.strategyPromote(s.id, 'PAPER_TRADE', { idempotencyKey: key });
    },
    onMutate: (s: AlphaStrategy) => {
      setPromotingId(s.id);
      setPromoteErrors((prev) => {
        if (!(s.id in prev)) return prev;
        const { [s.id]: _drop, ...rest } = prev;
        return rest;
      });
    },
    onSettled: () => {
      setPromotingId(null);
    },
    onSuccess: (_data, s) => {
      sessionStorage.removeItem(`promote-paper-${s.id}-key`);
      setPromoteErrors((prev) => {
        if (!(s.id in prev)) return prev;
        const { [s.id]: _drop, ...rest } = prev;
        return rest;
      });
      qc.invalidateQueries({ queryKey: queryKeys.strategies });
      qc.invalidateQueries({ queryKey: queryKeys.pipelineBuckets });
      qc.invalidateQueries({ queryKey: queryKeys.pipelineBucketsV2 });
    },
    onError: (e, s) => {
      setPromoteErrors((prev) => ({
        ...prev,
        [s.id]: e instanceof Error ? e.message : String(e),
      }));
    },
  });

  const all: AlphaStrategy[] = sq.data?.strategies ?? [];
  const awaiting = all.filter((s) => (s.status || '').toUpperCase() === 'APPROVED');

  // C-L3 safety: if the armed strategy disappears from the awaiting list
  // (e.g. promoted by another operator during the 8 s poll window), cancel
  // the arm timer and reset armed state so a re-appearing row cannot be
  // one-click promoted without re-arming.
  useEffect(() => {
    if (promoteArmedId !== null && !awaiting.find((s) => s.id === promoteArmedId)) {
      if (promoteArmTimerRef.current) {
        clearTimeout(promoteArmTimerRef.current);
        promoteArmTimerRef.current = null;
      }
      setPromoteArmedId(null);
    }
  }, [awaiting, promoteArmedId]);

  return (
    <div className="flex h-full w-full flex-col gap-3 overflow-y-auto p-3">
      <header className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
        <div className="flex items-center gap-2">
          <ClipboardCheck className="h-4 w-4 text-cyan-400" />
          <h1 className="text-sm font-bold tracking-widest text-slate-100">
            GATE REVIEW
          </h1>
        </div>
        <p className="mt-1 text-[11px] text-slate-500">
          Manual review queue for strategies that have passed the critic and are
          awaiting promotion into Paper Trade. The gate-criteria checklist
          mirrors the panel on /backtest-panel.
        </p>
      </header>

      <section className="rounded-xl border border-slate-800 bg-slate-900/40">
        <GateCriteria />
      </section>

      <section className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
        <header className="flex items-center justify-between border-b border-slate-800 px-4 py-2">
          <h2 className="text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
            Awaiting promotion ({awaiting.length})
          </h2>
          <span className="text-[10px] text-slate-500">
            APPROVED → PAPER_TRADE
          </span>
        </header>
        {Object.entries(promoteErrors).length > 0 && (
          <div className="space-y-1 border-b border-rose-800/60 bg-rose-500/10 px-4 py-2 text-[11px] text-rose-300">
            {Object.entries(promoteErrors).map(([sid, msg]) => (
              <div key={sid}>
                <span className="font-mono">S#{sid}</span>: {msg}
              </div>
            ))}
          </div>
        )}
        {awaiting.length === 0 ? (
          <div className="p-8 text-center text-xs text-slate-500">
            <ShieldCheck className="mx-auto mb-2 h-6 w-6 text-slate-600" />
            No strategies awaiting promotion.
          </div>
        ) : (
          <ul className="divide-y divide-slate-800">
            {awaiting.map((s) => {
              const ql = qualityLabel(Number(s.metrics?.annualized_sharpe));
              return (
                <li key={s.id} className="flex items-center justify-between px-4 py-3">
                  <div className="flex min-w-0 items-center gap-2">
                    <Link href={`/strategies/${s.id}`} className="font-mono text-cyan-300 hover:underline">
                      S#{s.id}
                    </Link>
                    <span className="truncate text-[12px] text-slate-200">
                      {s.slug ?? s.name}
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className={`rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider ${qualityLabelClasses(ql.tone)}`}>
                      {ql.text}
                    </span>
                    <button
                      onClick={() => {
                        setPromoteErrors((prev) => {
                          if (!(s.id in prev)) return prev;
                          const { [s.id]: _drop, ...rest } = prev;
                          return rest;
                        });
                        // Armed-confirm second click must always fire, even when another
                        // promotion is in flight. The `disabled` attribute already prevents
                        // arming new rows while promotingId !== null, so we only need to
                        // guard the arming path below, not this confirm path.
                        if (promoteArmedId === s.id) {
                          if (promoteArmTimerRef.current) {
                            clearTimeout(promoteArmTimerRef.current);
                            promoteArmTimerRef.current = null;
                          }
                          setPromoteArmedId(null);
                          promote.mutate(s);
                          return;
                        }
                        if (promotingId !== null) return;
                        if (promoteArmTimerRef.current) {
                          clearTimeout(promoteArmTimerRef.current);
                        }
                        setPromoteArmedId(s.id);
                        promoteArmTimerRef.current = setTimeout(() => {
                          setPromoteArmedId(null);
                          promoteArmTimerRef.current = null;
                        }, 3000);
                      }}
                      disabled={promotingId !== null}
                      title={
                        promoteArmedId === s.id
                          ? `Click again within 3s to confirm promotion of S#${s.id} to PAPER_TRADE`
                          : `Promote S#${s.id} to PAPER_TRADE`
                      }
                      className={clsx(
                        'inline-flex items-center gap-1 rounded border px-2 py-1 text-[10px] font-bold uppercase tracking-wider transition disabled:cursor-not-allowed disabled:opacity-40',
                        promoteArmedId === s.id
                          ? 'border-rose-600 bg-rose-500/20 text-rose-100 hover:bg-rose-500/30'
                          : 'border-amber-700 bg-amber-500/15 text-amber-200 hover:bg-amber-500/25',
                      )}
                    >
                      {promotingId === s.id ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : (
                        <AlertTriangle className="h-3 w-3" />
                      )}
                      {promotingId === s.id
                        ? 'Promoting…'
                        : promoteArmedId === s.id
                          ? 'Click again to confirm'
                          : 'Promote → Paper'}
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}
