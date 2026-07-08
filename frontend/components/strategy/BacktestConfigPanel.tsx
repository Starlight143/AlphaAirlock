'use client';

import type { ReactNode } from 'react';

/**
 * Structured Backtest Config panel — P8-FIX/H-8.
 *
 * The researcher persists ``config.backtest_config`` as a parsed YAML dict
 * (see :mod:`backend.agents.researcher`). The reference video shows the
 * operator scanning *what data is used*, *long vs. short bias*, *entry logic*,
 * *model name*, and *parameters* at a glance — not reading raw YAML.
 *
 * This panel surfaces those five buckets with field-aware formatting and
 * falls back to a "Researcher did not emit a config block" placeholder when
 * the dict is empty.
 *
 * The raw YAML is still rendered by the parent's ``<details>`` block below
 * the panel so power-users can audit the source-of-truth.
 */
export default function BacktestConfigPanel({
  config,
}: {
  config: Record<string, unknown> | null;
}) {
  if (!config || Object.keys(config).length === 0) {
    return (
      <div className="rounded border border-slate-800 bg-slate-950/40 p-3 text-[11px] text-slate-500">
        Researcher did not emit a structured backtest_config block.
      </div>
    );
  }
  const dataSources = readList(config, ['data_sources', 'datasources', 'data']);
  const direction = readScalar(config, ['direction', 'side', 'bias']);
  const entryLogic = readScalar(config, ['entry_logic', 'entry', 'entry_rule']);
  const exitLogic = readScalar(config, ['exit_logic', 'exit', 'exit_rule']);
  const model = readScalar(config, ['model', 'signal_model', 'method']);
  const parameters = readDict(config, ['parameters', 'params']);
  const symbols = readList(config, ['symbols', 'tickers', 'assets']);
  const timeframe = readScalar(config, ['timeframe', 'tf', 'bar', 'interval']);

  return (
    <div className="space-y-2">
      <Card title="Data Sources" tone="cyan">
        {dataSources.length === 0 ? (
          <span className="text-slate-500">—</span>
        ) : (
          <div className="flex flex-wrap gap-1">
            {dataSources.map((d, i) => (
              <Chip key={`${d}-${i}`} tone="cyan">{d}</Chip>
            ))}
          </div>
        )}
      </Card>
      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
        <Card title="Direction" tone="amber">
          {direction ? <Chip tone="amber">{direction}</Chip> : <span className="text-slate-500">—</span>}
        </Card>
        <Card title="Model" tone="emerald">
          {model ? <Chip tone="emerald">{model}</Chip> : <span className="text-slate-500">—</span>}
        </Card>
      </div>
      <Card title="Entry & Exit" tone="slate">
        <div className="grid grid-cols-1 gap-1 md:grid-cols-2">
          <Field label="Entry" value={entryLogic} />
          <Field label="Exit" value={exitLogic} />
        </div>
      </Card>
      <Card title="Parameters" tone="purple">
        {parameters.length === 0 ? (
          <span className="text-slate-500">—</span>
        ) : (
          <div className="grid grid-cols-2 gap-1 font-mono text-[10px] md:grid-cols-3">
            {parameters.map(([k, v]) => (
              <div
                key={k}
                className="flex items-center justify-between rounded border border-slate-800 bg-slate-900/60 px-2 py-0.5"
              >
                <span className="text-slate-400">{k}</span>
                <span className="text-purple-300">{formatValue(v)}</span>
              </div>
            ))}
          </div>
        )}
      </Card>
      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
        <Card title="Symbols" tone="slate">
          {symbols.length === 0 ? (
            <span className="text-slate-500">—</span>
          ) : (
            <div className="flex flex-wrap gap-1">
              {symbols.map((s, i) => (
                <Chip key={`${s}-${i}`} tone="slate">{s}</Chip>
              ))}
            </div>
          )}
        </Card>
        <Card title="Timeframe" tone="slate">
          {timeframe ? <Chip tone="slate">{timeframe}</Chip> : <span className="text-slate-500">—</span>}
        </Card>
      </div>
    </div>
  );
}

// ---- helpers ---------------------------------------------------------------

function Card({
  title,
  tone,
  children,
}: {
  title: string;
  tone: 'cyan' | 'amber' | 'emerald' | 'purple' | 'slate';
  children: ReactNode;
}) {
  const accent =
    tone === 'cyan'
      ? 'text-cyan-300'
      : tone === 'amber'
      ? 'text-amber-300'
      : tone === 'emerald'
      ? 'text-emerald-300'
      : tone === 'purple'
      ? 'text-purple-300'
      : 'text-slate-400';
  return (
    <div className="rounded border border-slate-800 bg-slate-950/40 p-2">
      <div className={`mb-1 font-mono text-[9px] font-bold uppercase tracking-widest ${accent}`}>
        {title}
      </div>
      <div className="text-[11px] text-slate-200">{children}</div>
    </div>
  );
}

function Chip({
  children,
  tone,
}: {
  children: ReactNode;
  tone: 'cyan' | 'amber' | 'emerald' | 'purple' | 'slate';
}) {
  const styles =
    tone === 'cyan'
      ? 'border-cyan-700/60 bg-cyan-500/10 text-cyan-200'
      : tone === 'amber'
      ? 'border-amber-700/60 bg-amber-500/10 text-amber-200'
      : tone === 'emerald'
      ? 'border-emerald-700/60 bg-emerald-500/10 text-emerald-200'
      : tone === 'purple'
      ? 'border-purple-700/60 bg-purple-500/10 text-purple-200'
      : 'border-slate-700 bg-slate-900/60 text-slate-200';
  return (
    <span className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${styles}`}>
      {children}
    </span>
  );
}

function Field({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="rounded border border-slate-800 bg-slate-900/40 px-2 py-1 text-[10px]">
      <div className="font-mono uppercase tracking-widest text-slate-500">{label}</div>
      <div className="mt-0.5 font-mono text-[11px] text-slate-200">
        {value && value.trim() ? value : <span className="text-slate-500">—</span>}
      </div>
    </div>
  );
}

function readScalar(obj: Record<string, unknown>, keys: string[]): string | null {
  for (const k of keys) {
    const v = obj[k];
    if (v == null) continue;
    if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
      return String(v);
    }
  }
  return null;
}

function readList(obj: Record<string, unknown>, keys: string[]): string[] {
  for (const k of keys) {
    const v = obj[k];
    if (Array.isArray(v)) return v.map((x) => String(x));
    if (typeof v === 'string') {
      return v.split(',').map((x) => x.trim()).filter(Boolean);
    }
  }
  return [];
}

function readDict(obj: Record<string, unknown>, keys: string[]): [string, unknown][] {
  for (const k of keys) {
    const v = obj[k];
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      return Object.entries(v as Record<string, unknown>);
    }
  }
  return [];
}

function formatValue(v: unknown): string {
  if (v == null) return '—';
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(3);
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (Array.isArray(v)) return `[${v.length}]`;
  if (typeof v === 'object') return JSON.stringify(v).slice(0, 24);
  return String(v);
}
