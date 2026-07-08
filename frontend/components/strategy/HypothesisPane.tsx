'use client';

import { useMemo, useState } from 'react';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import ReactMarkdown from 'react-markdown';
import { markdownComponents, remarkMathPlugins, rehypeMathPlugins } from '@/lib/markdownComponents';
import clsx from 'clsx';
import { api, type AlphaStrategy } from '@/lib/api';
import CriticVerdict from './CriticVerdict';

type Props = { strategy: AlphaStrategy };

type LeftTab = 'story' | 'config' | 'raw' | 'formula';

type BacktestRunRow = {
  start_date?: string | null;
  end_date?: string | null;
  sharpe?: number | null;
  win_rate?: number | null;
  max_drawdown?: number | null;
  profit_factor?: number | null;
  trades?: number | null;
  created_at?: string | null;
  // Origin label for the fallback synthesised row.
  source?: string;
};

/**
 * Left pane of the strategy detail page (P6-A8/A9 rewrite).
 *
 * Three tabs at the top — reference UI parity:
 *   - EDITING v{N}   : alpha story markdown + ProvenBacktestResults + Critic
 *   - FULL CONFIG    : backtest_config_yaml (and the rest of the config dict)
 *   - RAW JSON       : entire ``strategy.config`` JSON for power users
 *
 * The version selector is currently single-entry (`v1`) because the backend
 * doesn't expose BacktestRun history yet (deferred to a follow-on B-task).
 * The control is shaped so flipping to a real query later is a one-line
 * change in ``versions = useMemo(...)``.
 */
export default function HypothesisPane({ strategy }: Props) {
  const [tab, setTab] = useState<LeftTab>('story');
  const [version, setVersion] = useState<number>(1);

  // Future: when /api/strategies/{id}/runs exists, useQuery here and feed
  // versions / proven-results from that payload. Today the synthesised list
  // gives the UI shell its shape.
  // TODO(P13/C-M5): wire to api.strategyRuns(strategy.id) once the BacktestRun
  // endpoint ships — replace `[1]` with `runsQ.data?.versions ?? [1]`.
  const versions = useMemo(() => [1], []);

  const story = (strategy.config?.alpha_story as string | undefined) || '';
  const yaml =
    (strategy.config?.backtest_config_yaml as string | undefined) ||
    (strategy.config?.backtest_config_text as string | undefined) ||
    '';

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <header className="flex flex-col gap-1 border-b border-slate-800 px-3 py-1">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1">
            <TabBtn active={tab === 'story'} onClick={() => setTab('story')}>
              {versions.length > 1 ? `EDITING v${version}` : 'EDITING'}
            </TabBtn>
            <TabBtn active={tab === 'formula'} onClick={() => setTab('formula')}>
              FORMULA
            </TabBtn>
            <TabBtn active={tab === 'config'} onClick={() => setTab('config')}>
              FULL CONFIG
            </TabBtn>
            <TabBtn active={tab === 'raw'} onClick={() => setTab('raw')}>
              RAW JSON
            </TabBtn>
          </div>
          {/* P12 — hide the version selector when only one version exists.
              A select with a single option is visual noise and implies more
              history than the backend actually exposes today. */}
          {versions.length > 1 && (
            <select
              value={version}
              onChange={(e) => {
                const n = Number(e.target.value);
                if (Number.isFinite(n)) setVersion(n);
              }}
              className="rounded border border-slate-700 bg-slate-950 px-2 py-1 text-[10px] font-mono uppercase tracking-wider text-slate-300 hover:bg-slate-900"
              title="Version selector — populates from BacktestRun history when ready"
            >
              {versions.map((v) => (
                <option key={v} value={v}>
                  v{v}
                </option>
              ))}
            </select>
          )}
        </div>
        {/* P11-F2-08 — secondary toolbar chips for cross-page navigation */}
        <SecondaryToolbar strategy={strategy} />
      </header>
      <div className="flex-1 overflow-y-auto p-4">
        {tab === 'story' && (
          <div className="space-y-5">
            {story ? (
              <article className="prose prose-invert prose-sm max-w-none font-mono leading-relaxed text-slate-200 prose-headings:font-mono prose-headings:text-slate-100 prose-h2:text-[12px] prose-h2:font-bold prose-h2:tracking-wider prose-h2:uppercase prose-h2:text-emerald-300 prose-p:font-mono prose-li:font-mono prose-strong:text-slate-100 prose-code:text-emerald-300">
                {/* P31-S3: pass safe markdownComponents to filter javascript:/data: hrefs (XSS defense). */}
                <ReactMarkdown components={markdownComponents} remarkPlugins={remarkMathPlugins} rehypePlugins={rehypeMathPlugins}>{story}</ReactMarkdown>
              </article>
            ) : (
              <div className="text-xs text-slate-500">
                No alpha story yet — the Researcher agent has not run for this strategy.
              </div>
            )}

            <ProvenBacktestResults strategy={strategy} />
            <SourceConcepts strategy={strategy} />

            <div className="border-t border-slate-800 pt-4">
              <div className="mb-3 font-mono text-[10px] font-bold uppercase tracking-widest text-cyan-300">
                ## Team B — Risk Critic ##
              </div>
              <CriticVerdict strategy={strategy} />
            </div>
          </div>
        )}

        {tab === 'config' && (
          <div className="space-y-4">
            <div className="font-mono text-[10px] font-bold uppercase tracking-widest text-cyan-300">
              ## Backtest Config YAML ##
            </div>
            {yaml ? (
              <>
                <pre className="overflow-x-auto rounded border border-slate-800 bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-emerald-200">
                  {yaml}
                </pre>
                {/* P17/B-L1 — standardized-output chip when the YAML parsed cleanly */}
                {!(strategy.config as { config_yaml_invalid?: boolean } | undefined)?.config_yaml_invalid && (
                  <div className="inline-flex items-center gap-1 rounded border border-emerald-700/40 bg-emerald-500/10 px-1.5 py-0.5 font-mono text-[10px] text-emerald-300">
                    <span aria-hidden="true">✓</span>
                    <span>Standardized output — ready for backtest</span>
                  </div>
                )}
              </>
            ) : (
              <div className="text-[11px] text-slate-500">
                Coder agent did not emit a YAML config block for this strategy.
              </div>
            )}
          </div>
        )}

        {tab === 'formula' && (
          <div className="space-y-4">
            <div className="font-mono text-[10px] font-bold uppercase tracking-widest text-cyan-300">
              ## Alpha Formula ##
            </div>
            {strategy.formula_code && strategy.formula_code.trim() ? (
              <pre className="overflow-x-auto rounded border border-slate-800 bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-emerald-300">
                {strategy.formula_code}
              </pre>
            ) : (
              <div className="text-[11px] text-slate-500">
                No formula code yet — the Coder agent has not run for this strategy.
              </div>
            )}
          </div>
        )}

        {tab === 'raw' && (
          <div className="space-y-2">
            <div className="font-mono text-[10px] font-bold uppercase tracking-widest text-cyan-300">
              ## Raw Config (JSON) ##
            </div>
            <pre className="overflow-x-auto rounded border border-slate-800 bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-slate-200">
              {JSON.stringify(strategy.config ?? {}, null, 2)}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}


/**
 * P11-F2-08 — secondary toolbar with quick links to related views. The Recomp
 * button is a stub (confirm dialog) pending a backend reroll endpoint.
 */
function SecondaryToolbar({ strategy }: { strategy: AlphaStrategy }) {
  const chip =
    'rounded border border-slate-700 bg-slate-900/60 px-1.5 py-0.5 font-mono text-[9px] font-bold uppercase tracking-widest text-slate-300 hover:bg-slate-800 hover:text-slate-100';

  // P13 — Recomp is rendered DISABLED until a real backend recompute endpoint
  // ships (no `api.strategyRerun` exists today). The visible-but-disabled
  // chip preserves the layout slot so wiring it later is a one-line change,
  // while the tooltip + opacity tell the operator immediately that nothing
  // happens on click. The previous behaviour was a confirm() that only
  // emitted a console.info — strictly worse than no action at all.
  return (
    <div className="flex flex-wrap items-center gap-1 pt-0.5">
      <Link
        href={`/alpha-genealogy?strategy=${strategy.id}`}
        className={chip}
        title="Jump to the full alpha genealogy view for this strategy"
      >
        Full Alpha
      </Link>
      <Link
        href={`/alpha-genealogy?group=${strategy.id}`}
        className={chip}
        title="Show the live cohort/group surrounding this strategy"
      >
        Live Group
      </Link>
      <button
        type="button"
        disabled
        aria-disabled="true"
        className={`${chip} cursor-not-allowed opacity-50 disabled:hover:bg-slate-900/60 disabled:hover:text-slate-300`}
        title="Recomp — coming soon (backend recompute endpoint pending)"
      >
        Recomp <span className="text-[8px] text-slate-500">(soon)</span>
      </button>
      <Link
        href="/kb-explorer"
        className={chip}
        title="Open the knowledge base explorer"
      >
        KB All
      </Link>
      <Link
        href={`/strategies/${strategy.id}?tab=log`}
        className={chip}
        title="Open the log/output stream for this strategy"
      >
        Log Output
      </Link>
    </div>
  );
}


function TabBtn({
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
      role="tab"
      aria-selected={active}
      className={clsx(
        'rounded-sm px-2 py-1.5 font-mono text-[10px] font-bold uppercase tracking-widest transition-colors',
        active
          ? 'bg-cyan-500/15 text-cyan-300'
          : 'text-slate-500 hover:bg-slate-900 hover:text-slate-300',
      )}
    >
      {children}
    </button>
  );
}


/**
 * Multi-run backtest history. Reads ``strategy.config.backtest_runs`` when the
 * backend supplies it (P6-B: BacktestRun model), otherwise synthesises a
 * single "latest run" row from the current ``strategy.metrics`` snapshot so the
 * shell renders something useful from day one.
 */
function ProvenBacktestResults({ strategy }: { strategy: AlphaStrategy }) {
  const rows = useMemo<BacktestRunRow[]>(() => {
    // C-M13 — runtime guard before accepting the cast. The previous code
    // trusted whatever shape `strategy.config.backtest_runs` carried, which
    // crashes the panel if a stray scalar or string leaks in.
    const raw = (strategy.config as { backtest_runs?: unknown } | undefined)?.backtest_runs;
    if (
      Array.isArray(raw) &&
      raw.length > 0 &&
      raw.every((r) => r != null && typeof r === 'object')
    ) {
      return raw as BacktestRunRow[];
    }
    const m = strategy.metrics ?? {};
    const trades = (strategy.config?.trades as number | undefined) ?? null;
    return [
      {
        end_date: strategy.updated_at?.slice(0, 10) ?? null,
        sharpe: Number.isFinite(Number(m.annualized_sharpe)) ? Number(m.annualized_sharpe) : null,
        win_rate: Number.isFinite(Number(m.win_rate)) ? Number(m.win_rate) : null,
        max_drawdown: Number.isFinite(Number(m.max_drawdown)) ? Number(m.max_drawdown) : null,
        profit_factor: Number.isFinite(Number(m.profit_factor)) ? Number(m.profit_factor) : null,
        trades,
        source: 'latest snapshot',
      },
    ];
  }, [strategy]);

  return (
    <div className="border-t border-slate-800 pt-4">
      <div className="mb-2 font-mono text-[10px] font-bold uppercase tracking-widest text-cyan-300">
        ## Proven Backtest Results ##
      </div>
      <ul className="space-y-1 font-mono text-[11px] leading-relaxed text-slate-300">
        {rows.map((r, i) => (
          <li key={i} className="flex flex-wrap items-baseline gap-x-2">
            <span className="text-slate-500">
              [{r.start_date ?? '…'} → {r.end_date ?? '—'}]
            </span>
            <span>
              Sharpe <span className="text-emerald-300">{fmt(r.sharpe, 2)}</span>
            </span>
            <span>
              · WR <span className="text-cyan-300">{fmtPct(r.win_rate, 1)}</span>
            </span>
            <span>
              · MaxDD <span className="text-rose-300">{fmtPct(r.max_drawdown, 1)}</span>
            </span>
            <span>
              · PF <span className="text-amber-300">{fmt(r.profit_factor, 2)}</span>
            </span>
            {r.trades != null && (
              <span>
                · trades <span className="text-slate-200">{r.trades}</span>
              </span>
            )}
            {r.source && (
              <span className="text-[10px] text-slate-600">({r.source})</span>
            )}
          </li>
        ))}
      </ul>
      {rows.length === 1 && rows[0].source && (
        <div className="mt-2 text-[10px] text-slate-600">
          Showing the latest run only. Multi-run history will appear here once the
          backtest history endpoint is enabled.
        </div>
      )}
    </div>
  );
}


function fmt(v: number | null | undefined, digits = 2): string {
  return v == null || !Number.isFinite(v) ? '—' : v.toFixed(digits);
}


function fmtPct(v: number | null | undefined, digits = 1): string {
  return v == null || !Number.isFinite(v) ? '—' : `${(v * 100).toFixed(digits)}%`;
}


/**
 * P6-M17: reverse-lookup of KnowledgeNodes referenced by this strategy's
 * config.source_node_ids. Renders chip list with one-click jump to the
 * Knowledge Spaces detail. Hidden entirely when no concepts are linked.
 */
function SourceConcepts({ strategy }: { strategy: AlphaStrategy }) {
  const q = useQuery({
    queryKey: ['strategy', strategy.id, 'concepts'],
    queryFn: () => api.strategyConcepts(strategy.id),
    staleTime: 60_000,
    retry: false,
  });
  const concepts = q.data?.concepts ?? [];
  if (q.isLoading || concepts.length === 0) return null;
  return (
    <div className="border-t border-slate-800 pt-4">
      <div className="mb-2 font-mono text-[10px] font-bold uppercase tracking-widest text-cyan-300">
        ## Source Concepts ##
      </div>
      <div className="flex flex-wrap gap-1.5">
        {concepts.map((c) => (
          <Link
            key={c.id}
            href={`/kb-explorer?node=${c.id}`}
            className="inline-flex items-center gap-1 rounded border border-emerald-700/40 bg-emerald-500/10 px-1.5 py-0.5 font-mono text-[10px] text-emerald-200 hover:bg-emerald-500/20"
            title={`${c.kind} · IC ${Number.isFinite(c.ic_score) ? c.ic_score.toFixed(2) : '—'}`}
          >
            K#{c.id} {c.title.length > 32 ? `${c.title.slice(0, 32)}…` : c.title}
          </Link>
        ))}
      </div>
    </div>
  );
}
