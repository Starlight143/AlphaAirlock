'use client';

import { useMemo, useState } from 'react';
import { useQueries, useQuery } from '@tanstack/react-query';
import { Sparkles, Trash2 } from 'lucide-react';
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip,
  CartesianGrid, AreaChart, Area,
} from 'recharts';

import { api, type AlphaStrategy } from '@/lib/api';
import { queryKeys } from '@/lib/query';
// P15/C-M7 — pull trackingError from the shared derive module so the local
// implementation can be deleted (was an exact duplicate of the lib version).
import { alignReturns, pearsonCorrelation, normalizeToBase100, trackingError } from '@/lib/derive';

const MAX_SLOTS = 4;
const SLOT_COLORS = ['#22d3ee', '#34d399', '#a78bfa', '#fbbf24'] as const;

/**
 * /arena — P7-04, extended to N-way comparison (P8-FIX/M-9).
 *
 * The page now hosts up to 4 strategy slots. All cross-stats generalise to
 * the multi-strategy case:
 *   - correlation becomes an NxN heatmap (table-based; no D3 dep).
 *   - combined Sharpe uses an equal-weight portfolio across the selected set
 *     and is computed inline against the inner-joined daily-return matrix.
 *   - the equity / drawdown overlays render one line per slot.
 */
export default function ArenaPage() {
  // NOTE: must use raw `api.strategies` (not `.then((r) => r.strategies)`) so
  // every other consumer sharing `queryKeys.strategies` sees the same cached
  // `{ strategies: [...] }` shape. Flattening here would corrupt the cache for
  // pages like /paper-trade, /backtest-panel, etc. (and vice-versa).
  const listQ = useQuery({ queryKey: queryKeys.strategies, queryFn: api.strategies });
  // P15/C-H2 + C-L9 — extend the eligible set with PAUSED + GRAVEYARD so
  // operators can post-mortem retired strategies against current LIVE ones.
  // Defensive .toUpperCase() guards against future serialization changes.
  const eligible = (listQ.data?.strategies ?? []).filter((s) =>
    ['APPROVED', 'PAPER_TRADE', 'SMALL_CAPITAL', 'LIVE', 'REJECTED', 'PAUSED', 'GRAVEYARD']
      .includes((s.status || '').toUpperCase()),
  );

  // P15/C-H1 — keep selectedIds as a sparse fixed-length array. Compacting it
  // (previous implementation) caused slot↔color drift: picking slots 0+2 used
  // to render as positions 0+1 against SLOT_COLORS, so slot 2 lit up the slot-1
  // color (emerald) instead of the slot-2 color (violet). The sparse array
  // keeps `null` for empty slots so SLOT_COLORS[slot] is always correct.
  const [selectedIds, setSelectedIds] = useState<(number | null)[]>(
    Array(MAX_SLOTS).fill(null),
  );

  const detailQs = useQueries({
    queries: selectedIds.map((id) =>
      id != null
        ? { queryKey: queryKeys.strategy(id), queryFn: () => api.strategy(id) }
        // Disabled noop query so the hook order stays stable across renders
        // even when slots are empty (useQueries reads queries.length).
        : {
            queryKey: ['noop', 'arena'] as const,
            queryFn: () => Promise.resolve(null as unknown as AlphaStrategy),
            enabled: false,
          },
    ),
  });
  // Derive the (slot, strat) pairs so downstream consumers can index back into
  // SLOT_COLORS using the ORIGINAL slot number, not the post-filter position.
  // Wrapped in useMemo so the derived arrays are referentially stable: without
  // this, selectedStrats is a new array instance on every render, which defeats
  // the useMemo on buildReturnMatrix (O(S×D) computation).
  const { selectedStrats, selectedSlots } = useMemo(() => {
    const pairs = selectedIds
      .map((id, slot) => ({ slot, strat: id != null ? (detailQs[slot]?.data as AlphaStrategy | null) : null }))
      .filter((x): x is { slot: number; strat: AlphaStrategy } => x.strat != null);
    return { selectedStrats: pairs.map((x) => x.strat), selectedSlots: pairs.map((x) => x.slot) };
  }, [selectedIds, detailQs]);

  // Inner-join all equity curves on the YYYY-MM-DD prefix, then derive daily
  // simple returns. The matrix is `numStrategies x numDays`. Empty when fewer
  // than 2 strategies are loaded.
  const matrix = useMemo(() => buildReturnMatrix(selectedStrats), [selectedStrats]);

  // P15/C-H1 — only count duplicates among the currently-selected (non-null)
  // ids; ignore the empty-slot sentinels.
  const dupes = countDuplicates(selectedIds.filter((id): id is number => id != null));

  return (
    <div className="flex h-full w-full flex-col gap-3 overflow-y-auto p-3">
      <header className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
        <div className="mb-2 flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
          <Sparkles className="h-3.5 w-3.5 text-cyan-400" /> Arena · N-way Comparison
        </div>
        <div className="grid grid-cols-4 gap-3">
          {Array.from({ length: MAX_SLOTS }).map((_, slot) => (
            <Picker
              key={slot}
              slot={slot}
              value={selectedIds[slot] ?? null}
              color={SLOT_COLORS[slot]}
              onChange={(v) => {
                // P15/C-H1 — sparse-array set: keep fixed length and write the
                // value at the exact slot index so slot→color mapping never
                // drifts. null clears the slot without compacting.
                setSelectedIds((prev) => {
                  const next = prev.slice(0, MAX_SLOTS);
                  while (next.length < MAX_SLOTS) next.push(null);
                  next[slot] = v;
                  return next;
                });
              }}
              options={eligible}
            />
          ))}
        </div>
        {dupes > 0 && (
          <div className="mt-3 rounded-md border border-rose-700/40 bg-rose-500/10 px-2 py-1.5 text-[11px] text-rose-200">
            Duplicate strategies selected — each slot must be unique.
          </div>
        )}
      </header>

      {selectedStrats.length < 2 && (
        <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-6 text-center text-[12px] text-slate-500">
          Pick at least two strategies to begin (up to {MAX_SLOTS}).
        </div>
      )}

      {selectedStrats.length >= 2 && dupes === 0 && (
        <>
          <CrossStats strategies={selectedStrats} matrix={matrix} />
          <div className="grid grid-cols-12 gap-3">
            <Card title="Stat Scoreboard" className="col-span-5">
              <Scoreboard strategies={selectedStrats} slots={selectedSlots} />
            </Card>
            <Card title="Equity Overlay (base 100)" className="col-span-7">
              <EquityOverlay strategies={selectedStrats} slots={selectedSlots} />
            </Card>
            <Card title="Drawdown Overlay" className="col-span-7">
              <DrawdownOverlay strategies={selectedStrats} slots={selectedSlots} />
            </Card>
            <Card title="Correlation Matrix" className="col-span-5">
              <CorrelationHeatmap strategies={selectedStrats} slots={selectedSlots} matrix={matrix} />
            </Card>
          </div>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pickers
// ---------------------------------------------------------------------------

function Picker({
  slot,
  value,
  color,
  onChange,
  options,
}: {
  slot: number;
  value: number | null;
  color: string;
  onChange: (v: number | null) => void;
  options: AlphaStrategy[];
}) {
  return (
    <label className="block">
      <div className="mb-0.5 flex items-center justify-between text-[9px] uppercase tracking-wider">
        <span className="flex items-center gap-1 text-slate-500">
          <span className="inline-block h-2 w-2 rounded-full" style={{ background: color }} />
          Strategy {String.fromCharCode(65 + slot)}
        </span>
        {value != null && (
          <button
            onClick={() => onChange(null)}
            className="inline-flex items-center gap-0.5 text-slate-500 hover:text-rose-300"
            title="Clear slot"
          >
            <Trash2 className="h-2.5 w-2.5" /> clear
          </button>
        )}
      </div>
      <select
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}
        className="w-full rounded-md border border-slate-700 bg-slate-950 px-2 py-1 text-[11px] text-slate-200"
      >
        <option value="">— pick a strategy —</option>
        {options.map((s) => {
          const rawSh = Number(s.metrics?.annualized_sharpe);
          const sh = Number.isFinite(rawSh) ? rawSh.toFixed(2) : '—';
          return (
            <option key={s.id} value={s.id}>
              S#{s.id} {(s.slug ?? s.name).slice(0, 42)} (Sh {sh}, {s.status})
            </option>
          );
        })}
      </select>
    </label>
  );
}

// ---------------------------------------------------------------------------
// Cross-stats — generalised to N strategies
// ---------------------------------------------------------------------------

type ReturnMatrix = {
  strategyIds: number[];
  dates: string[];
  /** returns[strategyIndex][dayIndex] */
  returns: number[][];
};

function buildReturnMatrix(strats: AlphaStrategy[]): ReturnMatrix {
  if (strats.length < 2) {
    return { strategyIds: strats.map((s) => s.id), dates: [], returns: strats.map(() => []) };
  }
  const curves = strats.map((s) => s.equity_curve ?? []);
  // First sweep: build maps of YYYY-MM-DD → equity per strategy.
  const equities = curves.map((curve) => {
    const m = new Map<string, number>();
    for (const p of curve) m.set(p.timestamp.slice(0, 10), p.equity);
    return m;
  });
  // Intersect date sets.
  const datesIntersect = (() => {
    if (equities.length === 0) return [] as string[];
    const sets = equities.map((m) => new Set(m.keys()));
    const base = sets[0];
    const inAll: string[] = [];
    for (const d of base) {
      let ok = true;
      for (let i = 1; i < sets.length; i++) {
        if (!sets[i].has(d)) { ok = false; break; }
      }
      if (ok) inAll.push(d);
    }
    inAll.sort();
    return inAll;
  })();

  // Derive daily simple returns for every strategy in lockstep.
  const returns: number[][] = curves.map(() => []);
  for (let i = 1; i < datesIntersect.length; i++) {
    const prev = datesIntersect[i - 1];
    const cur = datesIntersect[i];
    for (let k = 0; k < equities.length; k++) {
      const prevEq = equities[k].get(prev);
      const curEq = equities[k].get(cur);
      if (prevEq == null || curEq == null || !(prevEq > 1e-14)) {
        returns[k].push(0);
        continue;
      }
      returns[k].push(curEq / prevEq - 1);
    }
  }
  return {
    strategyIds: strats.map((s) => s.id),
    dates: datesIntersect.slice(1),
    returns,
  };
}

function correlationMatrix(matrix: ReturnMatrix): (number | null)[][] {
  const n = matrix.returns.length;
  const out: (number | null)[][] = Array.from({ length: n }, () => Array(n).fill(null));
  for (let i = 0; i < n; i++) {
    out[i][i] = 1;
    for (let j = i + 1; j < n; j++) {
      const r = pearsonCorrelation(matrix.returns[i], matrix.returns[j]);
      out[i][j] = r;
      out[j][i] = r;
    }
  }
  return out;
}

function combinedSharpeN(matrix: ReturnMatrix): number | null {
  const n = matrix.returns.length;
  const days = matrix.returns[0]?.length ?? 0;
  if (n < 2 || days < 2) return null;
  const w = 1 / n;
  const port: number[] = [];
  for (let d = 0; d < days; d++) {
    let r = 0;
    for (let k = 0; k < n; k++) r += w * matrix.returns[k][d];
    port.push(r);
  }
  const mean = port.reduce((a, b) => a + b, 0) / port.length;
  const variance =
    port.reduce((a, b) => a + (b - mean) * (b - mean), 0) / Math.max(1, port.length - 1);
  const std = Math.sqrt(variance);
  if (!(std > 1e-14)) return null;
  return Math.max(-25, Math.min(25, (mean / std) * Math.sqrt(252)));
}

// ---------------------------------------------------------------------------
// View components
// ---------------------------------------------------------------------------

function CrossStats({
  strategies,
  matrix,
}: {
  strategies: AlphaStrategy[];
  matrix: ReturnMatrix;
}) {
  const combined = combinedSharpeN(matrix);
  // Mean pairwise correlation excludes the diagonal.
  const mat = correlationMatrix(matrix);
  let sum = 0;
  let count = 0;
  for (let i = 0; i < mat.length; i++) {
    for (let j = i + 1; j < mat.length; j++) {
      const v = mat[i][j];
      if (v != null && Number.isFinite(v)) { sum += v; count += 1; }
    }
  }
  const meanCorr = count > 0 ? sum / count : null;
  const tone = (v: number | null) => (v == null
    ? 'text-slate-500'
    : v >= 0.7 ? 'text-rose-300'
    : v >= 0.3 ? 'text-amber-300'
    : 'text-emerald-300');

  // Equal-weight pairwise alignment for tracking error proxy uses slots 0,1.
  // For N>2 we still show pairwise tracking error between A↔B for orientation;
  // the matrix below gives the operator the full picture.
  const trackingPair =
    strategies.length >= 2 ? alignReturns(strategies[0].equity_curve ?? [], strategies[1].equity_curve ?? []) : null;
  const te = trackingPair && trackingPair.retA.length >= 2 ? trackingError(trackingPair.retA, trackingPair.retB) : null;

  return (
    <div className="grid grid-cols-4 gap-3">
      <Tile label={`Mean ρ (${count} pairs)`} value={meanCorr != null ? meanCorr.toFixed(3) : '—'} cls={tone(meanCorr)} />
      <Tile label="Combined Sharpe (EW)" value={combined != null ? combined.toFixed(2) : '—'} cls="text-cyan-200" />
      {/* P15/C-M6 — for exactly 2 slots show the A↔B tracking error; for N>2
          the single-pair number is misleading (operators expect "across all
          selected"), so swap in a smaller "see matrix" hint with the slot
          count. The pairwise breakdown is in the correlation table below. */}
      {strategies.length === 2 ? (
        <Tile label="A↔B Tracking Error" value={te != null ? `${(te * 100).toFixed(1)}%` : '—'} cls="text-amber-200" />
      ) : (
        <Tile label="Pairwise TE (see matrix)" value={`${strategies.length} strats`} cls="text-slate-400" />
      )}
      <Tile label="Slots" value={`${strategies.length}/${MAX_SLOTS}`} cls="text-slate-200" />
    </div>
  );
}

function Tile({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
      <div className="text-[9px] uppercase tracking-widest text-slate-500">{label}</div>
      <div className={`mt-1 font-mono text-2xl ${cls ?? 'text-slate-200'}`}>{value}</div>
    </div>
  );
}

function Card({ title, className = '', children }: { title: string; className?: string; children: React.ReactNode }) {
  return (
    <section className={`flex flex-col rounded-xl border border-slate-800 bg-slate-900/40 p-3 ${className}`}>
      <header className="mb-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">{title}</header>
      <div className="flex-1 min-h-[200px]">{children}</div>
    </section>
  );
}

const METRIC_ROWS: { key: string; label: string; higher: boolean; fmt: (v: number) => string }[] = [
  { key: 'annualized_sharpe', label: 'Sharpe', higher: true, fmt: (v) => v.toFixed(2) },
  { key: 'max_drawdown', label: 'Max DD', higher: false, fmt: (v) => `${(v * 100).toFixed(1)}%` },
  { key: 'cumulative_return', label: 'Cum Return', higher: true, fmt: (v) => `${(v * 100).toFixed(1)}%` },
  { key: 'win_rate', label: 'Win Rate', higher: true, fmt: (v) => `${(v * 100).toFixed(1)}%` },
  { key: 'profit_factor', label: 'Profit Factor', higher: true, fmt: (v) => v.toFixed(2) },
];

function Scoreboard({ strategies, slots }: { strategies: AlphaStrategy[]; slots: number[] }) {
  // P15/C-H1 — index colors via the original slot so picking slots 0+2 shows
  // cyan + violet, not cyan + emerald (which was the pre-patch bug).
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[11px]">
        <thead className="text-[9px] uppercase tracking-wider text-slate-500">
          <tr>
            <th scope="col" className="text-left">Metric</th>
            {strategies.map((s, i) => (
              <th scope="col" key={s.id} className="text-right">
                <span className="inline-flex items-center gap-1">
                  <span className="inline-block h-2 w-2 rounded-full" style={{ background: SLOT_COLORS[slots[i]] }} />
                  S#{s.id}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="font-mono">
          {METRIC_ROWS.map((row) => {
            const values = strategies.map((s) => {
              const n = Number(s.metrics?.[row.key]);
              return Number.isFinite(n) ? n : null;
            });
            const finite = values.filter((v): v is number => v != null);
            const best = finite.length
              ? (row.higher ? Math.max(...finite) : Math.min(...finite))
              : null;
            return (
              <tr key={row.key} className="border-t border-slate-900/60">
                <th scope="row" className="py-1 text-left font-normal text-slate-200">{row.label}</th>
                {values.map((v, i) => (
                  <td key={i} className={`text-right ${v != null && v === best ? 'text-emerald-300' : 'text-slate-400'}`}>
                    {v == null ? '—' : row.fmt(v)}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function EquityOverlay({ strategies, slots }: { strategies: AlphaStrategy[]; slots: number[] }) {
  const series = strategies.map((s) => normalizeToBase100(s.equity_curve ?? []));
  // Build per-series lookup maps and compute the intersection of dates so that
  // every row in `data` has a value for every strategy. A union merge would
  // leave some s{i} keys undefined on dates only one strategy covers, causing
  // Recharts to render broken / gapped lines.
  const seriesMaps = series.map((points) => {
    const m = new Map<string, number>();
    for (const p of points) m.set(p.ts, p.v);
    return m;
  });
  const intersectedDates = (() => {
    if (seriesMaps.length === 0) return [] as string[];
    const base = seriesMaps[0];
    const result: string[] = [];
    for (const ts of base.keys()) {
      if (seriesMaps.every((m) => m.has(ts))) result.push(ts);
    }
    result.sort();
    return result;
  })();
  const data = intersectedDates.map((ts) => {
    const row: Record<string, number | string> = { date: ts };
    seriesMaps.forEach((m, i) => { row[`s${i}`] = m.get(ts) as number; });
    return row;
  });
  if (!data.length) return <Empty>No overlapping dates.</Empty>;
  return (
    <ResponsiveContainer width="100%" height={250}>
      <LineChart data={data}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis dataKey="date" stroke="#475569" tick={{ fontSize: 9 }} minTickGap={40} />
        <YAxis stroke="#475569" tick={{ fontSize: 9 }} />
        <Tooltip contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 10 }} />
        {strategies.map((s, i) => (
          <Line
            key={s.id}
            type="monotone"
            dataKey={`s${i}`}
            stroke={SLOT_COLORS[slots[i]]}
            dot={false}
            strokeWidth={2}
            name={`S#${s.id}`}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}

function DrawdownOverlay({ strategies, slots }: { strategies: AlphaStrategy[]; slots: number[] }) {
  // Mirror EquityOverlay's intersection logic: build one seriesMap per strategy
  // keyed by YYYY-MM-DD date string, intersect all key sets, then build `data`
  // only from the shared dates so every row has a value for every s{i} key.
  const seriesMaps = strategies.map((s) => {
    const m = new Map<string, number>();
    for (const p of s.equity_curve ?? []) m.set(p.timestamp.slice(0, 10), p.drawdown);
    return m;
  });
  const intersectedDates = (() => {
    if (!seriesMaps.length) return [] as string[];
    const base = seriesMaps[0];
    const result: string[] = [];
    for (const d of base.keys()) {
      if (seriesMaps.every((m) => m.has(d))) result.push(d);
    }
    result.sort();
    return result;
  })();
  const data = intersectedDates.map((d) => {
    const row: Record<string, number | string> = { date: d };
    seriesMaps.forEach((m, i) => { row[`s${i}`] = m.get(d) as number; });
    return row;
  });
  if (!data.length) return <Empty>No overlapping dates.</Empty>;
  return (
    <ResponsiveContainer width="100%" height={220}>
      <AreaChart data={data}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis dataKey="date" stroke="#475569" tick={{ fontSize: 9 }} minTickGap={40} />
        <YAxis stroke="#475569" tick={{ fontSize: 9 }} />
        <Tooltip contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 10 }} />
        {strategies.map((s, i) => (
          <Area
            key={s.id}
            type="monotone"
            dataKey={`s${i}`}
            stroke={SLOT_COLORS[slots[i]]}
            fill={SLOT_COLORS[slots[i]]}
            fillOpacity={0.18}
            name={`S#${s.id}`}
          />
        ))}
      </AreaChart>
    </ResponsiveContainer>
  );
}

function CorrelationHeatmap({
  strategies,
  slots,
  matrix,
}: {
  strategies: AlphaStrategy[];
  slots: number[];
  matrix: ReturnMatrix;
}) {
  const mat = correlationMatrix(matrix);
  if (matrix.returns[0]?.length === 0) {
    return <Empty>No overlapping dates yet — pick strategies with shared history.</Empty>;
  }
  const cellColor = (v: number | null) => {
    if (v == null || !Number.isFinite(v)) return 'rgba(71,85,105,0.4)';
    if (v >= 0.7) return 'rgba(244,63,94,0.55)';   // rose — highly correlated
    if (v >= 0.3) return 'rgba(245,158,11,0.5)';   // amber
    if (v > -0.3) return 'rgba(16,185,129,0.4)';   // emerald — uncorrelated
    if (v > -0.7) return 'rgba(34,211,238,0.5)';   // cyan — mild inverse
    return 'rgba(168,85,247,0.55)';                // purple — strong inverse
  };
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-[10px]">
        <thead>
          <tr>
            <th scope="col" className="px-2 py-1 text-left text-slate-500">·</th>
            {strategies.map((s, i) => (
              <th
                scope="col"
                key={s.id}
                className="px-2 py-1 text-center font-mono"
                style={{ color: SLOT_COLORS[slots[i]] }}
              >
                S#{s.id}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {strategies.map((s, i) => (
            <tr key={s.id}>
              <th
                scope="row"
                className="px-2 py-1 text-left font-mono"
                style={{ color: SLOT_COLORS[slots[i]] }}
              >
                S#{s.id}
              </th>
              {strategies.map((_, j) => {
                const v = mat[i]?.[j] ?? null;
                return (
                  <td
                    key={j}
                    className="px-2 py-1 text-center font-mono text-slate-100"
                    style={{ background: cellColor(v) }}
                    title={v != null ? `ρ(S#${s.id}, S#${strategies[j].id}) = ${v.toFixed(3)}` : 'n/a'}
                  >
                    {v != null && Number.isFinite(v) ? v.toFixed(2) : '—'}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full min-h-[120px] items-center justify-center text-center text-[11px] text-slate-500">
      {children}
    </div>
  );
}

function countDuplicates(ids: number[]): number {
  const seen = new Set<number>();
  let dupes = 0;
  for (const id of ids) {
    if (id == null) continue;
    if (seen.has(id)) dupes++;
    seen.add(id);
  }
  return dupes;
}

