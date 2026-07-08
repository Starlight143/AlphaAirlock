'use client';

import clsx from 'clsx';
import ReactMarkdown from 'react-markdown';
import { markdownComponents, remarkMathPlugins, rehypeMathPlugins } from '@/lib/markdownComponents';
import { useQuery } from '@tanstack/react-query';
import type { AlphaStrategy, PipelineStatusPayload } from '@/lib/api';
import { api } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { TERMINAL_STATES } from '@/lib/derive';
import PerformanceGrid from './PerformanceGrid';
import BacktestCsvTab from './BacktestCsvTab';
import BacktestConfigPanel from './BacktestConfigPanel';

export type Tab = 'code' | 'log' | 'csv' | 'performance';

const TABS: { key: Tab; label: string }[] = [
  { key: 'code', label: 'Strategy Code' },
  { key: 'log', label: 'Log Output' },
  { key: 'csv', label: 'Backtest CSV' },
  { key: 'performance', label: 'Performance' },
];

type Props = {
  strategy: AlphaStrategy;
  tab: Tab;
  onTabChange: (t: Tab) => void;
};

/**
 * Right pane of the strategy detail page — 4 tabs matching the reference:
 *   STRATEGY CODE | LOG OUTPUT | BACKTEST CSV | PERFORMANCE
 * `tab` is lifted to the parent so it can switch the page layout into
 * full-bleed mode for tabs that need the entire viewport (CSV per-bar tape).
 */
export default function TabsPane({ strategy, tab, onTabChange }: Props) {
  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div role="tablist" className="flex items-center gap-1 border-b border-slate-800 bg-slate-950/30 px-3">
        {TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            onClick={() => onTabChange(t.key)}
            className={clsx(
              'border-b-2 px-3 py-2 text-[11px] font-bold uppercase tracking-wider transition',
              tab === t.key
                ? 'border-cyan-500 text-cyan-200'
                : 'border-transparent text-slate-500 hover:text-slate-200',
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto">
        {tab === 'code' ? (
          <CodeTab strategy={strategy} />
        ) : tab === 'log' ? (
          <LogTab strategyId={strategy.id} />
        ) : tab === 'csv' ? (
          <BacktestCsvTab strategyId={strategy.id} />
        ) : (
          <div className="p-4">
            <PerformanceGrid strategy={strategy} />
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * STRATEGY CODE tab — formula code on top, the Backtest Config YAML stacked
 * below (per the reference layout), and the raw strategy config blob in a
 * collapsible `<details>` for power-users.
 */
function CodeTab({ strategy }: { strategy: AlphaStrategy }) {
  const code = (strategy.formula_code || '').trim();
  const yaml = ((strategy.config?.backtest_config_yaml as string | undefined) || '').trim();
  const yamlInvalid = !!strategy.config?.config_yaml_invalid;
  // P6-M15: when the Researcher/Coder agents emit a prose explanation of the
  // strategy logic, render it as markdown above the raw code so the right
  // pane matches the reference screenshot's "Step 1..4 + code" pattern.
  const prose =
    (strategy.config?.formula_explanation as string | undefined) ||
    (strategy.config?.alpha_story as string | undefined) ||
    '';

  return (
    <div className="space-y-4 p-4">
      {prose && (
        <section>
          <header className="mb-2">
            <h3 className="font-mono text-[11px] font-bold uppercase tracking-widest text-cyan-300">
              ## Strategy Logic ##
            </h3>
          </header>
          <article className="prose prose-invert prose-sm max-w-none font-mono leading-relaxed text-slate-200 prose-headings:font-mono prose-headings:text-slate-100 prose-h2:text-[12px] prose-h2:font-bold prose-h2:tracking-wider prose-h2:uppercase prose-h2:text-emerald-300 prose-p:font-mono prose-li:font-mono prose-code:text-emerald-300">
            {/* P31-S2: pass safe markdownComponents to filter javascript:/data: hrefs (XSS defense). */}
            <ReactMarkdown components={markdownComponents} remarkPlugins={remarkMathPlugins} rehypePlugins={rehypeMathPlugins}>{prose}</ReactMarkdown>
          </article>
        </section>
      )}

      <section>
        <header className="mb-2 flex items-baseline justify-between">
          <h3 className="font-mono text-[11px] font-bold uppercase tracking-widest text-cyan-300">
            ## Strategy Code ##
          </h3>
          <span className="text-[9px] uppercase tracking-widest text-slate-600">
            sandbox-validated
          </span>
        </header>
        {code ? (
          <pre className="overflow-x-auto rounded border border-slate-800 bg-slate-950 p-3 text-[11px] leading-relaxed text-emerald-300">
{code}
          </pre>
        ) : (
          <div className="rounded border border-slate-800 bg-slate-950/40 p-4 text-center text-xs text-slate-500">
            No factor code yet — Coder agent has not run.
          </div>
        )}
      </section>

      <section>
        <header className="mb-2 flex items-baseline justify-between">
          <h3 className="font-mono text-[11px] font-bold uppercase tracking-widest text-cyan-300">
            ## Backtest Config ##
          </h3>
          <span className="text-[9px] uppercase tracking-widest text-slate-600">
            ADRS OptimizerPipeline
          </span>
        </header>
        {yamlInvalid && (
          <div className="mb-2 rounded border border-amber-700/60 bg-amber-500/10 p-2 text-[10px] text-amber-200">
            YAML block was present but failed to parse. Raw text shown below.
          </div>
        )}
        {/* P8-FIX/H-8 — structured view backed by config.backtest_config dict. */}
        <BacktestConfigPanel
          config={(strategy.config?.backtest_config as Record<string, unknown> | null) ?? null}
        />
        {yaml && (
          <details className="mt-2 rounded border border-slate-800 bg-slate-950/30 p-2 text-[10px] text-slate-400">
            <summary className="cursor-pointer text-[10px] uppercase tracking-widest text-slate-500">
              Raw YAML source
            </summary>
            <pre className="mt-2 overflow-x-auto rounded border border-slate-800 bg-slate-950 p-3 text-[11px] leading-relaxed text-emerald-200">
{yaml}
            </pre>
          </details>
        )}
      </section>

      <details className="rounded border border-slate-800 bg-slate-950/30 p-3 text-[10px] text-slate-400">
        <summary className="cursor-pointer text-[10px] uppercase tracking-widest text-slate-500">
          Raw Strategy Config (JSON)
        </summary>
        <pre className="mt-2 overflow-x-auto rounded border border-slate-800 bg-slate-950 p-3 text-[10px] leading-relaxed text-slate-300">
{JSON.stringify(strategy.config ?? {}, null, 2)}
        </pre>
      </details>
    </div>
  );
}

function LogTab({ strategyId }: { strategyId: number }) {
  const q = useQuery<PipelineStatusPayload>({
    queryKey: queryKeys.pipelineStatus(strategyId),
    queryFn: () => api.pipelineStatus(strategyId),
    // R5/FE-DATA-001 — stop polling once the pipeline reaches a terminal state
    // (mirrors StrategyDetail's refetchInterval so both observers on the same
    // query key agree; in TanStack Query v5 a single observer returning false
    // cannot stop polling when another returns a numeric interval).
    refetchInterval: (q) => {
      const s = q.state.data?.current_status;
      return s && TERMINAL_STATES.has(s) ? false : 1_800;
    },
  });
  const logs = q.data?.terminal_logs ?? [];
  if (logs.length === 0) {
    return (
      <div className="p-4 text-xs text-slate-500">
        No log lines available — pipeline may have finished and been evicted
        from the in-process registry.
      </div>
    );
  }
  return (
    <div className="space-y-0.5 p-4 font-mono text-[11px] leading-relaxed text-emerald-200">
      {logs.map((line, i) => (
        <div key={i} className="border-b border-slate-900/60 py-0.5 text-slate-300">
          {line}
        </div>
      ))}
    </div>
  );
}
