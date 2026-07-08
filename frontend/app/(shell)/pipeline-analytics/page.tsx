'use client';

import { useMemo, useState } from 'react';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Clock,
  Filter as Funnel,
  LineChart as LineChartIcon,
  ListChecks,
} from 'lucide-react';
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
  LineChart, Line, Legend, Cell, CartesianGrid,
} from 'recharts';

import {
  api,
  type AlphaStrategy,
  type PAGate,
  type PAOccupancyStatus,
  type PATimeInStageRow,
} from '@/lib/api';
import { queryKeys } from '@/lib/query';

/**
 * /pipeline-analytics — P7-01
 * Bucket throughput · time-in-stage · gate pass-rate · bottleneck occupancy.
 * All four panels pull from /api/pipeline-analytics/* every 30-60s.
 *
 * P12 A-M5 — adds a 5th panel: Critic Soul-Question Coverage, which counts
 * the fill-rate of the 6 `config.critic_soul_questions` fields across all
 * reviewed strategies. Lets the operator see whether the Critic step is
 * actually decomposing alphas across all six dimensions.
 */
type SoulKey =
  | 'q1_why_works'
  | 'q2_who_is_wrong'
  | 'q3_when_fails'
  | 'q4_decay_horizon'
  | 'q5_capacity'
  | 'q6_risk_premium';
export default function PipelineAnalyticsPage() {
  const [days, setDays] = useState<number>(30);
  const [bucket, setBucket] = useState<'day' | 'week'>('day');
  const [selectedStatus, setSelectedStatus] = useState<string>('BACKTESTING');

  const tQ = useQuery({
    queryKey: queryKeys.paThroughput(days, bucket),
    queryFn: () => api.paThroughput(days, bucket),
    refetchInterval: 30_000,
  });
  const tisQ = useQuery({
    queryKey: queryKeys.paTimeInStage(days),
    queryFn: () => api.paTimeInStage(days),
    refetchInterval: 60_000,
  });
  const gpQ = useQuery({
    queryKey: queryKeys.paGatePass(days, 14),
    queryFn: () => api.paGatePassRate(days, 14),
    refetchInterval: 60_000,
  });
  const ocQ = useQuery({
    queryKey: queryKeys.paOccupancy,
    queryFn: api.paOccupancy,
    refetchInterval: 15_000,
  });
  // P12 A-M5 — Critic Soul-Question coverage: fraction of reviewed
  // strategies that recorded a non-empty response for each of the 6 Critic
  // soul questions. Denominator is "strategies with at least one q* non-empty"
  // so undecidable (no review at all) strategies don't deflate the bars.
  const stratsQ = useQuery({
    queryKey: queryKeys.strategies,
    queryFn: api.strategies,
    refetchInterval: 30_000,
  });

  const soulCoverage = useMemo(() => {
    const strategies: AlphaStrategy[] = stratsQ.data?.strategies ?? [];
    const keys: SoulKey[] = [
      'q1_why_works',
      'q2_who_is_wrong',
      'q3_when_fails',
      'q4_decay_horizon',
      'q5_capacity',
      'q6_risk_premium',
    ];
    const labels: Record<SoulKey, string> = {
      q1_why_works: 'Q1 · Why it works',
      q2_who_is_wrong: 'Q2 · Who is wrong',
      q3_when_fails: 'Q3 · When it fails',
      q4_decay_horizon: 'Q4 · Decay horizon',
      q5_capacity: 'Q5 · Capacity',
      q6_risk_premium: 'Q6 · Risk premium',
    };
    const isFilled = (v: unknown): boolean =>
      typeof v === 'string' && v.trim().length > 0;

    // Denominator = strategies where the Critic stage ran, indicated by a
    // non-empty config.critic_verdict. This captures strategies the Critic
    // processed but for which the LLM returned all-empty soul answers (a
    // systematic failure mode), instead of silently dropping them from both
    // numerator and denominator and inflating the displayed coverage rate.
    const hasCriticVerdict = (cfg: Record<string, unknown>): boolean =>
      typeof cfg.critic_verdict === 'string' && cfg.critic_verdict.trim().length > 0;
    let reviewed = 0;
    const counts: Record<SoulKey, number> = {
      q1_why_works: 0,
      q2_who_is_wrong: 0,
      q3_when_fails: 0,
      q4_decay_horizon: 0,
      q5_capacity: 0,
      q6_risk_premium: 0,
    };
    for (const s of strategies) {
      const cfg = (s.config as Record<string, unknown> | undefined) ?? {};
      if (!hasCriticVerdict(cfg)) continue;
      reviewed += 1;
      const soul =
        (cfg.critic_soul_questions as Record<string, unknown> | undefined) ??
        {};
      const filled = keys.filter((k) => isFilled(soul[k]));
      for (const k of filled) counts[k] += 1;
    }
    return keys.map((k) => ({
      key: k,
      label: labels[k],
      filled: counts[k],
      total: reviewed,
      rate: reviewed > 0 ? counts[k] / reviewed : null,
    }));
  }, [stratsQ.data]);

  return (
    <div className="flex h-full w-full flex-col gap-3 overflow-y-auto p-3">
      <header className="flex items-center justify-between rounded-xl border border-slate-800 bg-slate-900/40 px-3 py-2">
        <div>
          <h1 className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
            <Activity className="h-3.5 w-3.5 text-cyan-400" /> Pipeline Analytics
          </h1>
          <p className="mt-0.5 text-[11px] text-slate-500">
            Bucket throughput · time-in-stage · gate pass rates · bottlenecks
          </p>
        </div>
        <RangePicker
          days={days}
          bucket={bucket}
          onDays={setDays}
          onBucket={setBucket}
        />
      </header>

      <div className="grid grid-cols-12 gap-3">
        <Card title="Throughput" icon={<BarChart3 className="h-3 w-3" />} className="col-span-8">
          <ThroughputChart series={tQ.data?.series ?? []} />
        </Card>
        <Card title="Stage Occupancy" icon={<Clock className="h-3 w-3" />} className="col-span-4">
          <OccupancyTable rows={ocQ.data?.per_status ?? []} bottleneck={ocQ.data?.bottleneck_status ?? null} />
        </Card>

        <Card title="Gate Funnel" icon={<Funnel className="h-3 w-3" />} className="col-span-7">
          <GateFunnel gates={gpQ.data?.gates ?? []} />
        </Card>
        <Card title="Gate Pass-Rate Trend" icon={<LineChartIcon className="h-3 w-3" />} className="col-span-5">
          <GateTrendChart trend={gpQ.data?.trend ?? []} gates={gpQ.data?.gates ?? []} />
        </Card>

        <Card title="Time in Stage" icon={<Clock className="h-3 w-3" />} className="col-span-8">
          <TimeInStageHistogram
            rows={tisQ.data?.per_status ?? []}
            selected={selectedStatus}
            onSelect={setSelectedStatus}
          />
        </Card>
        <Card title="Stuck Strategies" icon={<AlertTriangle className="h-3 w-3" />} className="col-span-4">
          <StuckList rows={ocQ.data?.per_status ?? []} />
        </Card>

        <Card
          title="Critic Soul-Question Coverage"
          icon={<ListChecks className="h-3 w-3" />}
          className="col-span-12"
        >
          <SoulCoverageChart rows={soulCoverage} />
        </Card>
      </div>
    </div>
  );
}

function SoulCoverageChart({
  rows,
}: {
  rows: {
    key: SoulKey;
    label: string;
    filled: number;
    total: number;
    rate: number | null;
  }[];
}) {
  const anyReviewed = rows.some((r) => r.total > 0);
  if (!anyReviewed) {
    return (
      <EmptyChart message="No reviewed strategies yet — Critic soul questions are populated by the orchestrator after the CRITIC_LOOP stage." />
    );
  }
  return (
    <div className="flex flex-col gap-1.5">
      {rows.map((r) => {
        const pct = r.rate == null ? 0 : Math.round(r.rate * 100);
        const tone =
          r.rate == null
            ? 'text-slate-500'
            : pct >= 80
            ? 'text-emerald-300'
            : pct >= 50
            ? 'text-amber-300'
            : 'text-rose-300';
        const barColor =
          r.rate == null
            ? '#334155'
            : pct >= 80
            ? '#34d399'
            : pct >= 50
            ? '#fbbf24'
            : '#f87171';
        // P18 A3 — accessible tooltip summarising the underlying counts.
        const summary =
          r.rate == null
            ? `${r.label} — no reviewed strategies yet`
            : `${r.label} — ${r.filled} / ${r.total} reviewed strategies populated this dimension (${pct}%)`;
        return (
          <div
            key={r.key}
            className="grid grid-cols-12 items-center gap-2 text-[10px]"
            title={summary}
          >
            <div className="col-span-3 truncate text-slate-300" title={r.label}>
              {r.label}
            </div>
            <div className="col-span-7 h-3 overflow-hidden rounded-sm bg-slate-950">
              <div
                className="h-full"
                style={{ width: `${pct}%`, background: barColor }}
                aria-label={summary}
              />
            </div>
            <div className={`col-span-2 text-right font-mono ${tone}`}>
              {r.rate == null ? '—' : `${pct}%`}{' '}
              <span className="text-slate-600">
                ({r.filled}/{r.total})
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Card({ title, icon, className = '', children }: { title: string; icon?: React.ReactNode; className?: string; children: React.ReactNode }) {
  return (
    <section className={`flex flex-col rounded-xl border border-slate-800 bg-slate-900/40 p-3 ${className}`}>
      <header className="mb-2 flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">
        {icon}{title}
      </header>
      <div className="flex-1 min-h-[200px]">{children}</div>
    </section>
  );
}

function RangePicker({ days, bucket, onDays, onBucket }: { days: number; bucket: 'day' | 'week'; onDays: (d: number) => void; onBucket: (b: 'day' | 'week') => void }) {
  return (
    <div className="flex items-center gap-2 text-[10px]">
      <div className="flex overflow-hidden rounded-md border border-slate-700">
        {[7, 30, 90, 180].map((d) => (
          <button
            key={d}
            onClick={() => onDays(d)}
            className={`px-2 py-1 ${days === d ? 'bg-cyan-500/20 text-cyan-200' : 'text-slate-400 hover:bg-slate-800'}`}
          >
            {d}d
          </button>
        ))}
      </div>
      <div className="flex overflow-hidden rounded-md border border-slate-700">
        {(['day', 'week'] as const).map((b) => (
          <button
            key={b}
            onClick={() => onBucket(b)}
            className={`px-2 py-1 ${bucket === b ? 'bg-cyan-500/20 text-cyan-200' : 'text-slate-400 hover:bg-slate-800'}`}
          >
            {b.toUpperCase()}
          </button>
        ))}
      </div>
    </div>
  );
}

const BUCKET_COLORS: Record<string, string> = {
  alpha_ideas: '#64748b',
  research: '#22d3ee',
  factor_dev: '#a78bfa',
  full_backtest: '#34d399',
  paper_trade: '#fbbf24',
  small_capital: '#fb923c',
  live: '#f472b6',
  graveyard: '#475569',
  other: '#94a3b8',
};

function ThroughputChart({ series }: { series: { date: string; counts: Record<string, number> }[] }) {
  const data = useMemo(() => {
    return series.map((p) => ({
      date: p.date,
      ...p.counts,
    }));
  }, [series]);
  if (!data.length) {
    return <EmptyChart message="No transitions in window — pipeline idle or just deployed" />;
  }
  const keys = new Set<string>();
  for (const p of data) for (const k of Object.keys(p)) if (k !== 'date') keys.add(k);
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={data} barCategoryGap="20%">
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis dataKey="date" stroke="#475569" tick={{ fontSize: 9 }} />
        <YAxis stroke="#475569" tick={{ fontSize: 9 }} allowDecimals={false} />
        {/* cursor: the default Bar tooltip cursor is a near-white full-band
            rectangle; with a single date bucket that band spans the entire plot
            and paints as a giant light block. A faint slate fill keeps the hover
            hint without swallowing the chart. */}
        <Tooltip
          cursor={{ fill: 'rgba(148, 163, 184, 0.08)' }}
          contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 10 }}
        />
        <Legend wrapperStyle={{ fontSize: 10 }} />
        {/* maxBarSize: recharts sizes a category band to fill the plot when there
            is only one bucket, ballooning a lone "graveyard:1" bar across the
            whole canvas. Capping the column keeps sparse data readable. */}
        {Array.from(keys).map((k) => (
          <Bar key={k} dataKey={k} stackId="a" fill={BUCKET_COLORS[k] ?? '#94a3b8'} maxBarSize={56} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

function OccupancyTable({ rows, bottleneck }: { rows: PAOccupancyStatus[]; bottleneck: string | null }) {
  if (!rows.length) return <EmptyChart message="No strategies in pipeline" />;
  return (
    <div className="overflow-y-auto text-[11px]">
      <table className="w-full">
        <thead className="text-[9px] uppercase tracking-wider text-slate-500">
          <tr>
            <th scope="col" className="text-left">Status</th>
            <th scope="col" className="text-right">Count</th>
            <th scope="col" className="text-right">Median Age</th>
            <th scope="col" className="text-right">Stuck</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {rows.map((r) => (
            <tr
              key={r.status}
              className={`border-t border-slate-900/60 ${r.status === bottleneck ? 'border-l-2 border-l-rose-400 bg-rose-500/5' : ''}`}
            >
              <td className="py-1 text-slate-200">{r.status}</td>
              <td className="text-right text-cyan-200">{r.count}</td>
              <td className="text-right text-slate-400">{formatMinutes(r.median_age_minutes)}</td>
              <td className="text-right text-amber-200">{r.stuck.length}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function GateFunnel({ gates }: { gates: PAGate[] }) {
  if (!gates.length) return <EmptyChart message="No gate transitions yet" />;
  const data = gates.map((g) => ({
    label: g.label,
    passed: g.passed,
    failed: g.failed,
    rate: g.pass_rate,
    total: g.total,
  }));
  return (
    <div className="flex flex-col gap-1.5">
      {data.map((g) => {
        const ratePct = g.rate != null ? Math.round(g.rate * 100) : null;
        const tone = ratePct == null ? 'text-slate-500' : ratePct > 70 ? 'text-emerald-300' : ratePct > 40 ? 'text-amber-300' : 'text-rose-300';
        const max = Math.max(...data.map((d) => d.total), 1);
        return (
          <div key={g.label} className="grid grid-cols-12 items-center gap-2 text-[10px]">
            <div className="col-span-4 truncate text-slate-300">{g.label}</div>
            <div className="col-span-6 flex h-3 overflow-hidden rounded-sm bg-slate-950">
              <div className="bg-emerald-500/50" style={{ width: `${(g.passed / max) * 100}%` }} />
              <div className="bg-rose-500/50" style={{ width: `${(g.failed / max) * 100}%` }} />
            </div>
            <div className={`col-span-2 text-right font-mono ${tone}`}>
              {ratePct != null ? `${ratePct}%` : '—'} <span className="text-slate-600">({g.total})</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function GateTrendChart({ trend, gates }: { trend: { date: string; rates: Record<string, number | null> }[]; gates: PAGate[] }) {
  if (!trend.length || !gates.length) return <EmptyChart message="Not enough history for trend" />;
  const data = trend.map((t) => ({
    date: t.date,
    ...Object.fromEntries(Object.entries(t.rates).map(([k, v]) => [k, v == null ? null : Math.round((v ?? 0) * 100)])),
  }));
  const palette = ['#22d3ee', '#a78bfa', '#34d399', '#fbbf24', '#fb923c', '#f472b6', '#94a3b8', '#60a5fa'];
  return (
    <ResponsiveContainer width="100%" height={260}>
      <LineChart data={data}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis dataKey="date" stroke="#475569" tick={{ fontSize: 8 }} />
        <YAxis stroke="#475569" tick={{ fontSize: 8 }} domain={[0, 100]} />
        <Tooltip contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 10 }} />
        {gates.map((g, i) => (
          <Line
            key={g.key}
            type="monotone"
            dataKey={g.key}
            stroke={palette[i % palette.length]}
            dot={false}
            strokeWidth={1.5}
            connectNulls
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}

function TimeInStageHistogram({ rows, selected, onSelect }: { rows: PATimeInStageRow[]; selected: string; onSelect: (s: string) => void }) {
  if (!rows.length) return <EmptyChart message="No completed transitions in window" />;
  const active = rows.find((r) => r.status === selected) ?? rows[0];
  return (
    <div className="grid h-full grid-cols-12 gap-3">
      <div className="col-span-3 overflow-y-auto">
        {rows.map((r) => (
          <button
            key={r.status}
            onClick={() => onSelect(r.status)}
            className={`mb-1 flex w-full items-center justify-between rounded-md px-2 py-1 text-[10px] ${r.status === active.status ? 'bg-cyan-500/15 text-cyan-200' : 'text-slate-400 hover:bg-slate-800'}`}
          >
            <span className="truncate">{r.status}</span>
            <span className="font-mono text-slate-500">{formatMinutes(r.p50_minutes)}</span>
          </button>
        ))}
      </div>
      <div className="col-span-9 flex flex-col">
        <div className="mb-2 grid grid-cols-3 gap-2 text-[10px]">
          <Stat label="p50" value={formatMinutes(active.p50_minutes)} />
          <Stat label="p90" value={formatMinutes(active.p90_minutes)} />
          <Stat label="p99" value={formatMinutes(active.p99_minutes)} />
        </div>
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={active.histogram} barCategoryGap="20%">
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
            <XAxis dataKey="bin_label" stroke="#475569" tick={{ fontSize: 9 }} />
            <YAxis stroke="#475569" tick={{ fontSize: 9 }} allowDecimals={false} />
            <Tooltip
              cursor={{ fill: 'rgba(148, 163, 184, 0.08)' }}
              contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 10 }}
              itemStyle={{ color: '#e2e8f0' }}
            />
            <Bar dataKey="count" maxBarSize={56}>
              {active.histogram.map((_, i) => (
                <Cell key={i} fill={`hsl(${190 + i * 12}, 70%, ${45 + Math.min(20, i * 2)}%)`} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function StuckList({ rows }: { rows: PAOccupancyStatus[] }) {
  const all = rows.flatMap((r) => r.stuck.map((s) => ({ ...s, status: r.status })));
  all.sort((a, b) => b.age_minutes - a.age_minutes);
  if (!all.length) return <EmptyChart message="No stuck strategies" />;
  return (
    <ul className="space-y-1 overflow-y-auto text-[11px]">
      {all.slice(0, 20).map((s) => (
        <li key={`${s.status}:${s.id}`} className="flex items-center justify-between rounded-md border border-slate-900 bg-slate-950/40 px-2 py-1.5">
          <Link href={`/strategies/${s.id}`} className="truncate text-slate-200 hover:text-cyan-300">
            S#{s.id} {s.name}
          </Link>
          <div className="ml-2 flex items-center gap-2 text-[9px]">
            <span className="rounded-sm bg-slate-800 px-1.5 py-0.5 font-mono text-slate-400">{s.status}</span>
            <span className="font-mono text-amber-300">{formatMinutes(s.age_minutes)}</span>
          </div>
        </li>
      ))}
    </ul>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-950/60 p-2 text-center">
      <div className="text-[8px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className="font-mono text-sm text-cyan-200">{value}</div>
    </div>
  );
}

function EmptyChart({ message }: { message: string }) {
  return (
    <div className="flex h-full min-h-[140px] items-center justify-center text-center text-[11px] text-slate-500">
      {message}
    </div>
  );
}

function formatMinutes(m: number | null | undefined): string {
  if (m == null || !Number.isFinite(m as number)) return '—';
  if ((m as number) < 1) return '<1m';
  if (m < 60) return `${m.toFixed(0)}m`;
  if (m < 1440) return `${(m / 60).toFixed(1)}h`;
  return `${(m / 1440).toFixed(1)}d`;
}
