'use client';

import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Loader2 } from 'lucide-react';
import clsx from 'clsx';
import type { AlphaStrategy, PipelineStatusPayload } from '@/lib/api';
import { api } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { classifyVerdict, TERMINAL_STATES } from '@/lib/derive';
import { stageLabel, stageToneClass } from '@/lib/stageLabels';
import PipelineStepper from '@/components/PipelineStepper';
import HypothesisPane from './HypothesisPane';
import TabsPane, { type Tab } from './TabsPane';

type Props = { strategyId: number };

/**
 * Strategy detail orchestrator — two-pane layout (left markdown / right tabs)
 * plus a slim header with breadcrumb + stepper + active agent.
 *
 * Uses TanStack Query so polling, refetch-on-window-focus, etc. are handled
 * automatically. Strategy refetch is triggered whenever the live pipeline
 * status transitions to a terminal state.
 */
export default function StrategyDetail({ strategyId }: Props) {
  const qc = useQueryClient();

  // P5-FE-10: tab state is hoisted here so the parent can switch the
  // body to a single-column "full bleed" layout for tabs that need it
  // (BACKTEST CSV per-bar tape).
  const [tab, setTab] = useState<Tab>('code');
  const fullBleed = tab === 'csv';

  const strategyQ = useQuery<AlphaStrategy>({
    queryKey: queryKeys.strategy(strategyId),
    queryFn: () => api.strategy(strategyId),
  });

  const statusQ = useQuery<PipelineStatusPayload>({
    queryKey: queryKeys.pipelineStatus(strategyId),
    queryFn: () => api.pipelineStatus(strategyId),
    refetchInterval: (q) => {
      const s = q.state.data?.current_status;
      // Stop polling once a terminal state is reached.
      if (s && TERMINAL_STATES.has(s)) return false;
      // If status remains absent after ~100 polls (~3 min at 1.8s), stop.
      // This covers imported/manually-created strategies that never have a
      // pipeline entry and would otherwise poll indefinitely.
      if (!s && q.state.dataUpdateCount >= 100) return false;
      return 1_800;
    },
  });

  // When the pipeline reaches a terminal state, immediately refetch the
  // full strategy row so the right pane has critic verdict + metrics.
  useEffect(() => {
    const s = statusQ.data?.current_status;
    if (s && TERMINAL_STATES.has(s)) {
      qc.invalidateQueries({ queryKey: queryKeys.strategy(strategyId) });
    }
  }, [statusQ.data?.current_status, strategyId, qc]);

  const strategy = strategyQ.data;
  const live = statusQ.data;
  // C-M9 — use 'UNKNOWN' sentinel instead of '—'. The literal em-dash
  // previously leaked into the right-side header subtitle ("…s · —")
  // whenever both live and strategy.status were missing.
  const currentStatus = live?.current_status || strategy?.status || 'UNKNOWN';
  const isLive = currentStatus !== 'UNKNOWN' && !TERMINAL_STATES.has(currentStatus);

  if (strategyQ.isLoading || !strategy) {
    return (
      <div className="flex h-full items-center justify-center text-xs text-slate-500">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        Loading strategy…
      </div>
    );
  }
  if (strategyQ.isError) {
    return (
      <div className="flex h-full items-center justify-center text-xs text-rose-400">
        Failed to load strategy: {(strategyQ.error as Error).message}
      </div>
    );
  }

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      {/* Breadcrumb header — slug-style filename mirroring the reference UI.
         P8-FIX/LOW: the slug already encodes the YYYY-MM-DD prefix (see
         AlphaStrategy.slug()) — just expose a tooltip with the full string
         so truncated wide names are recoverable on hover. */}
      <header className="flex flex-col gap-2 border-b border-slate-800 bg-slate-900/40 px-5 py-3">
        <div className="flex items-center justify-between">
          <div className="flex items-baseline gap-2 overflow-hidden">
            <span className="shrink-0 text-[10px] font-bold uppercase tracking-widest text-slate-500">
              Strategy
            </span>
            <span className="shrink-0 font-mono text-base font-bold text-cyan-300">
              S#{strategy.id}
            </span>
            {strategy.alpha_id && (
              <span
                className="shrink-0 rounded border border-cyan-700/40 bg-cyan-500/10 px-1.5 py-0.5 font-mono text-[10px] text-cyan-200"
                title="Canonical alpha id (stable across renames)"
              >
                {strategy.alpha_id}
              </span>
            )}
            <span
              className="truncate font-mono text-xs text-slate-200"
              title={strategy.slug ?? strategy.name}
            >
              {strategy.slug ?? strategy.name}
            </span>
            {/* P17/A-H5 — semantic stage label badge */}
            <StageBadge stage={strategy.stage} />
            {/* P11-F2-07 — verdict chip surfaces approval state in header */}
            <VerdictBadge status={currentStatus} />
          </div>
          <div className="shrink-0 text-[10px] uppercase tracking-widest text-slate-500">
            {live ? (
              <>
                {Number.isFinite(live.execution_time_seconds) ? live.execution_time_seconds.toFixed(1) : '—'}s ·{' '}
                <span
                  className="inline-block max-w-[14rem] truncate align-bottom"
                  title={live.active_agent}
                >
                  {live.active_agent}
                </span>
              </>
            ) : (
              currentStatus
            )}
          </div>
        </div>
        <PipelineStepper status={currentStatus} />
      </header>

      {/* Body — 2-pane normally; full-bleed when an interior tab requests it. */}
      <div
        className={clsx(
          'relative grid flex-1 gap-3 overflow-hidden p-3',
          fullBleed ? 'grid-cols-1' : 'grid-cols-2',
        )}
      >
        {!fullBleed && (
          <section className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
            <HypothesisPane strategy={strategy} />
          </section>
        )}
        <section className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
          <TabsPane strategy={strategy} tab={tab} onTabChange={setTab} />
        </section>

        {isLive && <LiveOverlay logs={live?.terminal_logs ?? []} status={currentStatus} />}
      </div>
    </div>
  );
}

function LiveOverlay({ logs, status }: { logs: string[]; status: string }) {
  return (
    <div className="absolute inset-3 z-30 flex flex-col items-center justify-center gap-3 rounded-xl bg-slate-950/85 backdrop-blur-sm">
      <Loader2 className="h-9 w-9 animate-spin text-cyan-300" />
      <div className="text-[11px] uppercase tracking-widest text-cyan-300">
        {status} — pipeline in flight
      </div>
      <div className="max-h-48 w-[min(640px,90%)] overflow-y-auto rounded border border-slate-800 bg-slate-950/80 p-2 font-mono text-[10px] leading-relaxed text-emerald-200">
        {logs.length === 0 ? (
          <div className="text-slate-500">Waiting for first log line…</div>
        ) : (
          logs.slice(-30).map((l, i) => <div key={i}>{l}</div>)
        )}
      </div>
    </div>
  );
}

/**
 * P17/A-H5 — StageBadge renders the semantic stage label ("Backtest in Loop",
 * "Live Trade", etc.) sourced from STAGE_LABELS. Tone is derived from the
 * numeric stage so colour stays consistent with the legacy pill scheme.
 */
function StageBadge({ stage }: { stage: number | null | undefined }) {
  const label = stageLabel(stage);
  const tone = stageToneClass(stage);
  const numeric =
    stage == null || !Number.isFinite(Number(stage))
      ? '—'
      : String(Math.trunc(Number(stage)));
  return (
    <span
      className={clsx(
        'ml-2 shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px] font-bold uppercase tracking-widest',
        tone,
      )}
      title={`Stage ${numeric} — ${label}`}
    >
      STAGE {numeric} · {label}
    </span>
  );
}

/**
 * P11-F2-07 — VerdictBadge classifies the strategy's terminal/quality state
 * into PASS / FAIL / PENDING tones so the header instantly communicates the
 * outcome without parsing the raw status enum.
 */
function VerdictBadge({ status }: { status: string }) {
  const kind = classifyVerdict(status);
  const tone =
    kind === 'PASS'
      ? 'border-emerald-700/60 bg-emerald-500/10 text-emerald-300'
      : kind === 'FAIL'
        ? 'border-rose-700/60 bg-rose-500/10 text-rose-300'
        : 'border-slate-700 bg-slate-900 text-slate-400';
  return (
    <span
      className={clsx(
        'ml-2 shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px] font-bold uppercase tracking-widest',
        tone,
      )}
      title={`Verdict derived from current status: ${status}`}
    >
      VERDICT {kind}
    </span>
  );
}
