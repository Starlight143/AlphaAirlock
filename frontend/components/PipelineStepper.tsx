'use client';

import { ChevronRight } from 'lucide-react';
import clsx from 'clsx';

// F3 — 7-step automated-pipeline stepper used by the per-strategy workspace.
// Aligned with the V2 bucket framing exposed at /api/v2/pipeline/buckets:
//   Alpha Ideas → Research → Factor Dev → Backtest → Critic →
//   Paper Trade → Live Trade.
// SMALL_CAPITAL collapses onto the final Live Trade step so the visual stays
// compact for the small-cap → full-live progression.
// GRAVEYARD / REJECTED is treated as a rejection presentation on the
// terminal step.
const STEPS = [
  { label: 'Alpha Ideas', match: ['INTAKE'] },
  { label: 'Research', match: ['STORY_GEN'] },
  { label: 'Factor Dev', match: ['CODE_GEN'] },
  { label: 'Backtest', match: ['BACKTESTING'] },
  { label: 'Critic', match: ['CRITIC_LOOP'] },
  { label: 'Paper Trade', match: ['APPROVED', 'PAPER_TRADE'] },
  { label: 'Live Trade', match: ['SMALL_CAPITAL', 'LIVE'] },
] as const;

const REJECTED_STATUSES = new Set(['REJECTED', 'GRAVEYARD']);
const APPROVED_TERMINAL_STATUSES = new Set([
  'APPROVED',
  'PAPER_TRADE',
  'SMALL_CAPITAL',
  'LIVE',
]);

type Props = { status: string | null };

export default function PipelineStepper({ status }: Props) {
  const rejected = !!status && REJECTED_STATUSES.has(status);
  const currentIdx = STEPS.findIndex((s) =>
    (s.match as readonly string[]).includes(status ?? ''),
  );
  return (
    <div className="flex h-full items-center gap-1.5 overflow-x-auto px-1">
      {STEPS.map((step, idx) => {
        const isCurrent = idx === currentIdx && !rejected;
        const isDone = currentIdx >= 0 && idx < currentIdx;
        // Final step glows green for any post-promotion status, not just LIVE,
        // so an operator can tell a strategy "made it" even mid-promotion.
        const isApproved =
          step.label === 'Live Trade' &&
          !!status &&
          APPROVED_TERMINAL_STATUSES.has(status);
        const isRejected = rejected && idx === STEPS.length - 1;
        return (
          <div key={step.label} className="flex items-center">
            <div
              className={clsx(
                'flex items-center gap-2 rounded-md border px-3 py-1.5 text-[11px] font-bold tracking-wide transition-all',
                {
                  'border-emerald-600 bg-emerald-500/15 text-emerald-300 animate-pulse-slow':
                    isCurrent,
                  'border-emerald-700/60 bg-emerald-500/10 text-emerald-400/90':
                    isDone || isApproved,
                  'border-rose-700 bg-rose-500/15 text-rose-300': isRejected,
                  'border-slate-800 bg-slate-950/40 text-slate-500':
                    !isCurrent && !isDone && !isApproved && !isRejected,
                },
              )}
            >
              <span
                className={clsx('h-2 w-2 rounded-full', {
                  'bg-emerald-400': isCurrent || isDone || isApproved,
                  'bg-rose-400': isRejected,
                  'bg-slate-600':
                    !isCurrent && !isDone && !isApproved && !isRejected,
                })}
              />
              {step.label.toUpperCase()}
            </div>
            {idx < STEPS.length - 1 && (
              <ChevronRight className="mx-0.5 h-3.5 w-3.5 text-slate-700" />
            )}
          </div>
        );
      })}
    </div>
  );
}
