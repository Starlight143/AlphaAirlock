'use client';

import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { FixedSizeList } from 'react-window';

/** Returns window.innerHeight and keeps it in sync with resize events. SSR-safe (returns 600 until first mount). */
function useWindowHeight(): number {
  const [height, setHeight] = useState<number>(
    typeof window !== 'undefined' ? window.innerHeight : 600,
  );
  useEffect(() => {
    function handleResize() {
      setHeight(window.innerHeight);
    }
    window.addEventListener('resize', handleResize);
    handleResize();
    return () => window.removeEventListener('resize', handleResize);
  }, []);
  return height;
}
import clsx from 'clsx';
import { Loader2, Filter, RefreshCcw } from 'lucide-react';
import { api, ApiError, type TradeTapeRow } from '@/lib/api';

type Props = { strategyId: number };

const ROW_HEIGHT = 22;
const PAGE_LIMIT = 2000;

/**
 * BACKTEST CSV tab — per-bar trade tape served by
 * GET /api/strategies/{id}/trades (P5-BE-04, extended in P8-FIX/H-11).
 *
 * P8-FIX/H-11 adds direction chip (LONG cyan / SHORT rose / FLAT slate),
 * mark_price, and position_delta columns. Older strategy JSON files lacking
 * those fields fall back to derive-from-signal so legacy backtests keep
 * rendering without re-running.
 *
 * - For ≤ 500 rows: render a plain sticky-header table (cheap).
 * - For > 500 rows: switch to react-window for O(viewport) DOM cost.
 * - "Nonzero only" filter is a backend query param.
 */
export default function BacktestCsvTab({ strategyId }: Props) {
  const [nonzeroOnly, setNonzeroOnly] = useState(false);
  const q = useQuery({
    queryKey: ['strategy-trades', strategyId, nonzeroOnly],
    queryFn: () =>
      api.strategyTrades(strategyId, { limit: PAGE_LIMIT, nonzeroOnly }),
    staleTime: 30_000,
  });

  if (q.isLoading) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <Loader2 className="mr-2 h-4 w-4 animate-spin text-cyan-400" />
        <span className="text-xs text-slate-400">Loading trade tape…</span>
      </div>
    );
  }
  if (q.isError) {
    const err = q.error;
    const status = err instanceof ApiError ? err.status : 0;
    const detail =
      err instanceof ApiError ? err.detail : (err as Error)?.message || 'Unknown error';
    const is409 = status === 409;
    return (
      <div className="m-6 rounded-lg border border-amber-700/40 bg-amber-500/10 p-4 text-[12px] text-amber-200">
        <div className="mb-2 font-bold uppercase tracking-widest text-amber-300">
          {is409 ? 'Per-bar tape missing' : 'Failed to load tape'}
        </div>
        <div className="leading-relaxed text-amber-200/80">
          {is409
            ? 'This strategy was backtested before the per-bar tape was persisted. Re-run the pipeline (or trigger a new backtest) to regenerate.'
            : detail}
        </div>
        <button
          onClick={() => q.refetch()}
          className="mt-3 inline-flex items-center gap-1 rounded border border-amber-600/60 px-2 py-1 text-[10px] uppercase tracking-widest text-amber-200 hover:bg-amber-500/15"
        >
          <RefreshCcw className="h-3 w-3" /> Retry
        </button>
      </div>
    );
  }

  const rows: TradeTapeRow[] = q.data?.rows ?? [];
  const total = q.data?.total ?? 0;
  const useVirtual = rows.length > 500;

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-slate-800 bg-slate-950/30 px-4 py-2">
        <div className="font-mono text-[11px] text-slate-400">
          <span className="font-bold text-cyan-300">{rows.length.toLocaleString()}</span>
          {' '}rows shown · <span className="text-slate-500">{total.toLocaleString()}</span> total
          {q.data?.limit && total > q.data.limit && (
            <span className="ml-2 text-amber-300/80">
              (capped at {q.data.limit.toLocaleString()})
            </span>
          )}
        </div>
        <label className="flex items-center gap-2 text-[10px] uppercase tracking-widest text-slate-400">
          <Filter className="h-3 w-3" />
          <input
            type="checkbox"
            checked={nonzeroOnly}
            onChange={(e) => setNonzeroOnly(e.target.checked)}
            className="h-3 w-3 accent-cyan-500"
          />
          Nonzero signal only
        </label>
      </header>
      <div className="flex-1 overflow-hidden">
        <TapeHeader />
        {useVirtual ? <VirtualTable rows={rows} /> : <PlainTable rows={rows} />}
      </div>
    </div>
  );
}

// P8-FIX/H-11: 8 columns — start_time, direction chip, signal, mark_price,
// pos_delta, pnl_pct, cum_pnl_pct, drawdown_pct.
const COL_TEMPLATE = '170px 64px 70px 96px 84px 96px 96px 96px';

function TapeHeader() {
  return (
    <div
      className="sticky top-0 z-10 grid border-b border-slate-800 bg-slate-900/80 px-4 py-1 font-mono text-[10px] text-slate-500"
      style={{ gridTemplateColumns: COL_TEMPLATE }}
    >
      <span>start_time</span>
      <span className="text-center">dir</span>
      <span className="text-right">signal</span>
      <span className="text-right">mark_price</span>
      <span className="text-right">pos_delta</span>
      <span className="text-right">pnl_pct</span>
      <span className="text-right">cum_pnl_pct</span>
      <span className="text-right">drawdown_pct</span>
    </div>
  );
}

function PlainTable({ rows }: { rows: TradeTapeRow[] }) {
  if (rows.length === 0) {
    return (
      <div className="p-6 text-center text-xs text-slate-500">
        No rows to display (try unchecking "Nonzero signal only").
      </div>
    );
  }
  return (
    <div className="h-full overflow-y-auto font-mono text-[11px]">
      {rows.map((r, i) => (
        <TapeRow key={i} row={r} />
      ))}
    </div>
  );
}

function VirtualTable({ rows }: { rows: TradeTapeRow[] }) {
  const windowHeight = useWindowHeight();
  return (
    <div className="h-full">
      <FixedSizeList
        height={Math.max(200, windowHeight - 280)}
        itemCount={rows.length}
        itemSize={ROW_HEIGHT}
        width="100%"
        className="font-mono text-[11px]"
      >
        {({ index, style }) => (
          <div style={style}>
            <TapeRow row={rows[index]} />
          </div>
        )}
      </FixedSizeList>
    </div>
  );
}

function DirectionChip({ direction }: { direction: 'long' | 'short' | 'flat' }) {
  const styles =
    direction === 'long'
      ? 'border-cyan-600/70 bg-cyan-500/15 text-cyan-200'
      : direction === 'short'
      ? 'border-rose-600/70 bg-rose-500/15 text-rose-200'
      : 'border-slate-700 bg-slate-900 text-slate-500';
  return (
    <span
      className={`inline-flex items-center justify-center rounded border px-1 text-[9px] font-bold uppercase tracking-widest ${styles}`}
    >
      {direction}
    </span>
  );
}

function TapeRow({ row }: { row: TradeTapeRow }) {
  const pnlTone =
    row.pnl_pct > 0
      ? 'text-emerald-300'
      : row.pnl_pct < 0
      ? 'text-rose-300'
      : 'text-slate-500';
  const sigTone =
    row.signal > 0
      ? 'text-cyan-200'
      : row.signal < 0
      ? 'text-rose-200'
      : 'text-slate-500';
  const dir: 'long' | 'short' | 'flat' =
    row.direction ?? (row.signal > 0 ? 'long' : row.signal < 0 ? 'short' : 'flat');
  const mark = Number(row.mark_price);
  const posDelta = Number(row.position_delta);
  return (
    <div
      className="grid border-b border-slate-900/40 px-4 py-0.5 hover:bg-slate-900/40"
      style={{ gridTemplateColumns: COL_TEMPLATE }}
    >
      <span className="text-slate-300">{row.start_time}</span>
      <span className="flex justify-center"><DirectionChip direction={dir} /></span>
      <span className={clsx('text-right', sigTone)}>{fmtSig(row.signal)}</span>
      <span className="text-right text-slate-400">
        {Number.isFinite(mark) && mark !== 0 ? mark.toFixed(2) : '—'}
      </span>
      <span className="text-right text-amber-300/80">
        {Number.isFinite(posDelta) ? fmtSig(posDelta, 3) : '—'}
      </span>
      <span className={clsx('text-right', pnlTone)}>{fmtPct(row.pnl_pct, 3)}</span>
      <span className="text-right text-slate-200">{fmtPct(row.cum_pnl_pct, 2)}</span>
      <span className="text-right text-rose-300">{fmtPct(row.drawdown_pct, 2)}</span>
    </div>
  );
}

function fmtSig(v: number, digits = 2): string {
  if (!Number.isFinite(v)) return '—';
  if (v === 0) return '0';
  return v > 0 ? `+${v.toFixed(digits)}` : v.toFixed(digits);
}

function fmtPct(v: number, digits = 2): string {
  if (!Number.isFinite(v)) return '—';
  const pct = v * 100;
  const sign = pct > 0 ? '+' : '';
  return `${sign}${pct.toFixed(digits)}%`;
}
