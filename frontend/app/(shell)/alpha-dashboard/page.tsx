'use client';

import { useMemo, useState } from 'react';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { LayoutDashboard, TrendingUp, ShieldCheck, AlertCircle, ArrowUp, ArrowDown } from 'lucide-react';
import { api, type AlphaStrategy } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import CategoryDoughnut from '@/components/charts/CategoryDoughnut';
import { qualityLabel, qualityLabelClasses } from '@/lib/derive';

// P18 A1 — sortable leaderboard. Default preserves prior behaviour
// (Sharpe descending), but the header row now exposes click-sort on
// Name, Status, and Sharpe.
type SortKey = 'name' | 'status' | 'sharpe';
type SortDir = 'asc' | 'desc';

/**
 * /alpha-dashboard — executive overview of the live alpha inventory.
 * Composes existing primitives (KpiStrip patterns + CategoryDoughnut +
 * quality label) with the strategies + paper-trade payloads.
 */
export default function AlphaDashboardPage() {
  const stratQ = useQuery({
    queryKey: queryKeys.strategies,
    queryFn: api.strategies,
    refetchInterval: 10_000,
  });
  const paperQ = useQuery({
    queryKey: queryKeys.paperTradeList,
    queryFn: api.paperTradeList,
    refetchInterval: 15_000,
  });

  const strategies: AlphaStrategy[] = stratQ.data?.strategies ?? [];

  const stats = useMemo(() => {
    const approved = strategies.filter((s) => ['APPROVED', 'PAPER_TRADE', 'SMALL_CAPITAL', 'LIVE'].includes((s.status || '').toUpperCase()));
    const live = strategies.filter((s) => (s.status || '').toUpperCase() === 'LIVE');
    const rejected = strategies.filter((s) => (s.status || '').toUpperCase() === 'REJECTED');
    const sharpes = approved
      .map((s) => Number(s.metrics?.annualized_sharpe))
      .filter(Number.isFinite);
    const avg = (a: number[]) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : null);
    const proven = approved.filter((s) => Number(s.metrics?.annualized_sharpe) >= 1.0);
    return {
      total: strategies.length,
      approved: approved.length,
      live: live.length,
      rejected: rejected.length,
      avgSharpe: avg(sharpes),
      proven: proven.length,
    };
  }, [strategies]);

  const categorySlices = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const s of strategies) {
      const cat = (s.config?.alpha_category as string | undefined) || 'Unclassified';
      counts[cat] = (counts[cat] ?? 0) + 1;
    }
    return Object.entries(counts).map(([name, value]) => ({ name, value }));
  }, [strategies]);

  const [sortKey, setSortKey] = useState<SortKey>('sharpe');
  const [sortDir, setSortDir] = useState<SortDir>('desc');

  const topApproved = useMemo(() => {
    const filtered = strategies.filter((s) =>
      ['APPROVED', 'PAPER_TRADE', 'SMALL_CAPITAL', 'LIVE'].includes(
        (s.status || '').toUpperCase(),
      ),
    );
    const sign = sortDir === 'asc' ? 1 : -1;
    const cmp = (a: AlphaStrategy, b: AlphaStrategy) => {
      if (sortKey === 'sharpe') {
        // Push null/missing Sharpe to the sort tail in all directions.
        // `|| 0` conflates NaN (undefined) and 0 (null) with genuine zero
        // Sharpe, misordering null strategies above negative-Sharpe ones.
        const sh = (s: AlphaStrategy): number => {
          const v = Number(s.metrics?.annualized_sharpe);
          return Number.isFinite(v) ? v : (sortDir === 'asc' ? Infinity : -Infinity);
        };
        return (sh(a) - sh(b)) * sign;
      }
      if (sortKey === 'status') {
        return (a.status || '').localeCompare(b.status || '') * sign;
      }
      // name
      const an = (a.slug ?? a.name ?? '').toLowerCase();
      const bn = (b.slug ?? b.name ?? '').toLowerCase();
      return an.localeCompare(bn) * sign;
    };
    return filtered.slice().sort(cmp).slice(0, 8);
  }, [strategies, sortKey, sortDir]);

  const toggleSort = (k: SortKey) => {
    if (sortKey === k) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(k);
      setSortDir(k === 'sharpe' ? 'desc' : 'asc');
    }
  };

  const runs = paperQ.data?.runs ?? [];
  const healthy = runs.filter((r) => r.is_healthy).length;
  const unhealthy = runs.length - healthy;

  return (
    <div className="flex h-full w-full flex-col gap-3 overflow-y-auto p-3">
      <header className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
        <div className="flex items-center gap-2">
          <LayoutDashboard className="h-4 w-4 text-cyan-400" />
          <h1 className="text-sm font-bold tracking-widest text-slate-100">
            ALPHA HEALTH
          </h1>
        </div>
        <p className="mt-1 text-[11px] text-slate-500">
          Executive overview of the alpha inventory — pipeline throughput,
          category mix, and the proven-Sharpe leaderboard.
        </p>
        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4 md:grid-cols-7">
          <Kpi label="Total" value={stats.total} color="text-cyan-200" />
          <Kpi label="Approved" value={stats.approved} color="text-emerald-300" icon={<ShieldCheck className="h-3 w-3" />} />
          <Kpi label="Live" value={stats.live} color="text-emerald-300" />
          <Kpi label="Rejected" value={stats.rejected} color="text-rose-300" icon={<AlertCircle className="h-3 w-3" />} />
          <Kpi label="Avg Sharpe (approved)" value={stats.avgSharpe == null ? '—' : stats.avgSharpe.toFixed(2)} color="text-cyan-200" icon={<TrendingUp className="h-3 w-3" />} />
          <Kpi label="Proven (≥1.0)" value={stats.proven} color="text-emerald-300" />
          <Kpi label="Paper Healthy" value={`${healthy}/${runs.length}`} color={unhealthy === 0 ? 'text-emerald-300' : 'text-amber-300'} />
        </div>
      </header>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        <section className="lg:col-span-2 rounded-xl border border-slate-800 bg-slate-900/40">
          <header className="border-b border-slate-800 px-4 py-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
            {`Active Alphas · sorted by ${({ name: 'Name', status: 'Status', sharpe: 'Sharpe' } as Record<SortKey, string>)[sortKey]} (${({ asc: 'asc', desc: 'desc' } as Record<SortDir, string>)[sortDir]})`}
          </header>
          {/* P18 A1 — sortable column headers; default sort = Sharpe desc. */}
          <div className="flex items-center justify-between border-b border-slate-900/60 px-4 py-1.5 text-[9px] font-bold uppercase tracking-wider text-slate-500">
            <SortHeader label="Name" active={sortKey === 'name'} dir={sortDir} onClick={() => toggleSort('name')} />
            <div className="flex items-center gap-3">
              <SortHeader label="Sharpe" active={sortKey === 'sharpe'} dir={sortDir} onClick={() => toggleSort('sharpe')} />
              <SortHeader label="Status" active={sortKey === 'status'} dir={sortDir} onClick={() => toggleSort('status')} />
            </div>
          </div>
          <ul className="divide-y divide-slate-800">
            {topApproved.length === 0 && (
              <li className="p-4 text-center text-[11px] text-slate-500">
                No approved strategies yet.
              </li>
            )}
            {topApproved.map((s) => {
              const ql = qualityLabel(Number(s.metrics?.annualized_sharpe));
              return (
                <li key={s.id} className="flex items-center justify-between px-4 py-2 text-[11px]">
                  <div className="flex min-w-0 items-center gap-2">
                    <Link href={`/strategies/${s.id}`} className="font-mono text-cyan-300 hover:underline">
                      S#{s.id}
                    </Link>
                    {s.alpha_id && (
                      <span
                        className="shrink-0 rounded border border-cyan-700/40 bg-cyan-500/10 px-1.5 py-0.5 font-mono text-[9px] text-cyan-200"
                        title="Canonical alpha id (stable across renames)"
                      >
                        {s.alpha_id}
                      </span>
                    )}
                    <span className="truncate text-slate-200">{s.slug ?? s.name}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className={`rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase ${qualityLabelClasses(ql.tone)}`}>
                      {ql.text}
                    </span>
                    <span className="rounded border border-slate-700 bg-slate-900 px-1.5 py-0.5 text-[9px] uppercase text-slate-400">
                      {s.status}
                    </span>
                  </div>
                </li>
              );
            })}
          </ul>
          {stats.approved > topApproved.length && (
            <div className="border-t border-slate-800 px-4 py-2 text-center text-[10px] text-slate-500">
              Showing top {topApproved.length} of {stats.approved} approved strategies
              {' · '}
              <Link href="/strategies" className="text-cyan-400 hover:underline">
                View all
              </Link>
            </div>
          )}
        </section>

        <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
          <CategoryDoughnut slices={categorySlices} title="Category Mix (all strategies)" variant="pie" />
        </section>
      </div>
    </div>
  );
}

function SortHeader({
  label,
  active,
  dir,
  onClick,
}: {
  label: string;
  active: boolean;
  dir: SortDir;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex items-center gap-1 transition hover:text-cyan-300 ${active ? 'text-cyan-300' : ''}`}
      aria-label={`Sort by ${label}${active ? ` (${dir})` : ''}`}
    >
      {label}
      {active && (dir === 'asc' ? <ArrowUp className="h-2.5 w-2.5" /> : <ArrowDown className="h-2.5 w-2.5" />)}
    </button>
  );
}

function Kpi({
  label,
  value,
  color,
  icon,
}: {
  label: string;
  value: number | string;
  color: string;
  icon?: React.ReactNode;
}) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-950/50 p-2">
      <div className={`flex items-center gap-1 font-mono text-base leading-none ${color}`}>
        {icon}
        {value}
      </div>
      <div className="mt-1 text-[9px] uppercase tracking-wider text-slate-500">
        {label}
      </div>
    </div>
  );
}
