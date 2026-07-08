'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Target,
  AlertTriangle,
  RefreshCcw,
  Power,
  Rocket,
  ShieldCheck,
  Loader2,
} from 'lucide-react';
import { ResponsiveContainer, RadialBarChart, RadialBar, PolarAngleAxis } from 'recharts';

import { api, cryptoUuid, type AlphaStrategy, type LiveTradeDashboard, type PaperTradeRun } from '@/lib/api';
import { queryKeys } from '@/lib/query';

// P17/C-M8 — single source of truth for promotion-queue Sharpe.
// Priority: paper-trade run (the live-tracking sharpe) → dashboard's
// pre-resolved latest_sharpe (mirrors server-side paper→backtest fallback in
// live_trade_ops.py:227-228) → strategy's static backtest metrics → 0.
function resolveSharpe(
  s: AlphaStrategy,
  run: PaperTradeRun | undefined,
  dashSharpe: number | undefined,
): number {
  const runN = Number(run?.metrics?.annualized_sharpe ?? run?.metrics?.sharpe);
  if (Number.isFinite(runN)) return runN;
  const dashN = Number(dashSharpe);
  if (Number.isFinite(dashN)) return dashN;
  const metricN = Number(s.metrics?.annualized_sharpe);
  if (Number.isFinite(metricN)) return metricN;
  return 0;
}

export default function LiveTradePage() {
  const qc = useQueryClient();
  const dashQ = useQuery({
    queryKey: queryKeys.liveTradeDashboard,
    queryFn: api.liveTradeDashboard,
    refetchInterval: 5_000,
  });
  // P8-FIX/H-16 — promotion queue uses the master strategy list + paper-trade
  // runs (Sharpe chip), independently from the live dashboard payload.
  // NOTE: queryFn MUST be raw `api.strategies` so all consumers see the same
  //       `{ strategies: [...] }` shape — see arena/page.tsx for rationale.
  const stratsQ = useQuery({
    queryKey: queryKeys.strategies,
    queryFn: api.strategies,
    refetchInterval: 15_000,
  });
  const runsQ = useQuery({
    queryKey: queryKeys.paperTradeList,
    queryFn: api.paperTradeList,
    refetchInterval: 15_000,
  });

  const [showPauseModal, setShowPauseModal] = useState(false);
  const [pauseBannerDismissed, setPauseBannerDismissed] = useState(false);
  const [resumeErrors, setResumeErrors] = useState<Record<number, string>>({});
  const [resumeSucceeded, setResumeSucceeded] = useState<Set<number>>(new Set());
  // D-M15 — hold the button disabled for 5s after success so a triggered-but-
  // happy operator can't double-fire a fresh pause-all immediately.
  const [recentSuccess, setRecentSuccess] = useState(false);
  // D-M14/P16 — track the success-cooldown timeout so a page unmount mid-
  // cooldown clears the timer rather than firing a setState on an unmounted
  // component (React strict-mode noise + memory leak in long-lived dashboards).
  const recentSuccessTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // F3-3 — per-strategy resume-success chip timers. A Map keyed by strategy id
  // mirrors the pauseMut pattern (recentSuccessTimerRef above) for the
  // resumeMut path: each timer handle is stored so it can be cancelled on
  // unmount, preventing stale setState calls when the operator navigates away
  // within the 4-second chip display window.
  const resumeSuccessTimerRef = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());
  useEffect(() => {
    return () => {
      if (recentSuccessTimerRef.current != null) {
        clearTimeout(recentSuccessTimerRef.current);
        recentSuccessTimerRef.current = null;
      }
    };
  }, []);
  useEffect(() => {
    // F3-3 — clear all per-strategy resume-success timers on unmount.
    const timers = resumeSuccessTimerRef.current;
    return () => { timers.forEach(clearTimeout); };
  }, []);
  const pauseMut = useMutation({
    mutationFn: () => {
      // D-L7/P16 — only persist the key when it was freshly minted. The prior
      // unconditional setItem rewrote the existing key with itself on every
      // click, churning storage events for no benefit.
      let key = sessionStorage.getItem('pause-all-key');
      if (!key) {
        key = cryptoUuid();
        sessionStorage.setItem('pause-all-key', key);
      }
      return api.liveTradePauseAll(key);
    },
    onSuccess: () => {
      sessionStorage.removeItem('pause-all-key');
      setPauseBannerDismissed(false); // re-show banner after a fresh pause.
      setRecentSuccess(true);
      if (recentSuccessTimerRef.current != null) {
        clearTimeout(recentSuccessTimerRef.current);
      }
      recentSuccessTimerRef.current = setTimeout(() => {
        setRecentSuccess(false);
        recentSuccessTimerRef.current = null;
      }, 5000);
      qc.invalidateQueries({ queryKey: queryKeys.liveTradeDashboard });
      qc.invalidateQueries({ queryKey: queryKeys.strategies });
    },
  });
  const resumeMut = useMutation({
    mutationFn: (id: number) => {
      // P15/C-H7 — per-strategy idempotency key surviving page reload (e.g.
      // user mashes Resume, then refreshes mid-request: the next click reuses
      // the same key so the server short-circuits the replay rather than
      // double-resuming the strategy). Mirrors pause-all's pattern but scoped
      // per strategy id so resuming strategy A doesn't reuse strategy B's key.
      const storageKey = `resume-${id}-key`;
      let key = sessionStorage.getItem(storageKey);
      if (!key) {
        key = cryptoUuid();
        sessionStorage.setItem(storageKey, key);
      }
      return api.liveTradeResume(id, { idempotencyKey: key });
    },
    onMutate: (id: number) => {
      setResumeErrors((prev) => {
        if (!(id in prev)) return prev;
        const { [id]: _drop, ...rest } = prev;
        return rest;
      });
    },
    onSuccess: (_data, id) => {
      sessionStorage.removeItem(`resume-${id}-key`);
      setResumeSucceeded((prev) => { const next = new Set(prev); next.add(id); return next; });
      // F3-3 — cancel any in-flight timer for the same id before scheduling a
      // new one (guards against rapid double-resume of the same strategy).
      const existing = resumeSuccessTimerRef.current.get(id);
      if (existing != null) clearTimeout(existing);
      const t = setTimeout(() => {
        setResumeSucceeded((prev) => { const next = new Set(prev); next.delete(id); return next; });
        resumeSuccessTimerRef.current.delete(id);
      }, 4000);
      resumeSuccessTimerRef.current.set(id, t);
      qc.invalidateQueries({ queryKey: queryKeys.liveTradeDashboard });
    },
    onError: (e, id) => {
      setResumeErrors((prev) => ({
        ...prev,
        [id]: e instanceof Error ? e.message : String(e),
      }));
    },
  });

  // Deploy → LIVE confirm + 5s timer modal target.
  const [deployTargetId, setDeployTargetId] = useState<number | null>(null);
  const deployMut = useMutation({
    // D-H5/P16 — persist per-strategy idempotency key so a retry after a
    // network glitch (typed-confirm + 5s countdown is the highest-risk
    // operator action in the app) replays the same key rather than minting
    // a fresh UUID per click. Mirrors resumeMut's pattern.
    mutationFn: (id: number) => {
      const storageKey = `deploy-${id}-key`;
      let key = sessionStorage.getItem(storageKey);
      if (!key) {
        key = cryptoUuid();
        sessionStorage.setItem(storageKey, key);
      }
      return api.strategyPromote(id, 'LIVE', { idempotencyKey: key });
    },
    onSuccess: (_data, id) => {
      sessionStorage.removeItem(`deploy-${id}-key`);
      qc.invalidateQueries({ queryKey: queryKeys.strategies });
      qc.invalidateQueries({ queryKey: queryKeys.liveTradeDashboard });
      qc.invalidateQueries({ queryKey: queryKeys.pipelineBucketsV2 });
      setDeployTargetId(null);
    },
  });
  const deployGateMessage = deployMut.error
    ? extractDeployGate(deployMut.error as Error)
    : null;

  const data = dashQ.data;

  // R5/UX-01 — explicit loading / error states for the primary dashboard fetch,
  // so the operator sees a spinner or an actionable error instead of silently
  // blank tables and panels during the initial load or on backend failure.
  if (dashQ.isLoading && !data) {
    return (
      <div className="flex h-full w-full items-center justify-center gap-2 text-[12px] text-slate-400">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading live trade dashboard…
      </div>
    );
  }

  if (dashQ.isError && !data) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-3 text-center">
        <AlertTriangle className="h-6 w-6 text-rose-400" />
        <div className="text-[12px] font-bold uppercase tracking-wider text-rose-300">
          Dashboard unreachable
        </div>
        <div className="max-w-xs text-[11px] text-slate-400">
          {dashQ.error instanceof Error ? dashQ.error.message : 'Failed to fetch live trade data.'}
        </div>
        <button
          onClick={() => dashQ.refetch()}
          className="inline-flex items-center gap-1 rounded-md border border-slate-700 px-3 py-1.5 text-[11px] text-slate-300 hover:bg-slate-800"
        >
          <RefreshCcw className="h-3 w-3" /> Retry
        </button>
      </div>
    );
  }

  const strategies = stratsQ.data?.strategies ?? [];
  const runsById = new Map<number, PaperTradeRun>();
  for (const r of runsQ.data?.runs ?? []) runsById.set(r.strategy_id, r);
  const dashSharpeBySid = new Map<number, number>();
  for (const s of data?.strategies ?? []) dashSharpeBySid.set(s.sid, s.latest_sharpe);

  const queueRows: PromotionQueueRow[] = strategies
    .filter((s) => s.status === 'SMALL_CAPITAL')
    .map((s) => {
      const run = runsById.get(s.id);
      return {
        id: s.id,
        slug: s.slug ?? s.name,
        status: s.status,
        sharpe: resolveSharpe(s, run, dashSharpeBySid.get(s.id)),
        isHealthy: run ? run.is_healthy : null,
      };
    });

  const showPostPauseBanner =
    !pauseBannerDismissed && pauseMut.isSuccess && (pauseMut.data?.paused_count ?? 0) > 0;

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      {showPostPauseBanner && (
        <div role="status" aria-live="assertive" className="sticky top-0 z-30 flex items-center justify-between border-b border-rose-700 bg-rose-600 px-3 py-2 text-[11px] font-bold uppercase tracking-widest text-white">
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-3.5 w-3.5" />
            ALL PAUSED — drift no longer driving trades ({pauseMut.data?.paused_count}{' '}
            strategies{pauseMut.data?.idempotent_replay ? ' · replay' : ''})
          </div>
          <button
            onClick={() => setPauseBannerDismissed(true)}
            className="rounded border border-white/40 px-2 py-0.5 text-[10px] hover:bg-white/10"
          >
            Dismiss
          </button>
        </div>
      )}

      <div className="flex flex-1 flex-col gap-3 overflow-y-auto p-3">
        {/*
          P17/C-M10 — auto-tick disabled warning. When ALPHA_PAPER_TICK_ENABLED=0
          the periodic paper-trade refresh is off, so live-tracking Sharpe (and
          therefore the promotion-queue chip) is frozen at whatever value the
          last manual paper-run produced. Surface this prominently so the
          operator doesn't promote on stale data.

          `paper_tick_enabled` is optional on the type (older API responses
          predate this field) so the explicit `=== false` keeps "unknown" silent
          rather than warning on every page load against a stale backend.
        */}
        {data?.paper_tick_enabled === false && (
          <div className="flex items-center gap-2 rounded-md border border-amber-700/60 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            <span>
              Auto paper-tick is <span className="font-mono uppercase">OFF</span> —
              live-tracking Sharpe is frozen. Set{' '}
              <code className="rounded bg-amber-900/40 px-1 font-mono text-amber-100">
                ALPHA_PAPER_TICK_ENABLED=1
              </code>{' '}
              in <code className="rounded bg-amber-900/40 px-1 font-mono text-amber-100">.env</code>{' '}
              and restart the backend to resume scheduled paper-trade refresh.
            </span>
          </div>
        )}
        <Banner data={data} onRefresh={() => qc.invalidateQueries({ queryKey: queryKeys.liveTradeDashboard })} />

        <div className="grid grid-cols-12 gap-3">
          <main className="col-span-8 flex flex-col gap-3">
            <Card title="Active Positions">
              <PositionsTable rows={data?.positions ?? []} baseCcy={data?.base_ccy ?? 'USDT'} />
            </Card>
            <Card title="Promotion Queue · SMALL_CAPITAL → LIVE">
              <PromotionQueue
                rows={queueRows}
                loading={stratsQ.isLoading || runsQ.isLoading}
                pendingId={deployMut.isPending ? deployMut.variables ?? null : null}
                onDeploy={(id) => { deployMut.reset(); setDeployTargetId(id); }}
              />
              {deployMut.isError && deployGateMessage && (
                <div className="mt-2 rounded-md border border-rose-700/60 bg-rose-500/10 px-2 py-1.5 text-[10px] text-rose-300">
                  Gate {deployGateMessage.gate}: {deployGateMessage.detail}
                </div>
              )}
              {deployMut.isError && !deployGateMessage && (
                <div className="mt-2 rounded-md border border-rose-700/60 bg-rose-500/10 px-2 py-1.5 text-[10px] text-rose-300">
                  {(deployMut.error as Error).message}
                </div>
              )}
            </Card>
            <Card title="Recent Fills">
              <FillsList rows={data?.recent_fills ?? []} baseCcy={data?.base_ccy ?? 'USDT'} />
            </Card>
            <Card title="Strategy Health">
              <StrategyHealth rows={data?.strategies ?? []} onResume={(id) => resumeMut.mutate(id)} resumePending={resumeMut.isPending} resumePendingId={resumeMut.isPending ? (resumeMut.variables ?? null) : null} resumeErrors={resumeErrors} resumeSucceeded={resumeSucceeded} />
            </Card>
          </main>

          <aside className="col-span-4 flex flex-col gap-3">
            <PnlGrid pnl={data?.pnl} baseCcy={data?.base_ccy ?? 'USDT'} />
            <Card title="Risk Gauges">
              <RiskGauges risk={data?.risk} baseCcy={data?.base_ccy ?? 'USDT'} />
            </Card>
            <Card title="Emergency Stop">
              {/* P29-F6: disable only when literally nothing to pause. */}
              <button
                onClick={() => setShowPauseModal(true)}
                disabled={
                  !data ||
                  (data.positions.length === 0 &&
                    !data.strategies.some(
                      (s) => s.status === 'LIVE' || s.status === 'SMALL_CAPITAL',
                    )) ||
                  pauseMut.isPending ||
                  recentSuccess
                }
                title={
                  !data
                    ? 'Loading…'
                    : data.positions.length === 0 &&
                      !data.strategies.some(
                        (s) => s.status === 'LIVE' || s.status === 'SMALL_CAPITAL',
                      )
                      ? 'No live or small-capital strategies armed — nothing to pause.'
                      : pauseMut.isPending
                        ? 'Pausing in progress…'
                        : recentSuccess
                          ? 'Just paused — wait 5s before re-firing.'
                          : 'Flip every LIVE / SMALL_CAPITAL strategy to PAUSED.'
                }
                className="flex w-full items-center justify-center gap-2 rounded-md border border-rose-700 bg-rose-500/10 px-3 py-2 text-[11px] font-bold uppercase tracking-wider text-rose-200 hover:bg-rose-500/20 disabled:opacity-40"
              >
                <Power className="h-4 w-4" /> PAUSE ALL LIVE
              </button>
              {recentSuccess && (
                <div className="mt-2 text-[10px] text-emerald-300">
                  Paused {pauseMut.data?.paused_count} strategies{pauseMut.data?.idempotent_replay ? ' (replay)' : ''}
                </div>
              )}
              {pauseMut.isError && (
                <div className="mt-2 text-[10px] text-rose-300">
                  {(pauseMut.error as Error).message}
                </div>
              )}
            </Card>
          </aside>
        </div>

        {showPauseModal && (
          <PauseConfirmModal
            n={data?.strategies.filter((s) => s.status === 'LIVE' || s.status === 'SMALL_CAPITAL').length ?? 0}
            onCancel={() => setShowPauseModal(false)}
            onConfirm={() => {
              setShowPauseModal(false);
              pauseMut.mutate();
            }}
          />
        )}

        {deployTargetId != null && (
          <DeployConfirmModal
            key={deployTargetId}
            row={queueRows.find((r) => r.id === deployTargetId) ?? null}
            onCancel={() => {
              if (deployMut.isPending) return;
              setDeployTargetId(null);
              deployMut.reset();
            }}
            onConfirm={() => deployMut.mutate(deployTargetId)}
            pending={deployMut.isPending}
            error={deployGateMessage ? `Gate ${deployGateMessage.gate}: ${deployGateMessage.detail}` : deployMut.isError ? (deployMut.error as Error).message : null}
          />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Promotion queue
// ---------------------------------------------------------------------------

type PromotionQueueRow = {
  id: number;
  slug: string;
  status: string;
  sharpe: number;
  isHealthy: boolean | null;
};

function PromotionQueue({
  rows,
  loading,
  pendingId,
  onDeploy,
}: {
  rows: PromotionQueueRow[];
  loading: boolean;
  pendingId: number | null;
  onDeploy: (id: number) => void;
}) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 py-4 text-[11px] text-slate-500">
        <Loader2 className="h-3 w-3 animate-spin" /> Loading promotion candidates…
      </div>
    );
  }
  if (rows.length === 0) {
    return <Empty>No SMALL_CAPITAL strategies pending promotion.</Empty>;
  }
  return (
    <ul className="space-y-1 text-[11px]">
      {rows.map((row) => {
        const healthy = row.isHealthy === true;
        return (
          <li
            key={row.id}
            className="flex flex-wrap items-center justify-between gap-2 rounded border border-slate-900 bg-slate-950/40 px-3 py-2"
          >
            <div className="flex min-w-0 items-center gap-2">
              <Link
                href={`/strategies/${row.id}`}
                className="font-mono text-cyan-300 hover:underline"
              >
                S#{row.id}
              </Link>
              <span className="truncate text-slate-200">{row.slug}</span>
            </div>
            <div className="flex items-center gap-2 text-[10px]">
              <span className="rounded border border-slate-700 bg-slate-900 px-1.5 py-0.5 font-mono uppercase tracking-wider text-slate-300">
                {row.status}
              </span>
              <span
                className={`rounded border px-1.5 py-0.5 font-mono ${
                  row.sharpe >= 1.5
                    ? 'border-emerald-700/60 bg-emerald-500/10 text-emerald-300'
                    : row.sharpe >= 1.0
                    ? 'border-cyan-700/60 bg-cyan-500/10 text-cyan-300'
                    : 'border-amber-700/60 bg-amber-500/10 text-amber-300'
                }`}
              >
                Sh {Number.isFinite(row.sharpe) ? row.sharpe.toFixed(2) : '—'}
              </span>
              {row.isHealthy === false && (
                <span className="rounded border border-rose-700/60 bg-rose-500/10 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-rose-300">
                  unhealthy
                </span>
              )}
              {row.isHealthy === null && (
                <span className="rounded border border-slate-700/60 bg-slate-500/10 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-slate-400">
                  <Loader2 className="mr-0.5 inline h-2.5 w-2.5 animate-spin" />
                  awaiting paper run
                </span>
              )}
              <button
                onClick={() => onDeploy(row.id)}
                disabled={pendingId === row.id || !healthy}
                title={
                  healthy
                    ? 'Promote to LIVE'
                    : row.isHealthy === null
                      ? 'Loading paper-trade health — please wait'
                      : 'Healthy paper-trade run required'
                }
                className="inline-flex items-center gap-1 rounded border border-emerald-700 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-emerald-200 hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {pendingId === row.id ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <Rocket className="h-3 w-3" />
                )}
                Deploy → LIVE
              </button>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function DeployConfirmModal({
  row,
  onCancel,
  onConfirm,
  pending,
  error,
}: {
  row: PromotionQueueRow | null;
  onCancel: () => void;
  onConfirm: () => void;
  pending: boolean;
  error?: string | null;
}) {
  const [typed, setTyped] = useState('');
  const [counter, setCounter] = useState(5);
  const okPhrase = 'DEPLOY LIVE';
  useTimer(counter, setCounter);
  // P15/C-M16 — Escape cancels unless a deploy is in flight. Preserves the
  // existing pending guard pattern (cancel button is also disabled when pending).
  useEscapeKey(true, () => { if (!pending) onCancel(); });
  const ready = typed === okPhrase && counter <= 0 && !pending;
  if (!row) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70"
      onClick={onCancel}
    >
      <div
        className="w-full max-w-md rounded-2xl border border-emerald-700 bg-slate-900 p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="deploy-confirm-title"
      >
        <h2 id="deploy-confirm-title" className="flex items-center gap-2 text-sm font-bold uppercase tracking-widest text-emerald-300">
          <ShieldCheck className="h-4 w-4" /> Confirm LIVE Promotion
        </h2>
        <p className="mt-3 text-[12px] text-slate-300">
          Promote{' '}
          <span className="font-mono text-cyan-300">S#{row.id}</span> ·{' '}
          <span className="font-mono">{row.slug}</span> from{' '}
          <code className="rounded bg-slate-800 px-1 text-amber-300">SMALL_CAPITAL</code>{' '}
          to <code className="rounded bg-slate-800 px-1 text-emerald-300">LIVE</code>.
          Live orders will route to the exchange on the next tick.
        </p>
        {error && (
          <div className="mt-3 rounded-md border border-rose-700/60 bg-rose-500/10 px-2 py-1.5 text-[10px] text-rose-300">
            {error}
          </div>
        )}
        <input
          autoFocus
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder={`Type "${okPhrase}" to enable`}
          className="mt-3 w-full rounded border border-slate-700 bg-slate-950 px-3 py-1.5 text-[11px] text-slate-200"
        />
        <div className="mt-3 flex items-center justify-between">
          <span className="text-[10px] text-slate-500">
            {counter > 0 ? `Wait ${counter}s` : 'Ready'}
          </span>
          <div className="flex gap-2">
            <button
              onClick={onCancel}
              disabled={pending}
              className="rounded-md border border-slate-700 px-3 py-1 text-[11px] text-slate-300 hover:bg-slate-800 disabled:opacity-40"
            >
              Cancel
            </button>
            <button
              onClick={onConfirm}
              disabled={!ready}
              className="inline-flex items-center gap-1 rounded-md border border-emerald-700 bg-emerald-500/30 px-3 py-1 text-[11px] font-bold text-emerald-100 hover:bg-emerald-500/40 disabled:opacity-40"
            >
              {pending ? <Loader2 className="h-3 w-3 animate-spin" /> : <Rocket className="h-3 w-3" />}
              Deploy LIVE
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Error-shape parsers
// ---------------------------------------------------------------------------

type DeployGate = { gate: string; detail: string };

/**
 * Best-effort parser for the FastAPI `detail` field on a 412 Precondition
 * Failed (gate failure). The `api` json helper throws an Error whose message
 * looks like `HTTP 412 Precondition Failed: {"detail":{"gate":"sharpe","reason":"..."}}`.
 * We extract the JSON suffix and pull `gate` / `reason` (or `detail`) out.
 */
function extractDeployGate(err: Error): DeployGate | null {
  try {
    const idx = err.message.indexOf('{');
    if (idx < 0) return null;
    const payload = JSON.parse(err.message.slice(idx));
    const detail = payload?.detail;
    if (!detail || typeof detail !== 'object') return null;
    const gate = String(detail.gate ?? 'unknown');
    const reason = String(detail.reason ?? detail.detail ?? detail.message ?? 'failed');
    return { gate, detail: reason };
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Pre-existing pieces (carried over from prior revision, unchanged behaviour)
// ---------------------------------------------------------------------------

function Banner({ data, onRefresh }: { data?: LiveTradeDashboard; onRefresh: () => void }) {
  const mode = data?.mode ?? 'paper';
  const status = data?.exchange_status?.status ?? 'paper_mode';
  const cls = mode === 'live' ? 'bg-emerald-500/20 text-emerald-300' : 'bg-cyan-500/20 text-cyan-200';
  return (
    <header className="flex items-center justify-between rounded-xl border border-slate-800 bg-slate-900/40 px-4 py-2">
      <div className="flex items-center gap-3">
        <Target className="h-4 w-4 text-cyan-400" />
        <h1 className="text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">Live Trade Console</h1>
        <span className={`rounded px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider ${cls}`}>{mode}</span>
        <span className="text-[10px] text-slate-500">· {status}{data?.exchange_status?.venue ? ` (${data.exchange_status.venue})` : ''}</span>
        {data?.exchange_status?.note && <span className="text-[10px] text-amber-400">{data.exchange_status.note}</span>}
      </div>
      <div className="flex items-center gap-2 text-[10px] text-slate-500">
        <span>generated {data?.generated_at ? new Date(data.generated_at).toLocaleTimeString() : '—'}</span>
        <button onClick={onRefresh} className="inline-flex items-center gap-1 rounded-md border border-slate-700 px-2 py-1 text-slate-400 hover:bg-slate-800">
          <RefreshCcw className="h-3 w-3" /> Refresh
        </button>
      </div>
    </header>
  );
}

function PositionsTable({ rows, baseCcy }: { rows: LiveTradeDashboard['positions']; baseCcy: string }) {
  if (!rows.length) return <Empty>No live positions. Strategies need status SMALL_CAPITAL or LIVE.</Empty>;
  return (
    <table className="w-full text-[11px]">
      <thead className="text-[9px] uppercase tracking-wider text-slate-500">
        <tr>
          <th scope="col" className="text-left">Symbol</th><th scope="col" className="text-left">Side</th>
          <th scope="col" className="text-right">Qty</th><th scope="col" className="text-right">Entry</th>
          <th scope="col" className="text-right">Last</th><th scope="col" className="text-right">uPnL</th>
          <th scope="col" className="text-right">Held</th>
        </tr>
      </thead>
      <tbody className="font-mono">
        {rows.map((p) => (
          <tr key={p.strategy_id} className="border-t border-slate-900">
            <td className="py-1"><Link href={`/strategies/${p.strategy_id}`} className="text-cyan-300 hover:underline">S#{p.strategy_id}</Link></td>
            <td className={p.side === 'long' ? 'text-emerald-300' : 'text-rose-300'}>{p.side.toUpperCase()}</td>
            <td className="text-right">{Number.isFinite(p.qty) ? p.qty.toFixed(4) : '—'}</td>
            <td className="text-right">{Number.isFinite(p.entry_price) ? p.entry_price.toFixed(2) : '—'}</td>
            <td className="text-right">{Number.isFinite(p.last_price) ? p.last_price.toFixed(2) : '—'}</td>
            <td className={`text-right ${!Number.isFinite(p.unrealized_pnl) ? 'text-slate-400' : p.unrealized_pnl >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
              {Number.isFinite(p.unrealized_pnl) ? `${p.unrealized_pnl >= 0 ? '+' : ''}${p.unrealized_pnl.toFixed(2)} ${baseCcy}` : '—'}
            </td>
            <td className="text-right text-slate-400">{p.holding_hours != null ? `${p.holding_hours}h` : '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function FillsList({ rows, baseCcy }: { rows: LiveTradeDashboard['recent_fills']; baseCcy: string }) {
  if (!rows.length) return <Empty>No recent fills.</Empty>;
  return (
    <ul className="space-y-1 text-[11px] font-mono">
      {/* P31-F2: composite key — index keys break React DOM reuse when the
          fills list grows (new fill at top shifts every existing row's key
          and forces remount). */}
      {rows.slice(0, 30).map((f) => (
        <li key={`${f.ts ?? 'nots'}-${f.strategy_id}-${f.side}-${f.qty_delta}-${f.price}`} className="flex items-center justify-between rounded border border-slate-900 bg-slate-950/40 px-2 py-1">
          <span className="text-[10px] text-slate-500">{f.ts ? new Date(f.ts).toLocaleTimeString() : '—'}</span>
          <span className="text-cyan-300">S#{f.strategy_id}</span>
          <span className={f.side === 'buy' ? 'text-emerald-300' : 'text-rose-300'}>{f.side.toUpperCase()}</span>
          <span className="text-slate-300">{Number.isFinite(f.qty_delta) ? f.qty_delta.toFixed(4) : '—'} @ {Number.isFinite(f.price) ? f.price.toFixed(2) : '—'}</span>
          <span className="text-[10px] text-slate-500">{baseCcy}</span>
        </li>
      ))}
    </ul>
  );
}

function PnlGrid({ pnl, baseCcy }: { pnl?: LiveTradeDashboard['pnl']; baseCcy: string }) {
  if (!pnl) return null;
  const tiles = [
    { label: 'Today Total', value: pnl.today_total, cls: !Number.isFinite(pnl.today_total) ? 'text-slate-400' : pnl.today_total >= 0 ? 'text-emerald-300' : 'text-rose-300' },
    { label: 'Unrealized', value: pnl.today_unrealized, cls: !Number.isFinite(pnl.today_unrealized) ? 'text-slate-400' : pnl.today_unrealized >= 0 ? 'text-emerald-300' : 'text-rose-300' },
    { label: 'Realized', value: pnl.today_realized, cls: !Number.isFinite(pnl.today_realized) ? 'text-slate-400' : pnl.today_realized >= 0 ? 'text-emerald-300' : 'text-rose-300' },
    { label: 'All Time', value: pnl.all_time_total, cls: !Number.isFinite(pnl.all_time_total) ? 'text-slate-400' : pnl.all_time_total >= 0 ? 'text-emerald-300' : 'text-rose-300' },
  ];
  return (
    <div className="grid grid-cols-2 gap-2">
      {tiles.map((t) => (
        <div key={t.label} className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
          <div className="text-[9px] uppercase tracking-widest text-slate-500">{t.label}</div>
          <div className={`mt-1 font-mono text-lg ${t.cls}`}>
            {Number.isFinite(t.value) ? `${t.value >= 0 ? '+' : ''}${t.value.toFixed(2)}` : '—'}
          </div>
          <div className="text-[9px] text-slate-500">{baseCcy}</div>
        </div>
      ))}
    </div>
  );
}

function RiskGauges({ risk, baseCcy }: { risk?: LiveTradeDashboard['risk']; baseCcy?: string }) {
  if (!risk) return null;
  // NOTE: despite the _pct suffix, the backend computes dd_pct = (cur/peak)-1.0 which is a
  // decimal fraction (e.g. -0.052 for a 5.2% drawdown). The * 100 conversion here is correct.
  // Do NOT remove the * 100 based on the field name; the backend does not multiply by 100.
  const ddRaw = Number(risk.drawdown_from_peak_pct);
  const dd = Number.isFinite(ddRaw) ? Math.min(100, Math.abs(ddRaw * 100)) : 0;
  const ddDomainMax = Math.max(20, Math.ceil(dd * 1.1));
  const data = [{ name: 'dd', value: dd, fill: dd > 8 ? '#f43f5e' : dd > 4 ? '#fbbf24' : '#22d3ee' }];
  return (
    <div>
      <div className="text-[10px] text-slate-500">Drawdown from peak</div>
      <ResponsiveContainer width="100%" height={140}>
        <RadialBarChart innerRadius="60%" outerRadius="100%" data={data} startAngle={90} endAngle={-270}>
          <PolarAngleAxis type="number" domain={[0, ddDomainMax]} tick={false} />
          <RadialBar background dataKey="value" cornerRadius={4} />
        </RadialBarChart>
      </ResponsiveContainer>
      <div className="text-center font-mono text-lg text-rose-300">{dd.toFixed(2)}%</div>
      <div className="mt-2 grid grid-cols-2 gap-2 text-[10px] text-slate-400">
        <div>Exposure: <span className="font-mono text-slate-200">{Number.isFinite(risk.total_exposure_ccy) ? risk.total_exposure_ccy.toFixed(0) : '—'} {baseCcy}</span></div>
        <div>Peak: <span className="font-mono text-slate-200">{Number.isFinite(risk.peak_equity) ? risk.peak_equity.toFixed(2) : '—'} {baseCcy}</span></div>
      </div>
    </div>
  );
}

function StrategyHealth({ rows, onResume, resumePending, resumePendingId, resumeErrors, resumeSucceeded }: { rows: LiveTradeDashboard['strategies']; onResume: (id: number) => void; resumePending: boolean; resumePendingId?: number | null; resumeErrors?: Record<number, string>; resumeSucceeded?: Set<number> }) {
  // Armed-button state: first click arms the button for a specific strategy id,
  // second click within 3 s fires onResume. Auto-resets after 3 s.
  const [resumeArmedId, setResumeArmedId] = useState<number | null>(null);
  const resumeArmTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Clear the arm timer on unmount to avoid stale setState after navigation.
  useEffect(() => {
    return () => {
      if (resumeArmTimerRef.current != null) {
        clearTimeout(resumeArmTimerRef.current);
        resumeArmTimerRef.current = null;
      }
    };
  }, []);

  function handleResumeClick(sid: number) {
    if (resumeArmedId === sid) {
      // Second click within the arm window — fire the mutation and disarm.
      if (resumeArmTimerRef.current != null) {
        clearTimeout(resumeArmTimerRef.current);
        resumeArmTimerRef.current = null;
      }
      setResumeArmedId(null);
      onResume(sid);
    } else {
      // First click — arm the button and schedule auto-disarm after 3 s.
      if (resumeArmTimerRef.current != null) {
        clearTimeout(resumeArmTimerRef.current);
      }
      setResumeArmedId(sid);
      resumeArmTimerRef.current = setTimeout(() => {
        setResumeArmedId(null);
        resumeArmTimerRef.current = null;
      }, 3000);
    }
  }

  if (!rows.length) return <Empty>No strategies deployed.</Empty>;
  return (
    <ul className="space-y-1 text-[11px]">
      {rows.map((s) => (
        <li key={s.sid} className="flex items-center justify-between rounded border border-slate-900 bg-slate-950/40 px-3 py-2">
          <Link href={`/strategies/${s.sid}`} className="flex-1 truncate text-slate-200 hover:text-cyan-300">{s.slug}</Link>
          <div className="ml-2 flex items-center gap-2 text-[10px]">
            <span className={`rounded px-1.5 py-0.5 font-mono ${s.status === 'PAUSED' ? 'bg-amber-500/20 text-amber-200' : 'bg-slate-800 text-slate-300'}`}>{s.status}</span>
            <span className="font-mono text-cyan-300">Sh {Number.isFinite(s.latest_sharpe) ? s.latest_sharpe.toFixed(2) : '—'}</span>
            {Number.isFinite(s.ic_drift) && s.ic_drift !== 0 && <span className={`font-mono ${s.ic_drift < 0 ? 'text-rose-300' : 'text-emerald-300'}`}>Δ{s.ic_drift.toFixed(2)}</span>}
            {s.status === 'PAUSED' && (
              <button
                onClick={() => handleResumeClick(s.sid)}
                disabled={resumePendingId != null ? resumePendingId === s.sid : resumePending}
                title={resumeArmedId === s.sid ? 'Click again within 3 s to confirm resume' : 'Resume live trading for this strategy'}
                className={`rounded border px-2 py-0.5 disabled:opacity-40 ${
                  resumeArmedId === s.sid
                    ? 'border-amber-500 bg-amber-500/20 text-amber-200 hover:bg-amber-500/30'
                    : 'border-emerald-700 text-emerald-200 hover:bg-emerald-500/10'
                }`}
              >
                {resumeArmedId === s.sid ? 'Confirm?' : 'Resume'}
              </button>
            )}
            {resumeSucceeded?.has(s.sid) && <span className="text-[9px] text-emerald-300">Resumed</span>}
            {resumeErrors?.[s.sid] && (
              <span className="rounded border border-rose-700/40 bg-rose-500/10 px-1.5 py-0.5 text-[9px] text-rose-200" title={resumeErrors[s.sid]}>
                Resume failed: {resumeErrors[s.sid]}
              </span>
            )}
            {/* P15/C-M18 — PAUSED strategies can still be technically "healthy"
                (last paper tick passed), but the green dot mis-signals "money is
                flowing". Treat any paused strategy as visually unhealthy and
                surface the reason in the tooltip. */}
            <span
              role="img"
              aria-label={
                s.status === 'PAUSED'
                  ? 'Paused — no trades flowing'
                  : s.is_healthy
                  ? 'Healthy'
                  : 'Unhealthy'
              }
              className={`h-2 w-2 rounded-full ${
                s.is_healthy && s.status !== 'PAUSED' ? 'bg-emerald-400' : 'bg-rose-400'
              }`}
              title={
                s.status === 'PAUSED'
                  ? 'Paused — no trades flowing'
                  : s.is_healthy
                  ? 'Healthy'
                  : 'Unhealthy'
              }
            />
          </div>
        </li>
      ))}
    </ul>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
      <header className="mb-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">{title}</header>
      {children}
    </section>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="py-6 text-center text-[11px] text-slate-500">{children}</div>;
}

function PauseConfirmModal({ n, onCancel, onConfirm }: { n: number; onCancel: () => void; onConfirm: () => void }) {
  const [typed, setTyped] = useState('');
  const [counter, setCounter] = useState(5);
  const okPhrase = 'PAUSE ALL';
  useTimer(counter, setCounter);
  // P15/C-M16 — Escape cancels the modal. Pause-all has no in-flight pending
  // state to guard against (the parent gates the click with `pauseMut.isPending`).
  useEscapeKey(true, onCancel);
  const ready = typed === okPhrase && counter <= 0;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70" onClick={onCancel}>
      <div
        className="w-full max-w-md rounded-2xl border border-rose-700 bg-slate-900 p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="pause-confirm-title"
      >
        <h2 id="pause-confirm-title" className="flex items-center gap-2 text-sm font-bold uppercase tracking-widest text-rose-300">
          <AlertTriangle className="h-4 w-4" /> Confirm Emergency Stop
        </h2>
        <p className="mt-3 text-[12px] text-slate-300">
          Pause all {n} live/small-capital strategies. Strategies move to <code className="rounded bg-slate-800 px-1 text-amber-300">PAUSED</code>. Resume is per-strategy.
        </p>
        <input
          autoFocus
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder={`Type "${okPhrase}" to enable`}
          className="mt-3 w-full rounded border border-slate-700 bg-slate-950 px-3 py-1.5 text-[11px] text-slate-200"
        />
        <div className="mt-3 flex items-center justify-between">
          <span className="text-[10px] text-slate-500">{counter > 0 ? `Wait ${counter}s` : 'Ready'}</span>
          <div className="flex gap-2">
            <button onClick={onCancel} className="rounded-md border border-slate-700 px-3 py-1 text-[11px] text-slate-300 hover:bg-slate-800">Cancel</button>
            <button onClick={onConfirm} disabled={!ready} className="rounded-md border border-rose-700 bg-rose-500/30 px-3 py-1 text-[11px] font-bold text-rose-100 hover:bg-rose-500/40 disabled:opacity-40">
              Pause All
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function useTimer(value: number, setter: (v: number) => void) {
  useEffect(() => {
    if (value <= 0) return;
    const t = setTimeout(() => setter(value - 1), 1000);
    return () => clearTimeout(t);
  }, [value, setter]);
}

// P15/C-M16 — standard "Escape closes the modal" accessibility hook for the
// PauseConfirmModal + DeployConfirmModal. The trading-terminal page declares
// an identical hook; the duplication is intentional (small enough not to
// warrant a shared module, and live-trade lives under app/(shell) not
// components/ — outside the cross-page util convention).
function useEscapeKey(active: boolean, onEscape: () => void) {
  const cbRef = useRef<() => void>(onEscape);
  useEffect(() => { cbRef.current = onEscape; });
  useEffect(() => {
    if (!active) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') cbRef.current();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [active]);
}
