'use client';

import { useCallback, useState } from 'react';
import { GitBranch } from 'lucide-react';
import StrategyTable from '@/components/backtest-panel/StrategyTable';

/**
 * /strategies — strategy index page.
 *
 * The sidebar's "Strategies" entry (components/layout/nav.ts) links to
 * `/strategies`, but historically only the detail route
 * `/strategies/[id]` existed, so the index 404'd. This page provides the
 * missing list view by reusing the shared <StrategyTable> (the same table
 * rendered on /backtest-panel), which already fetches /api/strategies via
 * TanStack Query and links each row to `/strategies/{id}`.
 *
 * Selection state is owned here purely to satisfy StrategyTable's
 * (selectedIds, toggle) contract; on this page it just lets the operator
 * highlight rows. No bulk action is wired so the checkboxes are a harmless
 * visual aid — kept to avoid forking the component.
 */
export default function StrategiesPage() {
  const [selectedIds, setSelectedIds] = useState<Set<number>>(() => new Set());

  const toggle = useCallback((id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  return (
    <div className="flex h-full w-full flex-col gap-3 overflow-y-auto p-3">
      <header className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
        <div className="flex items-center gap-2">
          <GitBranch className="h-4 w-4 text-cyan-400" />
          <h1 className="text-sm font-bold tracking-widest text-slate-100">
            STRATEGIES
          </h1>
        </div>
        <p className="mt-1 text-[11px] text-slate-500">
          Every strategy across the pipeline — from freshly ingested alpha
          ideas through backtested, approved, paper-traded and live entries.
          Click an S#id to open its detail view. Ranked by annualized Sharpe.
        </p>
      </header>

      <section className="min-h-0 flex-1">
        <StrategyTable selectedIds={selectedIds} toggle={toggle} />
      </section>
    </div>
  );
}
