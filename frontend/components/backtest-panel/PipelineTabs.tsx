'use client';

import clsx from 'clsx';
import type { AlphaStrategy } from '@/lib/api';

/**
 * P8-FIX/M-18 (F3) — pipeline scope tabs realigned to the V2 bucket framing.
 *
 * The tab keys are the 7-bucket V2 partition (research / development /
 * backtest / critic / approved / paper / live), and labels + underlying
 * status sets mirror that row exactly. We intentionally do NOT pull these
 * from /api/v2/pipeline/buckets — the tab partitioning is a stable UI
 * contract; hardcoding it here avoids a useless cross-page roundtrip and
 * lets server components that wrap this file stay stateless.
 */
export type PipelineScope =
  | 'research'
  | 'development'
  | 'backtest'
  | 'critic'
  | 'approved'
  | 'paper'
  | 'live';

const TAB_LABEL: Record<PipelineScope, string> = {
  research: 'Research',
  development: 'Development',
  backtest: 'Backtest',
  critic: 'Critic Loop',
  approved: 'Approved',
  paper: 'Paper Trade',
  live: 'Live Trade',
};

// P13 (C-L3) — Shorter labels for sub-md viewports where the 7-tab strip
// otherwise overflows. The full label still renders at md and above.
const TAB_LABEL_SHORT: Record<PipelineScope, string> = {
  research: 'Research',
  development: 'Dev',
  backtest: 'Backtest',
  critic: 'Critic',
  approved: 'Approved',
  paper: 'Paper',
  live: 'Live',
};

// F3 — Critic Loop and Approved are first-class tabs so the operator can
// inspect strategies in each phase without losing the dedicated counter.
// Keep in sync with backend/app/main.py if bucket statuses change.
export const TAB_STATUSES: Record<PipelineScope, string[]> = {
  research: ['INTAKE', 'STORY_GEN'],
  development: ['CODE_GEN'],
  backtest: ['BACKTESTING'],
  critic: ['CRITIC_LOOP'],
  approved: ['APPROVED'],
  paper: ['PAPER_TRADE'],
  live: ['SMALL_CAPITAL', 'LIVE'],
};

type Props = {
  active: PipelineScope;
  onChange: (scope: PipelineScope) => void;
  strategies: AlphaStrategy[];
};

export default function PipelineTabs({ active, onChange, strategies }: Props) {
  const counts: Record<PipelineScope, number> = {
    research: 0,
    development: 0,
    backtest: 0,
    critic: 0,
    approved: 0,
    paper: 0,
    live: 0,
  };
  for (const s of strategies) {
    const up = (s.status || '').toUpperCase();
    for (const scope of Object.keys(counts) as PipelineScope[]) {
      if (TAB_STATUSES[scope].includes(up)) counts[scope]++;
    }
  }
  return (
    <div role="tablist" className="flex items-center gap-1 overflow-x-auto border-b border-slate-800 px-3">
      {(Object.keys(TAB_LABEL) as PipelineScope[]).map((k) => (
        <button
          key={k}
          role="tab"
          aria-selected={active === k}
          onClick={() => onChange(k)}
          className={clsx(
            'shrink-0 border-b-2 px-3 py-2 text-[11px] font-bold uppercase tracking-wider transition',
            active === k
              ? 'border-cyan-500 text-cyan-200'
              : 'border-transparent text-slate-500 hover:text-slate-200',
          )}
        >
          {/* P13 (C-L3) — shorter label below md so the 7-tab strip fits on
              1366px laptops without horizontal overflow. */}
          <span className="md:hidden">{TAB_LABEL_SHORT[k]}</span>
          <span className="hidden md:inline">{TAB_LABEL[k]}</span>{' '}
          <span className="ml-1 text-slate-600">[{counts[k]}]</span>
        </button>
      ))}
    </div>
  );
}

export function filterByScope(strategies: AlphaStrategy[], scope: PipelineScope): AlphaStrategy[] {
  const allowed = new Set(TAB_STATUSES[scope]);
  return strategies.filter((s) => allowed.has((s.status || '').toUpperCase()));
}
