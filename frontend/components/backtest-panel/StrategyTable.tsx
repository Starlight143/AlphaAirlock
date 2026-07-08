'use client';

import { useMemo } from 'react';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import type { AlphaStrategy } from '@/lib/api';
import { api } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { ddTone, getTradesCount, qualityLabel, qualityLabelClasses, type QualityLabel } from '@/lib/derive';
import { stageLabel } from '@/lib/stageLabels';

type Props = {
  selectedIds: Set<number>;
  toggle: (id: number) => void;
  // F3 — optional client-side row filter. The parent passes a predicate that
  // matches the current pipeline scope (V2 status buckets). When omitted, all
  // strategies in the cache are shown — preserving the previous behaviour for
  // callers that don't need scope filtering.
  filter?: (s: AlphaStrategy) => boolean;
  // C-M9 — when the parent already owns the strategies array, it can pass it
  // here so we skip our own useQuery roundtrip (TanStack already de-dupes
  // identical queryKeys, but explicit prop-passing is clearer and avoids the
  // refetchInterval doubling up).
  strategies?: AlphaStrategy[];
};

// F3 — stage labels aligned with the V2 7-bucket framing (Stage 0..6).
// Stage 0 = Alpha Ideas, Stage 5 = Live Trade (small-cap + live), Stage 6 =
// Graveyard (rejected / graveyard / paused).
const STAGE_PILL: Record<
  string,
  { label: string; color: string; ring: string }
> = {
  INTAKE:        { label: 'Stage 0', color: 'text-cyan-300',    ring: 'ring-cyan-700/40' },
  STORY_GEN:     { label: 'Stage 1', color: 'text-emerald-300', ring: 'ring-emerald-700/40' },
  CODE_GEN:      { label: 'Stage 2', color: 'text-purple-300',  ring: 'ring-purple-700/40' },
  BACKTESTING:   { label: 'Stage 3', color: 'text-blue-300',    ring: 'ring-blue-700/40' },
  CRITIC_LOOP:   { label: 'Stage 3', color: 'text-blue-300',    ring: 'ring-blue-700/40' },
  APPROVED:      { label: 'Stage 4', color: 'text-amber-300',   ring: 'ring-amber-700/40' },
  PAPER_TRADE:   { label: 'Stage 4', color: 'text-amber-300',   ring: 'ring-amber-700/40' },
  SMALL_CAPITAL: { label: 'Stage 5', color: 'text-orange-300',  ring: 'ring-orange-700/40' },
  LIVE:          { label: 'Stage 5', color: 'text-emerald-300', ring: 'ring-emerald-700/40' },
  REJECTED:      { label: 'Stage 6', color: 'text-rose-300',    ring: 'ring-rose-700/40' },
  GRAVEYARD:     { label: 'Stage 6', color: 'text-slate-400',   ring: 'ring-slate-700/40' },
  PAUSED:        { label: 'Stage 6', color: 'text-slate-400',   ring: 'ring-slate-700/40' },
};

export default function StrategyTable({ selectedIds, toggle, filter, strategies }: Props) {
  // C-M9 — only run the local useQuery when the parent hasn't supplied the
  // strategies prop. Both branches resolve to the same TanStack cache entry
  // so an in-flight parent fetch is reused.
  const q = useQuery({
    queryKey: queryKeys.strategies,
    queryFn: api.strategies,
    refetchInterval: 8_000,
    enabled: strategies === undefined,
  });

  const raw =
    strategies !== undefined
      ? strategies
      : ((q.data?.strategies ?? []) as AlphaStrategy[]);
  const rows = useMemo(
    () => (filter ? raw.filter(filter) : raw),
    [raw, filter],
  );

  // P6-A6: Rank by annualized Sharpe (rows without finite Sharpe rank `null`).
  // Memoised so an N=200 table doesn't re-sort on every cell render. Ranking
  // is computed on the filtered subset so the rank reflects the user's
  // current scope.
  const ranks = useMemo(() => {
    const m = new Map<number, number>();
    const eligible = rows
      .map((s) => ({ id: s.id, sharpe: Number(s.metrics?.annualized_sharpe) }))
      .filter((r) => Number.isFinite(r.sharpe))
      .sort((a, b) => b.sharpe - a.sharpe);
    eligible.forEach((r, idx) => m.set(r.id, idx + 1));
    return m;
  }, [rows]);

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40">
      <header className="flex items-center justify-between border-b border-slate-800 px-4 py-2">
        <h2 className="text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
          Strategies
        </h2>
        <span className="text-[9px] text-slate-500">
          {selectedIds.size} selected
          {filter ? ` · ${rows.length} shown` : ''}
          {' · '}{raw.length} total
        </span>
      </header>

      <div className="flex-1 overflow-y-auto">
        <table className="w-full border-separate border-spacing-0 text-[11px]">
          <thead className="sticky top-0 z-10 bg-slate-900 text-[9px] uppercase tracking-wider text-slate-500">
            <tr>
              <Th className="w-10"></Th>
              <Th>Strategy</Th>
              <Th className="w-28">Stage</Th>
              <Th className="w-20 text-right">Sharpe</Th>
              <Th
                className="w-16 text-right"
                title="Information Ratio — shown only when backend supplies metrics.information_ratio"
              >
                IR
              </Th>
              <Th className="w-20 text-right">MaxDD</Th>
              <Th className="w-20 text-right">Win Rate</Th>
              <Th className="w-16 text-right">Trades</Th>
              <Th className="w-12 text-right">Rank</Th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {rows.map((s) => {
              const pill =
                STAGE_PILL[(s.status || '').toUpperCase()] ?? STAGE_PILL.INTAKE;
              const sharpe = Number(s.metrics?.annualized_sharpe);
              const dd = Number(s.metrics?.max_drawdown);
              const winRate = Number(s.metrics?.win_rate);
              const trades = getTradesCount(s);
              const selected = selectedIds.has(s.id);
              const quality = qualityLabel(Number.isFinite(sharpe) ? sharpe : null);
              return (
                <tr
                  key={s.id}
                  className={clsx(
                    'border-b border-slate-900/50 hover:bg-slate-900/40',
                    selected && 'bg-cyan-500/5',
                  )}
                >
                  <Td>
                    <input
                      type="checkbox"
                      checked={selected}
                      onChange={() => toggle(s.id)}
                      className="h-3 w-3 accent-cyan-500"
                    />
                  </Td>
                  <Td>
                    <div className="flex items-center gap-2">
                      <Link
                        href={`/strategies/${s.id}`}
                        className="text-cyan-300 hover:text-cyan-200"
                      >
                        S#{s.id}
                      </Link>
                      {s.alpha_id && (
                        <span
                          className="shrink-0 rounded border border-cyan-700/40 bg-cyan-500/10 px-1 py-0.5 text-[8px] text-cyan-200"
                          title="Canonical alpha id (stable across renames)"
                        >
                          {s.alpha_id}
                        </span>
                      )}
                      <span className="truncate text-slate-300">{s.name || '—'}</span>
                      {quality.tier !== 'UNKNOWN' && (
                        <span
                          className={clsx(
                            'shrink-0 rounded border px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider',
                            qualityLabelClasses(quality.tone),
                          )}
                          title="Derived from annualized Sharpe"
                        >
                          {quality.text}
                        </span>
                      )}
                    </div>
                  </Td>
                  <Td>
                    <span
                      className={clsx(
                        'rounded px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider ring-1 ring-inset',
                        pill.color,
                        pill.ring,
                      )}
                      title={`${pill.label} · ${stageLabel(s.stage)}`}
                    >
                      {pill.label} · {stageLabel(s.stage)}
                    </span>
                  </Td>
                  <Td className="text-right">
                    {Number.isFinite(sharpe) ? (
                      <span className={sharpeToneClass(quality.tone)}>
                        {sharpe.toFixed(2)}
                      </span>
                    ) : (
                      <span className="text-slate-600">—</span>
                    )}
                  </Td>
                  <Td className="text-right">
                    {(() => {
                      const ir = Number(s.metrics?.information_ratio);
                      if (!Number.isFinite(ir)) return <span className="text-slate-600">—</span>;
                      return (
                        <span
                          className={
                            ir >= 1.0 ? 'text-emerald-300' : ir >= 0 ? 'text-cyan-200' : 'text-rose-300'
                          }
                        >
                          {ir.toFixed(2)}
                        </span>
                      );
                    })()}
                  </Td>
                  <Td className="text-right">
                    {Number.isFinite(dd) ? (
                      <span className={ddToneClass(ddTone(dd))}>
                        {(dd * 100).toFixed(1)}%
                      </span>
                    ) : (
                      <span className="text-slate-600">—</span>
                    )}
                  </Td>
                  <Td className="text-right">
                    {Number.isFinite(winRate) ? (
                      <span
                        className={
                          winRate >= 0.55
                            ? 'text-emerald-300'
                            : winRate >= 0.45
                            ? 'text-cyan-200'
                            : 'text-rose-300'
                        }
                      >
                        {(winRate * 100).toFixed(1)}%
                      </span>
                    ) : (
                      <span className="text-slate-600">—</span>
                    )}
                  </Td>
                  <Td className="text-right">
                    {trades == null ? (
                      <span className="text-slate-600">—</span>
                    ) : (
                      <span className="text-slate-300">{trades}</span>
                    )}
                  </Td>
                  <Td className="text-right">
                    {ranks.has(s.id) ? (
                      <span
                        className="font-mono text-slate-300"
                        title="Rank by annualized Sharpe within current scope"
                      >
                        #{ranks.get(s.id)}
                      </span>
                    ) : (
                      <span className="text-slate-600">—</span>
                    )}
                  </Td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td
                  colSpan={9}
                  className="px-4 py-8 text-center text-xs text-slate-600"
                >
                  {raw.length === 0
                    ? 'No strategies yet — ingest a market commentary to bootstrap.'
                    : 'No strategies in this scope — pick a different tab.'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Th({
  children,
  className,
  title,
}: {
  children?: React.ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <th
      scope="col"
      title={title}
      className={clsx('border-b border-slate-800 px-3 py-2 text-left', className)}
    >
      {children}
    </th>
  );
}

function Td({
  children,
  className,
}: {
  children?: React.ReactNode;
  className?: string;
}) {
  return <td className={clsx('px-3 py-1.5', className)}>{children}</td>;
}

// C-M5 — widened to accept the full QualityLabel['tone'] union so the cast
// `as Exclude<...>` at the call site can go away. 'muted' falls through to
// the slate default branch (same colour we'd render for an unknown tier).
function sharpeToneClass(tone: QualityLabel['tone']): string {
  switch (tone) {
    case 'emerald': return 'text-emerald-300';
    case 'cyan':    return 'text-cyan-200';
    case 'amber':   return 'text-amber-300';
    case 'slate':
    case 'muted':
    default:        return 'text-slate-400';
  }
}

function ddToneClass(t: 'emerald' | 'amber' | 'rose' | 'slate'): string {
  switch (t) {
    case 'emerald': return 'text-emerald-300';
    case 'amber':   return 'text-amber-300';
    case 'rose':    return 'text-rose-300';
    case 'slate':
    default:        return 'text-slate-600';
  }
}
