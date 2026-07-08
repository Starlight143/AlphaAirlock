'use client';

import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid,
  LineChart, Line, ReferenceLine, ComposedChart, Cell,
} from 'recharts';
import { LineChart as LineIcon, RefreshCcw } from 'lucide-react';

import { api, type IrBookResponse } from '@/lib/api';
import { queryKeys } from '@/lib/query';

const ALL_STATUSES = ['APPROVED', 'PAPER_TRADE', 'SMALL_CAPITAL', 'LIVE'] as const;

/**
 * /ir-explorer — P7-06
 * Information-ratio aggregation across the strategy book.
 */
export default function IrExplorerPage() {
  const [statuses, setStatuses] = useState<string[]>([...ALL_STATUSES]);
  const [benchmark, setBenchmark] = useState<'btc' | 'none'>('btc');
  const [rollingWindow, setRollingWindow] = useState<number>(30);
  const csv = [...statuses].sort().join(',');

  const hasStatuses = statuses.length > 0;
  const bookQ = useQuery({ queryKey: queryKeys.irBook(csv, benchmark), queryFn: () => api.irBook(statuses, benchmark), enabled: hasStatuses });
  const catQ = useQuery({ queryKey: queryKeys.irByCat(csv, benchmark), queryFn: () => api.irByCategory(statuses, benchmark), enabled: hasStatuses });
  const regQ = useQuery({ queryKey: queryKeys.irByRegime(csv, benchmark), queryFn: () => api.irByRegime(statuses, benchmark), enabled: hasStatuses });
  const assetQ = useQuery({ queryKey: queryKeys.irByAsset(csv, benchmark), queryFn: () => api.irByAsset(statuses, benchmark), enabled: hasStatuses });
  const rollQ = useQuery({ queryKey: queryKeys.irRolling(csv, benchmark, rollingWindow), queryFn: () => api.irRolling(statuses, benchmark, rollingWindow), enabled: hasStatuses });
  const wfQ = useQuery({ queryKey: queryKeys.irWaterfall(csv, benchmark), queryFn: () => api.irWaterfall(statuses, benchmark), enabled: hasStatuses });

  return (
    <div className="flex h-full w-full flex-col gap-3 overflow-y-auto p-3">
      <header className="flex items-center justify-between rounded-xl border border-slate-800 bg-slate-900/40 px-3 py-2">
        <div>
          <h1 className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
            <LineIcon className="h-3.5 w-3.5 text-cyan-400" /> IR Explorer
          </h1>
          <p className="mt-0.5 text-[11px] text-slate-500">
            Information ratio · book aggregate · category / regime / asset breakdown
          </p>
        </div>
        <Controls
          statuses={statuses} onStatuses={setStatuses}
          benchmark={benchmark} onBenchmark={setBenchmark}
          window={rollingWindow} onWindow={setRollingWindow}
        />
      </header>

      {!hasStatuses ? (
        <div className="flex w-full flex-1 items-center justify-center rounded-xl border border-slate-700 bg-slate-900/60 py-10 text-[13px] text-slate-400">
          Select at least one strategy status to view IR data.
        </div>
      ) : (
        <>
          <BookHeader data={bookQ.data} benchmark={benchmark} />

          <div className="grid grid-cols-12 gap-3">
            <Card title="IR by Category" className="col-span-7">
              <CategoryIRBar rows={catQ.data?.rows ?? []} />
            </Card>
            <Card title="IR by Regime (BTC trend)" className="col-span-5">
              <RegimeIRMatrix rows={regQ.data?.rows ?? []} available={regQ.data?.available ?? false} isLoading={regQ.isLoading} />
            </Card>
          </div>

          <Card title={`Rolling ${rollingWindow}d IR`}>
            <RollingIR data={rollQ.data?.series ?? []} />
          </Card>

          <div className="grid grid-cols-12 gap-3">
            {/* P16/B-M2 — renamed from "IR Waterfall". The chart renders each
                category's `delta` as a standalone bar from y=0 (not stacking on
                the previous running total), so the title was misleading. The
                "contribution" framing now matches what is actually drawn. */}
            <Card title="IR Contribution by Category" className="col-span-8">
              <Waterfall steps={wfQ.data?.steps ?? []} total={wfQ.data?.total_ir ?? 0} />
            </Card>
            <Card title="IR by Asset Universe" className="col-span-4">
              <AssetIRBar rows={assetQ.data?.rows ?? []} />
            </Card>
          </div>
        </>
      )}
    </div>
  );
}

function Controls({ statuses, onStatuses, benchmark, onBenchmark, window: rollingWindow, onWindow }: { statuses: string[]; onStatuses: (s: string[]) => void; benchmark: 'btc' | 'none'; onBenchmark: (b: 'btc' | 'none') => void; window: number; onWindow: (w: number) => void }) {
  return (
    <div className="flex items-center gap-2 text-[10px]">
      <div className="flex gap-1">
        {ALL_STATUSES.map((s) => {
          const on = statuses.includes(s);
          return (
            <button
              key={s}
              onClick={() => onStatuses(on ? statuses.filter((x) => x !== s) : [...statuses, s])}
              className={`rounded px-1.5 py-0.5 font-mono ${on ? 'bg-cyan-500/20 text-cyan-200' : 'bg-slate-800 text-slate-500'}`}
            >
              {s}
            </button>
          );
        })}
      </div>
      <div className="flex overflow-hidden rounded-md border border-slate-700">
        {(['btc', 'none'] as const).map((b) => (
          <button key={b} onClick={() => onBenchmark(b)} className={`px-2 py-0.5 ${benchmark === b ? 'bg-cyan-500/20 text-cyan-200' : 'text-slate-400 hover:bg-slate-800'}`}>
            {b === 'btc' ? 'vs BTC' : 'Abs (Sharpe)'}
          </button>
        ))}
      </div>
      <div className="flex overflow-hidden rounded-md border border-slate-700">
        {[14, 30, 60, 90].map((w) => (
          <button key={w} onClick={() => onWindow(w)} className={`px-2 py-0.5 ${rollingWindow === w ? 'bg-cyan-500/20 text-cyan-200' : 'text-slate-400 hover:bg-slate-800'}`}>{w}d</button>
        ))}
      </div>
    </div>
  );
}

function BookHeader({ data, benchmark }: { data: IrBookResponse | undefined; benchmark: 'btc' | 'none' }) {
  // P16/B-L3 — typed `data` so the optional-chained property access below is
  // typescript-checked (was `any` previously).
  const label = benchmark === 'btc' && !data?.degraded ? 'Book IR' : 'Book Sharpe';
  return (
    <div className="grid grid-cols-4 gap-3">
      <Tile label={label} value={data?.book_ir != null ? data.book_ir.toFixed(2) : '—'} cls="text-cyan-300" />
      <Tile label="Annual Return" value={data?.annualized_return != null ? `${(data.annualized_return * 100).toFixed(1)}%` : '—'} cls={data?.annualized_return == null ? 'text-slate-400' : data.annualized_return >= 0 ? 'text-emerald-300' : 'text-rose-300'} />
      <Tile label="Max DD" value={data?.max_drawdown != null ? `${(data.max_drawdown * 100).toFixed(1)}%` : '—'} cls="text-rose-300" />
      <Tile label="Strategies" value={String(data?.n_strategies ?? 0)} cls="text-slate-200" />
    </div>
  );
}

function Tile({ label, value, cls }: { label: string; value: string; cls: string }) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
      <div className="text-[9px] uppercase tracking-widest text-slate-500">{label}</div>
      <div className={`mt-1 font-mono text-2xl ${cls}`}>{value}</div>
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

function CategoryIRBar({ rows }: { rows: { category: string; ir: number | null; contribution: number; n_strategies: number }[] }) {
  if (!rows.length) return <Empty>Book empty — promote a strategy to APPROVED+.</Empty>;
  const data = rows.map((r) => ({ category: r.category, ir: r.ir, n: r.n_strategies }));
  return (
    <ResponsiveContainer width="100%" height={240}>
      <BarChart data={data} barCategoryGap="20%">
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis dataKey="category" stroke="#475569" tick={{ fontSize: 9 }} angle={-20} textAnchor="end" height={50} />
        <YAxis stroke="#475569" tick={{ fontSize: 9 }} />
        <Tooltip cursor={{ fill: 'rgba(148, 163, 184, 0.08)' }} contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 10 }} itemStyle={{ color: '#e2e8f0' }} />
        <ReferenceLine y={0} stroke="#475569" />
        <Bar dataKey="ir" maxBarSize={56}>
          {data.map((d, i) => <Cell key={i} fill={d.ir == null ? '#475569' : d.ir >= 0 ? '#22d3ee' : '#f43f5e'} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

function RegimeIRMatrix({ rows, available, isLoading }: { rows: { regime: string; ir: number | null; n_days: number }[]; available: boolean; isLoading?: boolean }) {
  if (isLoading) return <Empty>Loading regime data…</Empty>;
  if (!available) return <Empty>Regime data unavailable — BTC price history missing.</Empty>;
  if (!rows.length) return <Empty>No regime data yet.</Empty>;
  return (
    <div className="grid grid-cols-3 gap-2">
      {rows.map((r) => {
        const cls = r.ir == null ? 'text-slate-500' : r.ir >= 0.5 ? 'text-emerald-300' : r.ir >= 0 ? 'text-cyan-300' : 'text-rose-300';
        return (
          <div key={r.regime} className="rounded-md border border-slate-800 bg-slate-950/50 p-3 text-center">
            <div className="text-[9px] uppercase tracking-widest text-slate-500">{r.regime}</div>
            <div className={`mt-1 font-mono text-lg ${cls}`}>{r.ir != null ? r.ir.toFixed(2) : '—'}</div>
            <div className="mt-0.5 text-[9px] text-slate-500">{r.n_days}d</div>
          </div>
        );
      })}
    </div>
  );
}

function RollingIR({ data }: { data: { date: string; ir: number | null }[] }) {
  if (!data.length) return <Empty>Not enough history for rolling IR.</Empty>;
  return (
    <ResponsiveContainer width="100%" height={220}>
      <LineChart data={data}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis dataKey="date" stroke="#475569" tick={{ fontSize: 9 }} minTickGap={50} />
        <YAxis stroke="#475569" tick={{ fontSize: 9 }} />
        <Tooltip contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 10 }} />
        <ReferenceLine y={0} stroke="#475569" strokeDasharray="3 3" />
        <Line type="monotone" dataKey="ir" stroke="#22d3ee" dot={false} strokeWidth={2} connectNulls />
      </LineChart>
    </ResponsiveContainer>
  );
}

function Waterfall({ steps, total }: { steps: { label: string; delta: number; running: number }[]; total: number }) {
  if (!steps.length) return <Empty>No category contributions yet.</Empty>;
  // P16/B-M2 — the legacy `prev: s.running - s.delta` field was only used by
  // a true waterfall stack that was never wired up; the chart renders each
  // delta as a standalone bar. Dropped the dead computation along with the
  // title rename.
  const data = steps;
  return (
    <div>
      <div className="mb-1 text-right text-[10px] text-slate-500">
        Total IR: <span className="font-mono text-cyan-300">{total.toFixed(2)}</span>
      </div>
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={data} barCategoryGap="20%">
          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
          <XAxis dataKey="label" stroke="#475569" tick={{ fontSize: 9 }} angle={-15} textAnchor="end" height={50} />
          <YAxis stroke="#475569" tick={{ fontSize: 9 }} />
          <Tooltip cursor={{ fill: 'rgba(148, 163, 184, 0.08)' }} contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 10 }} itemStyle={{ color: '#e2e8f0' }} />
          <ReferenceLine y={0} stroke="#475569" />
          <Bar dataKey="delta" maxBarSize={56}>
            {data.map((d, i) => <Cell key={i} fill={d.delta >= 0 ? '#22d3ee' : '#f43f5e'} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function AssetIRBar({ rows }: { rows: { asset: string; ir: number | null; weight: number }[] }) {
  if (!rows.length) return <Empty>No asset data yet.</Empty>;
  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={rows} layout="vertical" barCategoryGap="20%">
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis type="number" stroke="#475569" tick={{ fontSize: 9 }} />
        <YAxis type="category" dataKey="asset" stroke="#475569" tick={{ fontSize: 9 }} width={50} />
        <Tooltip cursor={{ fill: 'rgba(148, 163, 184, 0.08)' }} contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 10 }} itemStyle={{ color: '#e2e8f0' }} />
        <Bar dataKey="ir" maxBarSize={48}>
          {rows.map((d, i) => <Cell key={i} fill={d.ir == null ? '#475569' : d.ir >= 0 ? '#a78bfa' : '#f43f5e'} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="flex h-full min-h-[120px] items-center justify-center text-center text-[11px] text-slate-500">{children}</div>;
}
