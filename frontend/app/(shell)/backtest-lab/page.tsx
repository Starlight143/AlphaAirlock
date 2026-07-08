'use client';

import { Suspense, useCallback, useMemo } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { FlaskConical, Layers } from 'lucide-react';
import HistoryTab from './HistoryTab';
import SweepTab from './SweepTab';

type TabKey = 'history' | 'sweep';

/**
 * /backtest-lab — P8-FIX/C-3.
 *
 * Thin wrapper that selects between HISTORY and SWEEP tabs, syncing the
 * active tab to the URL (?tab=history|sweep) so the operator can bookmark or
 * share a specific view. Both child tabs are pure client components, so we
 * just need a Suspense boundary around useSearchParams() per Next.js 14 rules.
 */
export default function BacktestLabPage() {
  return (
    <Suspense fallback={<TabsFallback />}>
      <BacktestLabInner />
    </Suspense>
  );
}

function BacktestLabInner() {
  const router = useRouter();
  const params = useSearchParams();
  const raw = (params?.get('tab') || 'history').toLowerCase();
  const active: TabKey = raw === 'sweep' ? 'sweep' : 'history';

  const setTab = useCallback(
    (next: TabKey) => {
      if (next === active) return;
      // Preserve other query params if any.
      const usp = new URLSearchParams(params?.toString() ?? '');
      usp.set('tab', next);
      router.replace(`/backtest-lab?${usp.toString()}`, { scroll: false });
    },
    [active, params, router],
  );

  const tabs = useMemo(
    () =>
      [
        { key: 'history' as TabKey, label: 'HISTORY', icon: Layers },
        { key: 'sweep' as TabKey, label: 'SWEEP', icon: FlaskConical },
      ] satisfies { key: TabKey; label: string; icon: typeof Layers }[],
    [],
  );

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      <div className="flex shrink-0 items-stretch gap-1 border-b border-slate-800 bg-slate-950/40 px-3 pt-3">
        {tabs.map((t) => {
          const isActive = active === t.key;
          const Icon = t.icon;
          return (
            <button
              key={t.key}
              type="button"
              onClick={() => setTab(t.key)}
              className={`flex items-center gap-2 rounded-t-lg border border-b-0 px-5 py-2 text-[11px] font-bold uppercase tracking-[0.2em] transition ${
                isActive
                  ? 'border-cyan-700 bg-cyan-500/15 text-cyan-200'
                  : 'border-slate-800 bg-slate-900/40 text-slate-400 hover:text-slate-200'
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
              {t.label}
            </button>
          );
        })}
      </div>
      <div className="min-h-0 flex-1 overflow-hidden">
        {active === 'history' ? <HistoryTab /> : <SweepTab />}
      </div>
    </div>
  );
}

function TabsFallback() {
  return (
    <div className="flex h-full items-center justify-center text-[11px] text-slate-500">
      Loading…
    </div>
  );
}
