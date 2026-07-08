'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Save, Play, Trash2, Scale, GitBranch, Loader2 } from 'lucide-react';
import {
  ResponsiveContainer, PieChart, Pie, Cell, Tooltip, BarChart, Bar, XAxis, YAxis,
  CartesianGrid, LineChart, Line, ScatterChart, Scatter, ZAxis, ReferenceLine,
} from 'recharts';

import { api, type PoCombineResp, type PoFrontierPoint } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { CHART_COLORS } from '@/components/charts/_shared';

// P9-C-L3 — backend ships 11 methods (see backend/core/allocators.py METHOD_KEYS).
// The 8 reference-screenshot methods are unmarked; the 3 P6-M16 extras
// (UMC / Use Target ETL / ERoC) carry a BETA suffix so users can see they
// are extended-registry entries not in the original spec.
const METHOD_GROUPS: { label: string; methods: { key: string; label: string }[] }[] = [
  { label: 'Risk-balanced', methods: [
    { key: 'equal_weight', label: 'Even (1/N)' },
    { key: 'inverse_vol', label: 'Inverse Vol' },
    { key: 'risk_parity', label: 'Risk Parity' },
    { key: 'umc', label: 'UMC · BETA' },
    { key: 'min_variance', label: 'Min Variance' },
  ]},
  { label: 'Return-maximizing', methods: [
    { key: 'mean_variance', label: 'Mean Variance' },
    { key: 'half_kelly', label: 'Half-Kelly' },
    { key: 'eroc_es', label: 'ERoC (ES) · BETA' },
  ]},
  { label: 'Tail-risk-aware', methods: [
    { key: 'vol_target_15', label: 'Vol Target 15%' },
    { key: 'use_target_etl', label: 'Use Target ETL · BETA' },
    { key: 'cvar_5', label: 'CVaR 5%' },
  ]},
];

export default function PortfolioOptimizerPage() {
  const qc = useQueryClient();
  // NOTE: must use raw `api.strategies` (not `.then((r) => r.strategies)`) so
  // every other consumer sharing `queryKeys.strategies` sees the same cached
  // shape `{ strategies: [...] }`. See arena/page.tsx for the full rationale.
  const listQ = useQuery({ queryKey: queryKeys.strategies, queryFn: api.strategies });
  const savedQ = useQuery({ queryKey: queryKeys.poSaved, queryFn: api.poSaved });

  const eligible = (listQ.data?.strategies ?? []).filter((s) => ['APPROVED', 'PAPER_TRADE', 'SMALL_CAPITAL', 'LIVE'].includes(s.status));
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [method, setMethod] = useState<string>('equal_weight');
  const [maxWeight, setMaxWeight] = useState<number>(0.30);
  const [minWeight, setMinWeight] = useState<number>(0.0);
  const [allowShort, setAllowShort] = useState<boolean>(false);
  const [volTarget, setVolTarget] = useState<number>(0.15);
  // P18-C2 — Leverage / beta caps are not yet honoured by the backend
  // PoConstraints payload (see frontend/lib/api.ts:1237). The inputs are
  // captured here so the operator can dial them in for downstream batch
  // jobs / manual review; once the backend grows `max_leverage` and
  // `beta_limit` keys, simply forward these values via `constraints` below.
  const [maxLeverage, setMaxLeverage] = useState<number>(1.0);
  const [betaLimit, setBetaLimit] = useState<number>(0.5);
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [saveNameInput, setSaveNameInput] = useState<string | null>(null); // null = hidden, string = visible with current value
  const [saveSuccess, setSaveSuccess] = useState<boolean>(false);  // R5/UX-06: transient "Saved!" feedback
  const saveSuccessTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [deleteArmedId, setDeleteArmedId] = useState<number | null>(null);
  const deleteArmTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const combineMut = useMutation({
    mutationFn: () =>
      api.poCombine({
        strategy_ids: Array.from(selected),
        method,
        constraints: { max_weight: maxWeight, min_weight: minWeight, allow_short: allowShort, vol_target_annual: volTarget },
      }),
    onError: (e) => setMutationError(e instanceof Error ? e.message : String(e)),
  });
  const saveMut = useMutation({
    mutationFn: (name: string) =>
      api.poSave({
        name,
        strategy_ids: Array.from(selected),
        weights: combineMut.data?.weights ?? {},
        method,
        constraints: { max_weight: maxWeight, min_weight: minWeight, allow_short: allowShort, vol_target_annual: volTarget },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.poSaved });
      setMutationError(null);
      // R5/UX-06 — show a transient "Saved!" chip so the operator gets positive
      // confirmation instead of the name input silently disappearing.
      setSaveSuccess(true);
      if (saveSuccessTimerRef.current) clearTimeout(saveSuccessTimerRef.current);
      saveSuccessTimerRef.current = setTimeout(() => { setSaveSuccess(false); saveSuccessTimerRef.current = null; }, 2000);
    },
    onError: (e) => setMutationError(e instanceof Error ? e.message : String(e)),
  });
  const deleteMut = useMutation({
    mutationFn: (id: number) => api.poDelete(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.poSaved }); setMutationError(null); },
    onError: (e) => setMutationError(e instanceof Error ? e.message : String(e)),
  });

  // C-H8 — strategies can ship with a null/empty `name` (the auto-pipeline
  // creates rows before the namer agent runs). Coerce through `|| ''` so the
  // search filter doesn't throw on lookup.
  const filtered = eligible.filter((s) =>
    !searchQuery ||
    (s.name || '').toLowerCase().includes(searchQuery.toLowerCase()) ||
    String(s.id).includes(searchQuery)
  );

  const selectedIds = useMemo(() => Array.from(selected).sort((a, b) => a - b), [selected]);

  // Reset the stale combine result whenever any input that feeds the combine
  // request changes. Prevents saving NEW strategy_ids/method with OLD weights
  // and prevents ResultsPanel/EfficientFrontier from rendering stale metrics.
  useEffect(() => {
    if (combineMut.data || combineMut.isError) combineMut.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedIds, method, maxWeight, minWeight, allowShort, volTarget]);

  // Cleanup armed-delete timer on unmount to prevent state updates after unmount.
  useEffect(() => {
    return () => {
      if (deleteArmTimerRef.current) clearTimeout(deleteArmTimerRef.current);
    };
  }, []);

  // Cleanup save-success timer on unmount to prevent state updates after unmount.
  useEffect(() => {
    return () => {
      if (saveSuccessTimerRef.current) clearTimeout(saveSuccessTimerRef.current);
    };
  }, []);

  return (
    <div className="grid h-full w-full grid-cols-12 gap-3 overflow-hidden p-3">
      <aside className="col-span-3 flex flex-col gap-3 overflow-y-auto">
        <Card title="Strategies">
          <input
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search..."
            className="mb-2 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-[11px] text-slate-200"
          />
          <ul className="max-h-72 space-y-1 overflow-y-auto">
            {filtered.map((s) => {
              const checked = selected.has(s.id);
              // C-H6 — when Sharpe is missing, render '—' instead of "0.00";
              // "0.00" cyan implied a real near-zero strategy.
              const sharpeNum = Number(s.metrics?.annualized_sharpe);
              const sharpe = Number.isFinite(sharpeNum) ? sharpeNum.toFixed(2) : '—';
              return (
                <li key={s.id}>
                  <label className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-[11px] hover:bg-slate-800/40">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => {
                        const ns = new Set(selected);
                        checked ? ns.delete(s.id) : ns.add(s.id);
                        setSelected(ns);
                      }}
                    />
                    <span className="flex-1 truncate text-slate-200">S#{s.id} {s.name}</span>
                    <span className="font-mono text-[10px] text-cyan-300">{sharpe}</span>
                  </label>
                </li>
              );
            })}
          </ul>
          <div className="mt-2 text-[10px] text-slate-500">{selected.size} selected</div>
        </Card>

        <Card title="Method">
          {/* P8-FIX/H-12 — prominent toggle group for the two most-used methods.
              Click bypasses the dropdown entirely and updates `method` directly. */}
          <div className="mb-2 grid grid-cols-2 gap-1">
            <QuickMethodButton
              active={method === 'equal_weight'}
              onClick={() => setMethod('equal_weight')}
              icon={<Scale className="h-3 w-3" />}
              label="EVENLY SPREAD"
              sub="1/N"
            />
            <QuickMethodButton
              active={method === 'risk_parity'}
              onClick={() => setMethod('risk_parity')}
              icon={<GitBranch className="h-3 w-3" />}
              label="RISK PARITY"
              sub="ERC"
            />
          </div>
          <label className="block text-[9px] uppercase tracking-wider text-slate-500">
            Advanced methods…
          </label>
          <select
            value={method}
            onChange={(e) => setMethod(e.target.value)}
            className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-[11px] text-slate-200"
          >
            {METHOD_GROUPS.map((g) => (
              <optgroup key={g.label} label={g.label}>
                {g.methods.map((m) => <option key={m.key} value={m.key}>{m.label}</option>)}
              </optgroup>
            ))}
          </select>
        </Card>

        <Card title="Constraints">
          <div className="space-y-2 text-[11px]">
            <Numeric label="Max weight %" value={maxWeight * 100} step={5} min={1} max={100} onChange={(v) => setMaxWeight(v / 100)} />
            <Numeric label="Min weight %" value={minWeight * 100} step={1} min={allowShort ? -100 : 0} max={50} onChange={(v) => setMinWeight(v / 100)} />
            <Numeric label="Vol target %" value={volTarget * 100} step={1} min={1} max={50} onChange={(v) => setVolTarget(v / 100)} />
            {/* P18-C2 — Leverage cap (gross-notional / NAV) + Beta limit (book β
                vs BTC). Not yet wired through PoConstraints; the (client-side
                only) hint signals this is operator memo, not an enforced
                solver constraint. Remove the suffix once the backend
                exposes `max_leverage` / `beta_limit`. */}
            <Numeric label="Leverage cap × (client-side only)" value={maxLeverage} step={0.1} min={0.5} max={5} onChange={setMaxLeverage} />
            <Numeric label="Beta limit |β| (client-side only)" value={betaLimit} step={0.1} min={0} max={2} onChange={setBetaLimit} />
            {minWeight > maxWeight && (
              <p className="text-[10px] text-rose-400">Min weight must be ≤ Max weight</p>
            )}
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={allowShort} onChange={(e) => setAllowShort(e.target.checked)} />
              <span className="text-slate-300">Allow short</span>
            </label>
            <button
              disabled={selected.size < 2 || combineMut.isPending || minWeight > maxWeight}
              onClick={() => combineMut.mutate()}
              className="flex w-full items-center justify-center gap-1 rounded-md border border-cyan-700 bg-cyan-500/20 px-3 py-1.5 text-[11px] font-bold text-cyan-200 hover:bg-cyan-500/30 disabled:opacity-40"
            >
              {combineMut.isPending
                ? <><Loader2 className="h-3 w-3 animate-spin" /> Optimizing…</>
                : <><Play className="h-3 w-3" /> Run Optimization</>}
            </button>
            {combineMut.data && combineMut.data.weights && (
              saveNameInput === null ? (
                <button
                  onClick={() => setSaveNameInput(`Portfolio · ${method} · ${selected.size} strats`)}
                  disabled={saveMut.isPending}
                  className="flex w-full items-center justify-center gap-1 rounded-md border border-slate-700 px-3 py-1.5 text-[11px] text-slate-300 hover:bg-slate-800 disabled:opacity-40"
                >
                  <Save className="h-3 w-3" /> Save as Portfolio
                </button>
              ) : (
                <div className="flex flex-col gap-1">
                  <input
                    autoFocus
                    value={saveNameInput}
                    onChange={(e) => setSaveNameInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') { setSaveNameInput(null); }
                      if (e.key === 'Enter') {
                        const trimmed = saveNameInput.trim();
                        if (trimmed) { saveMut.mutate(trimmed); setSaveNameInput(null); }
                      }
                    }}
                    className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-[11px] text-slate-200"
                    placeholder="Portfolio name…"
                  />
                  <div className="flex gap-1">
                    <button
                      onClick={() => {
                        const trimmed = saveNameInput.trim();
                        if (trimmed) { saveMut.mutate(trimmed); setSaveNameInput(null); }
                      }}
                      disabled={!saveNameInput.trim() || saveMut.isPending}
                      className="flex flex-1 items-center justify-center gap-1 rounded-md border border-slate-700 px-2 py-1 text-[11px] text-slate-300 hover:bg-slate-800 disabled:opacity-40"
                    >
                      {saveMut.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />} Confirm
                    </button>
                    <button
                      onClick={() => setSaveNameInput(null)}
                      className="rounded-md border border-slate-700 px-2 py-1 text-[11px] text-slate-500 hover:bg-slate-800"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )
            )}
            {saveSuccess && (
              <div className="rounded border border-emerald-700 bg-emerald-500/10 px-2 py-1 text-[11px] text-emerald-300">
                Saved!
              </div>
            )}
            {mutationError && (
              <div className="rounded border border-rose-700 bg-rose-900/40 px-2 py-1.5 text-[11px] text-rose-300">
                {mutationError}
              </div>
            )}
          </div>
        </Card>

        <Card title="Saved Portfolios">
          <ul className="space-y-1 text-[11px]">
            {(savedQ.data?.portfolios ?? []).map((p) => (
              <li key={p.id} className="flex items-center justify-between rounded border border-slate-900 bg-slate-950/40 px-2 py-1.5">
                <div className="flex-1 truncate">
                  <div className="text-slate-200">{p.name}</div>
                  <div className="text-[9px] text-slate-500">{p.method} · {p.strategy_ids.length} strats {p.stale && <span className="ml-1 text-amber-300">STALE</span>}</div>
                </div>
                <button
                  disabled={deleteMut.isPending}
                  onClick={() => {
                    if (deleteArmedId === p.id) {
                      // Second click within 3 s — execute delete
                      if (deleteArmTimerRef.current) clearTimeout(deleteArmTimerRef.current);
                      setDeleteArmedId(null);
                      deleteArmTimerRef.current = null;
                      deleteMut.mutate(p.id);
                    } else {
                      // First click — arm; disarm after 3 s
                      if (deleteArmTimerRef.current) clearTimeout(deleteArmTimerRef.current);
                      setDeleteArmedId(p.id);
                      deleteArmTimerRef.current = setTimeout(() => {
                        setDeleteArmedId(null);
                        deleteArmTimerRef.current = null;
                      }, 3000);
                    }
                  }}
                  className={deleteArmedId === p.id ? 'text-rose-300 font-bold text-[10px] hover:text-rose-200 disabled:opacity-40' : 'text-rose-400 hover:text-rose-300 disabled:opacity-40'}
                >
                  {deleteArmedId === p.id ? 'Confirm?' : <Trash2 className="h-3 w-3" />}
                </button>
              </li>
            ))}
            {!savedQ.data?.portfolios.length && <li className="text-[11px] text-slate-500">No saved portfolios.</li>}
          </ul>
        </Card>
      </aside>

      <main className="col-span-9 flex flex-col gap-3 overflow-y-auto">
        {combineMut.data ? (
          <>
            <ResultsPanel data={combineMut.data} />
            {/* P8-FIX/M-19 — Efficient Frontier scatter, marker for current combo. */}
            <EfficientFrontierScatter
              strategyIds={selectedIds}
              currentVol={combineMut.data.realized_vol_annual}
              currentSharpe={Number(combineMut.data.metrics?.annualized_sharpe ?? NaN)}
            />
          </>
        ) : (
          <Card title="Portfolio Output">
            <div className="flex h-72 items-center justify-center text-center text-[11px] text-slate-500">
              Pick at least 2 strategies and click Run Optimization.
            </div>
          </Card>
        )}
      </main>
    </div>
  );
}

function ResultsPanel({ data }: { data: PoCombineResp }) {
  const metrics = data.metrics ?? {};
  // C-H7 — guard each metric coercion so missing-metric responses render
  // an em-dash rather than fictitious "0.00 Sharpe" / "0.0% Max DD".
  const sharpeN = Number(metrics.annualized_sharpe);
  const maxDdN = Number(metrics.max_drawdown);
  const tiles = [
    {
      label: 'Sharpe',
      value: Number.isFinite(sharpeN) ? sharpeN.toFixed(2) : '—',
      cls: 'text-cyan-300',
    },
    {
      label: 'Max DD',
      value: Number.isFinite(maxDdN) ? `${(maxDdN * 100).toFixed(1)}%` : '—',
      cls: 'text-rose-300',
    },
    {
      label: 'Realized Vol',
      value:
        data.realized_vol_annual != null && Number.isFinite(data.realized_vol_annual)
          ? `${(data.realized_vol_annual * 100).toFixed(1)}%`
          : '—',
      cls: 'text-amber-300',
    },
    {
      label: 'Diversification',
      value:
        data.diversification_ratio != null && Number.isFinite(data.diversification_ratio)
          ? data.diversification_ratio.toFixed(2)
          : '—',
      cls: 'text-emerald-300',
    },
  ];
  // P13 — switched from the bespoke 8-color array to the shared 16-color
  // strategySeries palette used by every other multi-strategy chart in the
  // app. Eliminates the silent color collision past 8 strategies and keeps
  // the visual identity consistent across pages.
  const colors = CHART_COLORS.strategySeries;
  const weightData = data.components.map((c, i) => ({ name: `S#${c.id}`, value: c.weight, fill: colors[i % colors.length] }));
  const mrcData = data.components.map((c, i) => ({ name: `S#${c.id}`, mrc: c.mrc_pct, fill: colors[i % colors.length] }));

  return (
    <>
      <div className="grid grid-cols-4 gap-3">
        {tiles.map((t) => (
          <div key={t.label} className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
            <div className="text-[9px] uppercase tracking-widest text-slate-500">{t.label}</div>
            <div className={`mt-1 font-mono text-2xl ${t.cls}`}>{t.value}</div>
          </div>
        ))}
      </div>
      {data.vol_target_hit && (
        <div className="rounded-md border border-emerald-700/40 bg-emerald-500/10 px-3 py-1.5 text-[11px] text-emerald-300">
          Vol target {((data.vol_target_annual ?? 0) * 100).toFixed(0)}% hit
        </div>
      )}
      <div className="grid grid-cols-12 gap-3">
        <Card title="Weights" className="col-span-5">
          <ResponsiveContainer width="100%" height={220}>
            <PieChart>
              <Pie data={weightData} dataKey="value" nameKey="name" outerRadius={80} innerRadius={40} label={(d: { value: number }) => Number.isFinite(d.value) ? `${(d.value * 100).toFixed(0)}%` : ''}>
                {weightData.map((d, i) => <Cell key={i} fill={d.fill} />)}
              </Pie>
              <Tooltip contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 10 }} itemStyle={{ color: '#e2e8f0' }} />
            </PieChart>
          </ResponsiveContainer>
        </Card>
        <Card title="Risk Contribution" className="col-span-7">
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={mrcData} layout="vertical" barCategoryGap="20%">
              <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
              <XAxis type="number" stroke="#475569" tick={{ fontSize: 9 }} tickFormatter={(v: number) => `${(v * 100).toFixed(0)}%`} label={{ value: 'Risk contribution %', position: 'insideBottomRight', offset: -4, fill: '#64748b', fontSize: 9 }} />
              <YAxis type="category" dataKey="name" stroke="#475569" tick={{ fontSize: 9 }} width={50} />
              <Tooltip cursor={{ fill: 'rgba(148, 163, 184, 0.08)' }} contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 10 }} itemStyle={{ color: '#e2e8f0' }} formatter={(v: number) => [`${(v * 100).toFixed(1)}%`, 'Risk contribution']} />
              {data.components.length > 0 && <ReferenceLine x={1 / data.components.length} stroke="#fbbf24" strokeDasharray="3 3" label={{ value: 'Equal weight', position: 'insideTopRight', fill: '#fbbf24', fontSize: 9 }} />}
              <Bar dataKey="mrc" maxBarSize={48}>
                {mrcData.map((d, i) => <Cell key={i} fill={d.fill} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </Card>
      </div>
      {data.equity_curve?.length > 0 && (
        <Card title="Combined Equity">
          <ResponsiveContainer width="100%" height={220}>
            <LineChart data={data.equity_curve.map((p) => ({ date: p.timestamp.slice(0, 10), equity: p.equity, dd: p.drawdown }))}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
              <XAxis dataKey="date" stroke="#475569" tick={{ fontSize: 9 }} minTickGap={40} />
              <YAxis stroke="#475569" tick={{ fontSize: 9 }} />
              <Tooltip contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 10 }} />
              <Line type="monotone" dataKey="equity" stroke="#22d3ee" dot={false} strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        </Card>
      )}
    </>
  );
}

function QuickMethodButton({
  active,
  onClick,
  icon,
  label,
  sub,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  sub: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex flex-col items-center gap-0.5 rounded-md border px-2 py-2 text-[10px] font-bold uppercase tracking-wider transition ${
        active
          ? 'border-cyan-700 bg-cyan-500/15 text-cyan-200 ring-1 ring-cyan-500/40'
          : 'border-slate-700 bg-slate-950 text-slate-400 hover:bg-slate-900 hover:text-slate-200'
      }`}
    >
      <span className="flex items-center gap-1">{icon}{label}</span>
      <span className={`font-mono text-[9px] tracking-normal ${active ? 'text-cyan-300/80' : 'text-slate-600'}`}>{sub}</span>
    </button>
  );
}

function EfficientFrontierScatter({
  strategyIds,
  currentVol,
  currentSharpe,
}: {
  strategyIds: number[];
  currentVol: number | null;
  currentSharpe: number;
}) {
  const q = useQuery({
    queryKey: queryKeys.poFrontier(strategyIds),
    queryFn: () => api.poFrontier(strategyIds, 0.05, 0.30, 10),
    enabled: strategyIds.length >= 2,
    staleTime: 60_000,
  });

  // Scatter data: realized_vol (x) vs sharpe (y). Skip points whose
  // realized_vol came back null (vol target couldn't be met).
  const points = useMemo(() => {
    const raw = q.data?.points ?? [];
    return raw
      .filter((p): p is PoFrontierPoint & { realized_vol: number } => p.realized_vol != null && Number.isFinite(p.realized_vol))
      .map((p) => ({
        vol: Number((p.realized_vol * 100).toFixed(3)),
        sharpe: Number(p.sharpe.toFixed(3)),
        targetVol: Number((p.vol_target * 100).toFixed(3)),
        weights: p.weights,
      }));
  }, [q.data]);

  const currentMarker = useMemo(() => {
    if (currentVol == null || !Number.isFinite(currentVol) || !Number.isFinite(currentSharpe)) {
      return [];
    }
    return [
      {
        vol: Number((currentVol * 100).toFixed(3)),
        sharpe: Number(currentSharpe.toFixed(3)),
        targetVol: 0,
        weights: {},
        isCurrent: true,
      },
    ];
  }, [currentVol, currentSharpe]);

  return (
    <Card title="Efficient Frontier">
      {strategyIds.length < 2 ? (
        <div className="flex h-48 items-center justify-center text-[11px] text-slate-500">
          Select at least 2 strategies to compute the frontier.
        </div>
      ) : q.isLoading ? (
        <div className="flex h-48 items-center justify-center text-[11px] text-slate-500">
          Computing frontier across 10 vol targets…
        </div>
      ) : q.isError ? (
        <div className="flex h-48 items-center justify-center text-[11px] text-rose-300">
          Frontier endpoint failed — check backend logs.
        </div>
      ) : points.length === 0 ? (
        <div className="flex h-48 items-center justify-center text-[11px] text-slate-500">
          No feasible portfolios in the 5–30% vol range.
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={260}>
          <ScatterChart margin={{ top: 12, right: 12, bottom: 12, left: 12 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
            <XAxis
              type="number"
              dataKey="vol"
              name="Realized Vol %"
              stroke="#475569"
              tick={{ fontSize: 9 }}
              label={{ value: 'Realized Vol %', position: 'insideBottom', offset: -5, fill: '#64748b', fontSize: 9 }}
            />
            <YAxis
              type="number"
              dataKey="sharpe"
              name="Sharpe"
              stroke="#475569"
              tick={{ fontSize: 9 }}
              label={{ value: 'Sharpe', angle: -90, position: 'insideLeft', offset: 14, fill: '#64748b', fontSize: 9 }}
            />
            <ZAxis range={[60, 60]} />
            <ReferenceLine y={0} stroke="#475569" strokeDasharray="3 3" />
            <Tooltip
              cursor={{ strokeDasharray: '3 3' }}
              contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 10 }}
              content={<FrontierTooltip />}
            />
            <Scatter name="Frontier" data={points} fill="#22d3ee" line shape="circle" />
            {currentMarker.length > 0 && (
              <Scatter name="Current" data={currentMarker} fill="#fbbf24" shape="star" />
            )}
          </ScatterChart>
        </ResponsiveContainer>
      )}
    </Card>
  );
}

function FrontierTooltip({ active, payload }: any) {
  if (!active || !payload?.length) return null;
  const row = payload[0]?.payload ?? {};
  const weights = row.weights ?? {};
  const entries = Object.entries(weights) as [string, number][];
  return (
    <div className="rounded border border-slate-700 bg-slate-950 p-2 text-[10px] text-slate-200 shadow-lg">
      <div className="mb-1 flex items-center justify-between gap-3 font-mono">
        <span className="text-cyan-300">Vol {Number(row.vol).toFixed(2)}%</span>
        <span className="text-emerald-300">Sharpe {Number(row.sharpe).toFixed(2)}</span>
      </div>
      {row.isCurrent ? (
        <div className="text-amber-300">Current combination</div>
      ) : (
        <>
          {row.targetVol > 0 && (
            <div className="mb-1 text-slate-500">target {Number(row.targetVol).toFixed(0)}%</div>
          )}
          {entries.length > 0 && (
            <div className="grid grid-cols-2 gap-x-2 gap-y-0.5 text-[9px]">
              {entries
                .sort((a, b) => Number(b[1]) - Number(a[1]))
                .slice(0, 8)
                .map(([sid, w]) => (
                  <span key={sid} className="font-mono text-slate-300">
                    S#{sid} {(Number(w) * 100).toFixed(1)}%
                  </span>
                ))}
              {entries.length > 8 && (
                <span className="text-slate-600">+{entries.length - 8} more</span>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function Card({ title, className = '', children }: { title: string; className?: string; children: React.ReactNode }) {
  return (
    <section className={`flex flex-col rounded-xl border border-slate-800 bg-slate-900/40 p-3 ${className}`}>
      <header className="mb-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">{title}</header>
      <div className="flex-1">{children}</div>
    </section>
  );
}

function Numeric({ label, value, step, min, max, onChange }: { label: string; value: number; step: number; min: number; max: number; onChange: (v: number) => void }) {
  return (
    <label className="block">
      <span className="mb-0.5 block text-[9px] uppercase tracking-wider text-slate-500">{label}</span>
      <input
        type="number"
        value={value}
        step={step}
        min={min}
        max={max}
        onChange={(e) => { const v = Number(e.target.value); onChange(Number.isFinite(v) ? Math.max(min, Math.min(max, v)) : min); }}
        className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-200"
      />
    </label>
  );
}
