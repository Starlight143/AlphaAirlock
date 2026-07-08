'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import clsx from 'clsx';
import {
  Activity,
  PlayCircle,
  ShieldCheck,
  ShieldAlert,
  Loader2,
  CheckCircle2,
  XCircle,
} from 'lucide-react';

import { api, ApiError, cryptoUuid, type AlphaStrategy, type PaperTradeRun, type TradeTapeResponse } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import EquityDrawdown from '@/components/charts/EquityDrawdown';
import DailyPnlBars from '@/components/charts/DailyPnlBars';
import RollingSharpe from '@/components/charts/RollingSharpe';
import DrawdownDistribution from '@/components/charts/DrawdownDistribution';
import RecoveryTime from '@/components/charts/RecoveryTime';
import PositionDistribution from '@/components/charts/PositionDistribution';

/**
 * /paper-trade — Stage 4 forward simulation console (P8-FIX/F6).
 *
 *  ┌─────────────────────┬────────────────────────────────────┐
 *  │ APPROVED strategies │ Selected strategy detail            │
 *  │ + paper-trade KPIs  │ (run button, health, equity chart)  │
 *  └─────────────────────┴────────────────────────────────────┘
 *
 *  P8-FIX/M-11: list filters by status + coach health, [PROVEN SR=x.xx]
 *               chip when healthy + Sharpe ≥ 1.0.
 *  P8-FIX/M-12: detail panel now has Overview | Signal Tape tabs.
 *  P8-FIX/M-10: detail panel extended with rolling Sharpe, drawdown dist,
 *               recovery time, and position distribution charts.
 */

type StatusFilter = 'ALL' | 'APPROVED' | 'PAPER_TRADE' | 'SMALL_CAPITAL' | 'LIVE';
type CoachFilter = 'ALL' | 'HEALTHY' | 'UNHEALTHY';

/**
 * Minimum annualized Sharpe (from the paper-trade window metrics) required
 * for a healthy run to earn the [PROVEN SR=x.xx] chip in the strategies list.
 * Tunable — spec line 289 cites 1.93 as an aspirational example, but 1.0 is
 * the floor used by the critic for the "healthy" gate and is what the UI
 * advertises. Bump this if the bar for "proven" should be raised.
 */
const PROVEN_SHARPE_THRESHOLD = 1.0;

export default function PaperTradePage() {
  const qc = useQueryClient();
  const [windowDays, setWindowDays] = useState(30);
  const [selected, setSelected] = useState<number | null>(null);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('ALL');
  const [coachFilter, setCoachFilter] = useState<CoachFilter>('ALL');
  // P29-F5 fix: track the strategy id that is currently in-flight for a
  // paper-trade run at the page level, independent of which panel is selected.
  // This allows the cross-strategy lock badge/disable to work even when the
  // user switches panel selection while a run is pending on another strategy.
  const [inFlightSid, setInFlightSid] = useState<number | null>(null);

  const strategiesQ = useQuery({
    queryKey: queryKeys.strategies,
    queryFn: api.strategies,
    refetchInterval: 10_000,
  });
  const runsQ = useQuery({
    queryKey: queryKeys.paperTradeList,
    queryFn: api.paperTradeList,
    refetchInterval: 10_000,
  });

  const eligibleRows: AlphaStrategy[] = useMemo(
    () =>
      (strategiesQ.data?.strategies ?? []).filter((s) =>
        ['APPROVED', 'PAPER_TRADE', 'SMALL_CAPITAL', 'LIVE'].includes(s.status),
      ),
    [strategiesQ.data],
  );
  const runsById = useMemo(() => {
    const m = new Map<number, PaperTradeRun>();
    for (const r of runsQ.data?.runs ?? []) m.set(r.strategy_id, r);
    return m;
  }, [runsQ.data]);

  const filteredRows = useMemo(() => {
    return eligibleRows.filter((s) => {
      if (statusFilter !== 'ALL' && s.status !== statusFilter) return false;
      if (coachFilter !== 'ALL') {
        const run = runsById.get(s.id);
        const healthy = run?.is_healthy === true;
        if (coachFilter === 'HEALTHY' && !healthy) return false;
        if (coachFilter === 'UNHEALTHY' && healthy) return false;
      }
      return true;
    });
  }, [eligibleRows, statusFilter, coachFilter, runsById]);

  const selectedRun = selected != null ? runsById.get(selected) ?? null : null;
  const selectedStrategy = eligibleRows.find((s) => s.id === selected) ?? null;

  // D-M10/P16 — both mutations now persist a per-strategy Idempotency-Key in
  // sessionStorage so a retry after a page reload (or React refetch after a
  // network blip) replays the same key instead of minting a fresh UUID. The
  // keys are cleared on success so the next *intentional* click gets a fresh
  // key for the next paper-trade run / promotion.
  const runPaper = useMutation({
    mutationFn: (sid: number) => {
      if (sid == null) throw new Error('No strategy selected');
      const storageKey = `paper-run-${sid}-key`;
      let key = sessionStorage.getItem(storageKey);
      if (!key) {
        key = cryptoUuid();
        sessionStorage.setItem(storageKey, key);
      }
      return api.paperTradeRun(sid, windowDays, { idempotencyKey: key });
    },
    onMutate: (sid) => {
      // Record which strategy is in-flight so the cross-strategy lock UI works
      // even when the user navigates to a different strategy panel mid-flight.
      setInFlightSid(sid);
    },
    onSuccess: (_data, sid) => {
      sessionStorage.removeItem(`paper-run-${sid}-key`);
      qc.invalidateQueries({ queryKey: queryKeys.paperTradeList });
    },
    onSettled: () => {
      setInFlightSid(null);
    },
  });

  const promote = useMutation({
    mutationFn: ({ target, sid }: { target: 'SMALL_CAPITAL'; sid: number }) => {
      if (sid == null) throw new Error('No strategy selected');
      const storageKey = `promote-${sid}-key`;
      let key = sessionStorage.getItem(storageKey);
      if (!key) {
        key = cryptoUuid();
        sessionStorage.setItem(storageKey, key);
      }
      return api.strategyPromote(sid, target, { idempotencyKey: key });
    },
    onSuccess: (_data, { sid }) => {
      sessionStorage.removeItem(`promote-${sid}-key`);
      qc.invalidateQueries({ queryKey: queryKeys.strategies });
      qc.invalidateQueries({ queryKey: queryKeys.pipelineBuckets });
      qc.invalidateQueries({ queryKey: queryKeys.pipelineBucketsV2 });
    },
  });

  // Clear stale mutation errors when the operator switches to a different
  // strategy so the error banner from strategy A does not bleed into strategy B.
  useEffect(() => {
    runPaper.reset();
    promote.reset();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);

  const totals = useMemo(() => {
    const eligibleIdSet = new Set(eligibleRows.map((s) => s.id));
    const eligibleRuns = Array.from(runsById.values()).filter((r) =>
      eligibleIdSet.has(r.strategy_id),
    );
    return {
      eligible: eligibleRows.length,
      withRun: eligibleRuns.length,
      healthy: eligibleRuns.filter((r) => r.is_healthy).length,
      unhealthy: eligibleRuns.filter((r) => !r.is_healthy).length,
    };
  }, [eligibleRows, runsById]);

  return (
    <div className="grid h-full w-full grid-cols-12 gap-3 overflow-hidden p-3">
      <aside className="col-span-4 flex flex-col gap-3 overflow-hidden">
        <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
          <h1 className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
            <Activity className="h-3.5 w-3.5 text-amber-400" />
            Paper Trade
          </h1>
          <p className="mt-1 text-[10px] text-slate-500">
            Forward-simulate APPROVED strategies on the most recent N days of
            data. Healthy runs unlock promotion to SMALL_CAPITAL.
          </p>
          <div className="mt-3 grid grid-cols-2 gap-2">
            <Kpi label="Eligible" value={totals.eligible} color="text-cyan-200" />
            <Kpi label="Runs" value={totals.withRun} color="text-cyan-200" />
            <Kpi label="Healthy" value={totals.healthy} color="text-emerald-300" />
            <Kpi label="Unhealthy" value={totals.unhealthy} color={totals.unhealthy ? 'text-rose-300' : 'text-slate-500'} />
          </div>
        </div>

        <div className="flex flex-1 flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
          <header className="border-b border-slate-800 px-3 py-2">
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
                Strategies
              </span>
              <span className="text-[9px] text-slate-500">
                {filteredRows.length}/{eligibleRows.length}
              </span>
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value as StatusFilter)}
                className="rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-[10px] text-slate-200"
              >
                <option value="ALL">All Statuses</option>
                <option value="APPROVED">APPROVED</option>
                <option value="PAPER_TRADE">PAPER_TRADE</option>
                <option value="SMALL_CAPITAL">SMALL_CAPITAL</option>
                <option value="LIVE">LIVE</option>
              </select>
              <select
                value={coachFilter}
                onChange={(e) => setCoachFilter(e.target.value as CoachFilter)}
                className="rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-[10px] text-slate-200"
              >
                <option value="ALL">All Coach</option>
                <option value="HEALTHY">Healthy</option>
                <option value="UNHEALTHY">Unhealthy</option>
              </select>
            </div>
          </header>
          <div className="flex-1 overflow-y-auto">
            {filteredRows.length === 0 && (
              <div className="p-4 text-center text-xs text-slate-500">
                {eligibleRows.length === 0
                  ? 'No APPROVED strategies yet. Run the pipeline first.'
                  : 'No strategies match the current filters.'}
              </div>
            )}
            {filteredRows.map((s) => {
              const run = runsById.get(s.id);
              const sharpe = Number(run?.metrics?.annualized_sharpe ?? 0);
              const showProven = run?.is_healthy && Number.isFinite(sharpe) && sharpe >= PROVEN_SHARPE_THRESHOLD;
              return (
                <button
                  key={s.id}
                  onClick={() => setSelected(s.id)}
                  className={clsx(
                    'block w-full border-b border-slate-900 px-3 py-2 text-left text-[11px] hover:bg-slate-900/50',
                    selected === s.id && 'bg-amber-500/5',
                  )}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-cyan-300">S#{s.id}</span>
                    <RunHealth run={run} />
                  </div>
                  <div className="line-clamp-1 text-[10px] text-slate-300">{s.name}</div>
                  {showProven && (
                    <div className="mt-1 inline-flex items-center rounded border border-emerald-700/60 bg-emerald-500/10 px-1.5 py-0.5 font-mono text-[9px] font-bold uppercase tracking-wider text-emerald-300">
                      PROVEN SR={sharpe.toFixed(2)}
                    </div>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      </aside>

      <section className="col-span-8 flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
        {selected == null || !selectedStrategy ? (
          <div className="flex h-full items-center justify-center text-xs text-slate-500">
            Pick a strategy to run forward simulation.
          </div>
        ) : (
          <SelectedPanel
            strategy={selectedStrategy}
            run={selectedRun}
            windowDays={windowDays}
            onWindowChange={setWindowDays}
            onRun={() => runPaper.mutate(selected!)}
            running={runPaper.isPending}
            runError={runPaper.error as Error | null}
            onPromote={() => promote.mutate({ target: 'SMALL_CAPITAL', sid: selected! })}
            promoting={promote.isPending}
            promoteError={promote.error as Error | null}
            globalRunning={inFlightSid != null}
            globalRunningSid={inFlightSid}
          />
        )}
      </section>
    </div>
  );
}

function Kpi({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-950/50 p-2">
      <div className={`font-mono text-base leading-none ${color}`}>{value}</div>
      <div className="mt-1 text-[9px] uppercase tracking-wider text-slate-500">{label}</div>
    </div>
  );
}

function RunHealth({ run }: { run?: PaperTradeRun }) {
  if (!run) {
    return (
      <span className="rounded border border-slate-700 px-1 text-[9px] text-slate-500">
        no run
      </span>
    );
  }
  return run.is_healthy ? (
    <span className="flex items-center gap-1 rounded border border-emerald-700/40 bg-emerald-500/10 px-1 text-[9px] text-emerald-300">
      <ShieldCheck className="h-2.5 w-2.5" /> healthy
    </span>
  ) : (
    <span className="flex items-center gap-1 rounded border border-rose-700/40 bg-rose-500/10 px-1 text-[9px] text-rose-300">
      <ShieldAlert className="h-2.5 w-2.5" /> unhealthy
    </span>
  );
}

// ---------------------------------------------------------------------------
// Selected panel — Overview + Signal Tape tabs
// ---------------------------------------------------------------------------

type DetailTab = 'overview' | 'signal-tape';

function SelectedPanel(props: {
  strategy: AlphaStrategy;
  run: PaperTradeRun | null;
  windowDays: number;
  onWindowChange: (n: number) => void;
  onRun: () => void;
  running: boolean;
  runError: Error | null;
  onPromote: () => void;
  promoting: boolean;
  promoteError: Error | null;
  /** P29-F5: true when ANY paper-trade mutation is in flight. */
  globalRunning: boolean;
  /** P29-F5: strategy id of the in-flight run; null when no run is pending. */
  globalRunningSid: number | null;
}) {
  const {
    strategy, run, windowDays, onWindowChange, onRun, running, runError,
    onPromote, promoting, promoteError, globalRunning, globalRunningSid,
  } = props;
  const promoted = strategy.status !== 'APPROVED';
  const [tab, setTab] = useState<DetailTab>('overview');
  const [showPromoteConfirm, setShowPromoteConfirm] = useState(false);
  const [promoteTyped, setPromoteTyped] = useState('');
  const [promoteCounter, setPromoteCounter] = useState(3);
  // R8/M1: Escape closes the promote-confirm modal (blocked while a promote is
  // in flight to avoid a mid-transition dismiss). Mirrors live-trade/terminal.
  useEscapeKey(showPromoteConfirm, () => { if (!promoting) setShowPromoteConfirm(false); });

  // R9/[19] — transient success feedback after a promote completes. The
  // background refetch flips strategy.status away from APPROVED up to ~10 s
  // later, so without this the operator gets no positive confirmation. Show a
  // chip for 4 s on the promoting true→false edge (no error); clear on switch.
  const [promoteSucceeded, setPromoteSucceeded] = useState(false);
  const prevPromotingRef = useRef(false);
  useEffect(() => {
    const wasPromoting = prevPromotingRef.current;
    prevPromotingRef.current = promoting;
    if (wasPromoting && !promoting && !promoteError) {
      setPromoteSucceeded(true);
      const t = window.setTimeout(() => setPromoteSucceeded(false), 4000);
      return () => window.clearTimeout(t);
    }
  }, [promoting, promoteError]);
  useEffect(() => { setPromoteSucceeded(false); }, [strategy.id]);

  // Reset to overview when switching strategies — otherwise the prior tab can
  // be sticky on an unrelated strategy and silently load that one's tape.
  useEffect(() => {
    setTab('overview');
  }, [strategy.id]);

  // Reset confirmation state each time the modal is opened.
  useEffect(() => {
    if (showPromoteConfirm) {
      setPromoteTyped('');
      setPromoteCounter(3);
    }
  }, [showPromoteConfirm]);

  // Close the modal if the strategy selection changes while it is open —
  // prevents the stale-name / wrong-target race where the operator has already
  // typed 'PROMOTE' for strategy A and the selection silently flips to B.
  useEffect(() => {
    if (showPromoteConfirm) setShowPromoteConfirm(false);
  }, [strategy.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Countdown while the modal is open.
  useEffect(() => {
    if (!showPromoteConfirm || promoteCounter <= 0) return;
    const t = window.setTimeout(() => setPromoteCounter((c) => c - 1), 1000);
    return () => window.clearTimeout(t);
  }, [showPromoteConfirm, promoteCounter]);

  const promoteReady = promoteTyped === 'PROMOTE' && promoteCounter <= 0 && !promoting;

  function handleConfirmedPromote() {
    if (!promoteReady) return;
    setShowPromoteConfirm(false);
    onPromote();
  }

  return (
    <>
      <header className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
        <div className="overflow-hidden">
          <div className="flex items-baseline gap-2 overflow-hidden">
            <span className="shrink-0 font-mono text-base text-cyan-300">
              S#{strategy.id}
            </span>
            <Link
              href={`/strategies/${strategy.id}`}
              className="truncate font-mono text-xs text-cyan-200 hover:underline"
              title={strategy.name}
            >
              {strategy.slug ?? strategy.name}
            </Link>
            <span className="shrink-0 rounded border border-amber-700/40 bg-amber-500/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-amber-300">
              {strategy.status}
            </span>
          </div>
          <div className="mt-0.5 text-[10px] text-slate-500">
            Forward simulation runs the same factor code on the last N days of
            data. Healthy = passes the same hard thresholds as the critic.
          </div>
        </div>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1 text-[10px] text-slate-500">
            window
            <input
              type="number"
              min={7}
              max={180}
              value={windowDays}
              onChange={(e) => onWindowChange(Math.max(7, Math.min(180, Number(e.target.value) || 30)))}
              className="w-16 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[10px] text-slate-200"
            />
            days
          </label>
          <button
            onClick={onRun}
            disabled={running || globalRunning}
            title={globalRunning && !running ? `Paper trade already running for S#${globalRunningSid ?? '?'}` : undefined}
            className="flex items-center gap-1.5 rounded-md border border-cyan-700 bg-cyan-500/15 px-3 py-1.5 text-[11px] font-bold tracking-wide text-cyan-200 hover:bg-cyan-500/25 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <PlayCircle className="h-3.5 w-3.5" />}
            {running ? 'RUNNING' : 'RUN PAPER TRADE'}
          </button>
          {globalRunning && !running && globalRunningSid != null && (
            <span
              className="rounded border border-amber-700/60 bg-amber-500/10 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider text-amber-200"
              title="A paper trade run is already in flight on another strategy."
            >
              running on S#{globalRunningSid}
            </span>
          )}
          {run?.is_healthy && !promoted && (
            <button
              onClick={() => setShowPromoteConfirm(true)}
              disabled={promoting}
              className="flex items-center gap-1.5 rounded-md border border-amber-700 bg-amber-500/15 px-3 py-1.5 text-[11px] font-bold tracking-wide text-amber-200 hover:bg-amber-500/25 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {promoting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ShieldCheck className="h-3.5 w-3.5" />}
              {promoting ? 'PROMOTING' : 'PROMOTE → SMALL_CAPITAL'}
            </button>
          )}
        </div>
      </header>

      {(runError || promoteError) && (
        <div className="border-b border-rose-800/60 bg-rose-500/10 px-5 py-2 text-[11px] text-rose-300">
          {(runError ?? promoteError)?.message}
        </div>
      )}

      {promoteSucceeded && (
        <div className="border-b border-emerald-800/60 bg-emerald-500/10 px-5 py-2 text-[11px] font-semibold text-emerald-300">
          ✓ Promoted to SMALL_CAPITAL — refreshing status…
        </div>
      )}

      <div className="flex items-center gap-2 border-b border-slate-800 px-5">
        <TabButton active={tab === 'overview'} onClick={() => setTab('overview')}>
          Overview
        </TabButton>
        <TabButton active={tab === 'signal-tape'} onClick={() => setTab('signal-tape')}>
          Signal Tape
        </TabButton>
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        {tab === 'overview' ? (
          <OverviewTab run={run} windowDays={windowDays} strategyId={strategy.id} />
        ) : (
          <SignalTapeTab strategyId={strategy.id} />
        )}
      </div>

      {/* PROMOTE → SMALL_CAPITAL confirmation modal */}
      {showPromoteConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm" onClick={() => { if (!promoting) setShowPromoteConfirm(false); }}>
          <div className="w-full max-w-sm rounded-xl border border-amber-700/60 bg-slate-900 p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <h2 className="mb-1 text-sm font-bold text-amber-300">
              Promote to SMALL_CAPITAL?
            </h2>
            <p className="mb-4 text-[11px] leading-relaxed text-slate-400">
              This will start live-capital trading on the next engine tick. Type{' '}
              <span className="font-mono font-bold text-amber-200">PROMOTE</span>{' '}
              below to confirm.
            </p>
            <input
              autoFocus
              type="text"
              value={promoteTyped}
              onChange={(e) => setPromoteTyped(e.target.value)}
              placeholder="Type PROMOTE to confirm"
              className="mb-4 w-full rounded border border-slate-700 bg-slate-800 px-3 py-2 font-mono text-xs text-slate-100 placeholder-slate-600 focus:border-amber-600 focus:outline-none"
            />
            <div className="flex items-center justify-end gap-2">
              <button
                onClick={() => setShowPromoteConfirm(false)}
                className="rounded-md border border-slate-700 px-3 py-1.5 text-[11px] text-slate-300 hover:bg-slate-800"
              >
                Cancel
              </button>
              <button
                onClick={handleConfirmedPromote}
                disabled={!promoteReady}
                className="flex items-center gap-1.5 rounded-md border border-amber-700 bg-amber-500/20 px-3 py-1.5 text-[11px] font-bold text-amber-200 hover:bg-amber-500/30 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {promoteCounter > 0 ? `Wait ${promoteCounter}s…` : 'Confirm PROMOTE'}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function TabButton({
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
        '-mb-px border-b-2 px-3 py-2 text-[11px] font-bold uppercase tracking-widest transition-colors',
        active
          ? 'border-cyan-400 text-cyan-200'
          : 'border-transparent text-slate-500 hover:text-slate-300',
      )}
    >
      {children}
    </button>
  );
}

function OverviewTab({
  run,
  windowDays,
  strategyId,
}: {
  run: PaperTradeRun | null;
  windowDays: number;
  strategyId: number;
}) {
  // Trade tape underlies the per-bar position chart on the overview panel
  // (P8-FIX/M-10). Disabled until the user is actually on this tab so we
  // don't waste a request when they're viewing Signal Tape.
  const tradesQ = useQuery({
    queryKey: queryKeys.strategyTrades(strategyId, false),  // R5/FE-DATA-002: share cache with PerformanceGrid/MultiPositionOverlay
    queryFn: () => api.strategyTrades(strategyId, { limit: 500, nonzeroOnly: false }),
    staleTime: 60_000,
    enabled: !!run,
  });

  if (!run) {
    return (
      <div className="flex h-full items-center justify-center text-center">
        <div className="rounded-lg border border-slate-800 bg-slate-950/40 p-6">
          <PlayCircle className="mx-auto mb-2 h-8 w-8 text-cyan-400" />
          <div className="text-sm text-slate-200">
            No paper-trade run yet for this strategy.
          </div>
          <div className="mt-1 text-[10px] text-slate-500">
            Click RUN PAPER TRADE to forward-simulate on the last {windowDays} days.
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className="space-y-4">
      <HealthChecklist run={run} />
      <EquityDrawdown data={run.equity_curve} />
      <DailyPnlBars data={run.equity_curve} />
      <div className="grid grid-cols-2 gap-3">
        <RollingSharpe data={run.equity_curve} windowSelector />
        <DrawdownDistribution data={run.equity_curve} />
        <RecoveryTime data={run.equity_curve} />
        <PositionDistribution data={tradesQ.data?.rows ?? []} />
      </div>
      <MetricsTable run={run} />
    </div>
  );
}

function HealthChecklist({ run }: { run: PaperTradeRun }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[10px] font-bold uppercase tracking-widest text-cyan-300">
          Health Gate · {run.window_days}d window · run at {run.run_at.slice(11, 19)} UTC
        </span>
        <span
          className={`rounded border px-2 py-0.5 text-[9px] font-bold uppercase tracking-wider ${
            run.is_healthy
              ? 'border-emerald-700/40 bg-emerald-500/10 text-emerald-300'
              : 'border-rose-700/40 bg-rose-500/10 text-rose-300'
          }`}
        >
          {run.is_healthy ? 'HEALTHY' : 'UNHEALTHY'}
        </span>
      </div>
      <ul className="space-y-1">
        {run.health_notes.map((note, i) => {
          const passed = note.endsWith('OK');
          const Icon = passed ? CheckCircle2 : XCircle;
          const color = passed ? 'text-emerald-300' : 'text-rose-300';
          return (
            <li key={i} className={`flex items-center gap-2 text-[11px] ${color}`}>
              <Icon className="h-3 w-3" />
              <span>{note}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// R5/UX-08 — human-readable labels + per-metric formatting for the Window
// Metrics table (mirrors the arena page's METRIC_ROWS). Unknown keys fall back
// to the raw key name + toFixed(4). Note: the backtest engine emits
// 'cumulative_return' (not 'cum_return').
const PAPER_METRIC_ROWS: { key: string; label: string; fmt: (v: number) => string }[] = [
  { key: 'annualized_sharpe', label: 'Sharpe (annual)', fmt: (v) => v.toFixed(2) },
  { key: 'max_drawdown',      label: 'Max DD',          fmt: (v) => `${(v * 100).toFixed(1)}%` },
  { key: 'cumulative_return', label: 'Cum Return',      fmt: (v) => `${(v * 100).toFixed(1)}%` },
  { key: 'win_rate',          label: 'Win Rate',        fmt: (v) => `${(v * 100).toFixed(1)}%` },
  { key: 'profit_factor',     label: 'Profit Factor',   fmt: (v) => v.toFixed(2) },
  { key: 'annualized_return', label: 'Return (annual)', fmt: (v) => `${(v * 100).toFixed(1)}%` },
  { key: 'calmar_ratio',      label: 'Calmar',          fmt: (v) => v.toFixed(2) },
];

function MetricsTable({ run }: { run: PaperTradeRun }) {
  const knownMap: Record<string, { key: string; label: string; fmt: (v: number) => string }> =
    Object.fromEntries(PAPER_METRIC_ROWS.map((r) => [r.key, r]));
  const rows = Object.entries(run.metrics);
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
      <div className="mb-2 text-[10px] font-bold uppercase tracking-widest text-cyan-300">
        Window Metrics
      </div>
      <table className="w-full text-[11px]">
        <tbody>
          {rows.map(([k, v]) => {
            const def = knownMap[k];
            const label = def ? def.label : k;
            const formatted = Number.isFinite(v)
              ? def ? def.fmt(Number(v)) : Number(v).toFixed(4)
              : String(v);
            return (
              <tr key={k} className="border-b border-slate-900/40">
                <td className="py-1 text-slate-500">{label}</td>
                <td className="py-1 text-right font-mono text-slate-200">{formatted}</td>
              </tr>
            );
          })}
          <tr>
            <td className="py-1 text-slate-500">trades</td>
            <td className="py-1 text-right font-mono text-slate-200">{run.trades}</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Signal Tape tab
// ---------------------------------------------------------------------------

function SignalTapeTab({ strategyId }: { strategyId: number }) {
  const tapeQ = useQuery<TradeTapeResponse>({
    queryKey: ['strategy-trades', strategyId, true],  // R5/FE-DATA-002: consistent namespace
    queryFn: () => api.strategyTrades(strategyId, { limit: 500, nonzeroOnly: true }),
    staleTime: 30_000,
  });
  const rows = tapeQ.data?.rows ?? [];
  // P12 — display newest-first so the operator sees the latest signal at
  // the top of the table. Reverse a shallow copy so the underlying query
  // cache is not mutated.
  const displayRows = useMemo(() => rows.slice().reverse(), [rows]);
  if (tapeQ.isLoading) {
    return (
      <div className="flex items-center gap-2 text-[11px] text-slate-500">
        <Loader2 className="h-3 w-3 animate-spin" /> Loading signal tape…
      </div>
    );
  }
  if (tapeQ.isError) {
    const err = tapeQ.error as Error;
    // A 404 from the tape endpoint is an EXPECTED "no backtest data yet" state —
    // the per-bar tape only exists once the pipeline's backtest stage has run for
    // this strategy. Render a calm empty card (mirrors the Overview "no run yet"
    // panel) instead of dumping the raw `HTTP 404 ... {"detail":...}` string into
    // a red error box, which reads as a system failure to the operator.
    if (err instanceof ApiError && err.status === 404) {
      return (
        <div className="rounded-lg border border-slate-800 bg-slate-950/40 p-6 text-center">
          <PlayCircle className="mx-auto mb-2 h-8 w-8 text-slate-600" />
          <div className="text-sm text-slate-200">No signal tape yet for this strategy.</div>
          <div className="mt-1 text-[10px] text-slate-500">
            {err.detail?.trim()
              ? err.detail
              : 'The per-bar tape is generated by the pipeline backtest stage. Run the pipeline first.'}
          </div>
        </div>
      );
    }
    return (
      <div className="rounded-md border border-rose-700/60 bg-rose-500/10 px-3 py-2 text-[11px] text-rose-300">
        {err?.message ?? 'Failed to load tape.'}
      </div>
    );
  }
  if (rows.length === 0) {
    return (
      <div className="rounded-lg border border-slate-800 bg-slate-950/40 p-6 text-center text-[11px] text-slate-500">
        No non-zero signal bars in the per-bar backtest tape.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800 bg-slate-950/40">
      <div className="border-b border-slate-800 px-3 py-2 text-[10px] font-bold uppercase tracking-widest text-cyan-300">
        Per-bar Signal Tape · {tapeQ.data?.total ?? rows.length} bars (showing {rows.length})
      </div>
      <table className="w-full text-[11px]">
        <thead className="sticky top-0 bg-slate-950 text-[9px] uppercase tracking-wider text-slate-500">
          <tr>
            <th scope="col" className="px-3 py-1.5 text-left">Time</th>
            <th scope="col" className="px-3 py-1.5 text-left">Direction</th>
            <th scope="col" className="px-3 py-1.5 text-right">PnL %</th>
            <th scope="col" className="px-3 py-1.5 text-right">Cum PnL %</th>
            <th scope="col" className="px-3 py-1.5 text-right">DD %</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {/* P31-F3: composite key on start_time — index keys break React DOM
              reuse as bars stream in, forcing remount of every row each tick. */}
          {displayRows.map((r) => {
            const direction = r.direction ?? deriveDirection(r.signal);
            return (
              <tr key={r.start_time} className="border-t border-slate-900/60">
                <td className="px-3 py-1 text-slate-400">{r.start_time}</td>
                <td className="px-3 py-1">
                  <DirectionChip direction={direction} />
                </td>
                <td className={`px-3 py-1 text-right ${pnlClass(r.pnl_pct)}`}>
                  {fmtPct(r.pnl_pct)}
                </td>
                <td className={`px-3 py-1 text-right ${pnlClass(r.cum_pnl_pct)}`}>
                  {fmtPct(r.cum_pnl_pct)}
                </td>
                <td className="px-3 py-1 text-right text-rose-300">
                  {fmtPct(r.drawdown_pct)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function DirectionChip({ direction }: { direction: 'long' | 'short' | 'flat' }) {
  if (direction === 'long') {
    return (
      <span className="rounded border border-emerald-700/60 bg-emerald-500/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-emerald-300">
        LONG
      </span>
    );
  }
  if (direction === 'short') {
    return (
      <span className="rounded border border-rose-700/60 bg-rose-500/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-rose-300">
        SHORT
      </span>
    );
  }
  return (
    <span className="rounded border border-slate-700 bg-slate-900 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-slate-400">
      FLAT
    </span>
  );
}

function deriveDirection(signal: number): 'long' | 'short' | 'flat' {
  const s = Math.sign(Number(signal) || 0);
  if (s > 0) return 'long';
  if (s < 0) return 'short';
  return 'flat';
}

function pnlClass(v: number | null | undefined): string {
  const n = Number(v);
  if (!Number.isFinite(n) || n === 0) return 'text-slate-400';
  return n > 0 ? 'text-emerald-300' : 'text-rose-300';
}

function fmtPct(v: number | null | undefined): string {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  const sign = n > 0 ? '+' : '';
  return `${sign}${(n * 100).toFixed(2)}%`;
}

// Accessibility: Escape key closes an active modal. Mirrors the identical hook
// in live-trade/page.tsx and trading-terminal/page.tsx. The callback is held in
// a ref so the keydown listener always sees the latest closure without
// re-subscribing on every render.
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
