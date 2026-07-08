'use client';

import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Play, X } from 'lucide-react';
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid,
  ScatterChart, Scatter, ZAxis, Cell, ReferenceLine,
} from 'recharts';

import { api, cryptoUuid, type BacktestLabCell, type BacktestLabParam, type BacktestLabSweep } from '@/lib/api';
import { queryKeys } from '@/lib/query';

// P27 — derive a sweep window inside a param's whitelisted [min, max] range.
// Used to auto-populate sensible defaults whenever the user picks a new param
// whose range doesn't accommodate the previous numeric values (e.g. switching
// from a float param into ``signal.window`` whose range is [8, 720] would
// previously leave ``yMin=0.5`` in place and trip a backend HTTP 422).
function defaultRangeForSpec(spec: BacktestLabParam): { min: string; max: string } {
  const lo = spec.min;
  const hi = spec.max;
  if (!isFinite(lo) || !isFinite(hi) || hi <= lo) {
    return { min: String(lo), max: String(hi) };
  }
  // Q1-Q3 window — symmetric around the midpoint, leaves room on both sides
  // for the user to widen if they want, and works for both signed (e.g.
  // threshold_entry ∈ [-3.5, 3.5]) and one-sided (fee/slippage fraction ∈ [0, 0.002], i.e. 0.0005 = 5 bps) ranges.
  const q1 = lo + (hi - lo) * 0.25;
  const q3 = lo + (hi - lo) * 0.75;
  if (spec.type === 'int') {
    return {
      min: String(Math.max(Math.round(q1), lo)),
      max: String(Math.min(Math.round(q3), hi)),
    };
  }
  const span = hi - lo;
  const decimals = span < 0.01 ? 6 : span < 0.1 ? 5 : span < 1 ? 4 : span < 10 ? 3 : 2;
  return {
    min: Number(q1.toFixed(decimals)).toString(),
    max: Number(q3.toFixed(decimals)).toString(),
  };
}

function outOfRange(spec: BacktestLabParam | undefined, raw: string): boolean {
  if (!spec) return false;
  const v = Number(raw);
  if (!Number.isFinite(v) || /[^0-9eE+\-.]/.test(raw.trim())) return true;
  return v < spec.min || v > spec.max;
}

// P8 extension — not in original screenshots, allowed additive enhancement.
// Metric switch over the 5 standard backtest KPIs for sweep cell coloring.
type Metric = 'sharpe' | 'max_drawdown' | 'cum_return' | 'profit_factor' | 'win_rate';

/**
 * SweepTab — extracted from /backtest-lab P7-03 page.
 * Parameter-sweep runner: configure X (and optional Y) parameter ranges,
 * launch a sweep, watch the heatmap / line / scatter / top-cells update in
 * real time. Honours the BACKTEST_LAB_ENABLED gating from /api/backtest-lab/params.
 */
export default function SweepTab() {
  const qc = useQueryClient();
  const paramsQ = useQuery({ queryKey: queryKeys.blParams, queryFn: api.backtestLabParams });
  // NOTE: must use raw `api.strategies` (not `.then((r) => r.strategies)`) so
  // every other consumer sharing `queryKeys.strategies` sees the same cached
  // shape `{ strategies: [...] }`. See arena/page.tsx for the full rationale.
  const strategiesQ = useQuery({ queryKey: queryKeys.strategies, queryFn: api.strategies });

  const [strategyId, setStrategyId] = useState<number | null>(null);
  const [activeSweepId, setActiveSweepId] = useState<number | null>(null);
  const [metric, setMetric] = useState<Metric>('sharpe');
  const [paramX, setParamX] = useState<string>('signal.window');
  const [xMin, setXMin] = useState<string>('20');
  const [xMax, setXMax] = useState<string>('120');
  const [xSteps, setXSteps] = useState<number>(5);
  const [paramY, setParamY] = useState<string>('');
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [yMin, setYMin] = useState<string>('0.5');
  const [yMax, setYMax] = useState<string>('2.0');
  const [ySteps, setYSteps] = useState<number>(5);

  // P27 — index the whitelist by name so we can look up min/max in O(1)
  // for auto-reset and inline validation.
  const paramMap = useMemo(() => {
    const m = new Map<string, BacktestLabParam>();
    for (const p of paramsQ.data?.params ?? []) m.set(p.name, p);
    return m;
  }, [paramsQ.data]);
  const xSpec = paramMap.get(paramX);
  const ySpec = paramY ? paramMap.get(paramY) : undefined;

  // P27 — when the X param changes (or the whitelist finally loads), check if
  // the current xMin/xMax are inside the new spec range; if not, snap to the
  // Q1-Q3 default window for that param. Same for Y. Values that are already
  // valid for the new param are left untouched — we never clobber a deliberate
  // user input that happens to be in range.
  useEffect(() => {
    if (!xSpec) return;
    if (outOfRange(xSpec, xMin) || outOfRange(xSpec, xMax)) {
      const d = defaultRangeForSpec(xSpec);
      setXMin(d.min);
      setXMax(d.max);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paramX, xSpec?.min, xSpec?.max]);
  useEffect(() => {
    if (!ySpec) return;
    if (outOfRange(ySpec, yMin) || outOfRange(ySpec, yMax)) {
      const d = defaultRangeForSpec(ySpec);
      setYMin(d.min);
      setYMax(d.max);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paramY, ySpec?.min, ySpec?.max]);

  // P27 — collect validation errors so we can disable Run and surface inline
  // messages BEFORE the user hits the network and waits for an HTTP 422.
  const xnum = parseFloat(xMin), xmum = parseFloat(xMax);
  const ynum = parseFloat(yMin), ymum = parseFloat(yMax);
  const sameAxis = !!paramY && paramY === paramX;
  const xMinErr = outOfRange(xSpec, xMin);
  const xMaxErr = outOfRange(xSpec, xMax);
  const xOrderErr = isFinite(xnum) && isFinite(xmum) && xnum >= xmum;
  const yMinErr = !!paramY && outOfRange(ySpec, yMin);
  const yMaxErr = !!paramY && outOfRange(ySpec, yMax);
  const yOrderErr = !!paramY && isFinite(ynum) && isFinite(ymum) && ynum >= ymum;
  const paramsLoaded = !!paramsQ.data && !!xSpec;
  const boundsFinite = isFinite(xnum) && isFinite(xmum) && (!paramY || (isFinite(ynum) && isFinite(ymum)));
  const validationErrors: string[] = [];
  if (sameAxis) validationErrors.push('Param X and Param Y must be different parameters.');
  if (xMinErr && xSpec) validationErrors.push(`X min must be in [${xSpec.min} — ${xSpec.max}]`);
  if (xMaxErr && xSpec) validationErrors.push(`X max must be in [${xSpec.min} — ${xSpec.max}]`);
  if (xOrderErr) validationErrors.push('X min must be strictly less than X max.');
  if (yMinErr && ySpec) validationErrors.push(`Y min must be in [${ySpec.min} — ${ySpec.max}]`);
  if (yMaxErr && ySpec) validationErrors.push(`Y max must be in [${ySpec.min} — ${ySpec.max}]`);
  if (yOrderErr) validationErrors.push('Y min must be strictly less than Y max.');
  const canRun = validationErrors.length === 0;

  const sweepQ = useQuery({
    // R5/FE-DATA-003 — distinct sentinel key when disabled so a real sweep_id=0
    // can never collide with the disabled-mount cache slot.
    queryKey: activeSweepId != null ? queryKeys.blSweep(activeSweepId) : ['bl-sweep', '__disabled__'],
    queryFn: () => api.backtestLabGetSweep(activeSweepId!),
    enabled: activeSweepId != null,
    refetchInterval: (q) => {
      const s = (q.state.data as BacktestLabSweep | undefined)?.status;
      return s === 'running' || s === 'queued' ? 1500 : false;
    },
  });

  const sweepsQ = useQuery({
    queryKey: strategyId != null ? queryKeys.blSweeps(strategyId) : ['bl-sweeps', '__disabled__'],
    queryFn: () => api.backtestLabListSweeps(strategyId!),
    enabled: strategyId != null,
  });

  // Invalidate the Sweep History list once the active sweep leaves polling
  // states (running → done / failed / partial). sweepQ stops polling at that
  // point (refetchInterval returns false above), so this effect fires exactly
  // once per sweep completion and refreshes the history panel without
  // continuous polling of the list endpoint.
  useEffect(() => {
    const s = sweepQ.data?.status;
    if (s && s !== 'running' && s !== 'queued' && strategyId != null) {
      qc.invalidateQueries({ queryKey: queryKeys.blSweeps(strategyId) });
    }
  }, [sweepQ.data?.status, strategyId, qc]);

  const createMut = useMutation({
    // R8/M0: mutationFn takes the strategy id as an explicit argument and
    // onSuccess reads it from the variables param — NOT a stale `strategyId`
    // closure. Previously onSuccess used `queryKeys.blSweeps(strategyId ?? 0)`,
    // which, if `strategyId` had become null between mint and settle, cleared
    // the bogus id=0 cache entry and left the real sweep list stale.
    mutationFn: (sid: number) => {
      const xs = linspace(parseFloat(xMin), parseFloat(xMax), xSteps);
      const ys = paramY ? linspace(parseFloat(yMin), parseFloat(yMax), ySteps) : undefined;
      // D-H10/P16 — persist a per-strategy Idempotency-Key in sessionStorage so a
      // double-click or a retry after a page reload replays the cached sweep_id
      // instead of spawning a duplicate sweep run. Cleared on success so the next
      // *intentional* click mints a fresh key. Mirrors the paper-trade pattern.
      const storageKey = `bl-sweep-${sid}-key`;
      let idem = sessionStorage.getItem(storageKey);
      if (!idem) {
        idem = cryptoUuid();
        sessionStorage.setItem(storageKey, idem);
      }
      return api.backtestLabCreateSweep({
        strategy_id: sid,
        param_x_name: paramX,
        param_x_values: xs,
        param_y_name: paramY || undefined,
        param_y_values: ys,
      }, { idempotencyKey: idem });
    },
    onSuccess: (r, sid) => {
      sessionStorage.removeItem(`bl-sweep-${sid}-key`);
      setActiveSweepId(r.sweep_id);
      qc.invalidateQueries({ queryKey: queryKeys.blSweeps(sid) });
    },
  });
  const cancelMut = useMutation({
    mutationFn: (sweepId: number) => api.backtestLabCancel(sweepId),
    onMutate: () => setCancelError(null),
    onSuccess: (_, sweepId) => qc.invalidateQueries({ queryKey: queryKeys.blSweep(sweepId) }),
    onError: (e) => setCancelError(e instanceof Error ? e.message : String(e)),
  });

  if (paramsQ.data && !paramsQ.data.enabled) {
    return <DisabledState />;
  }
  const cellsTotal = xSteps * (paramY ? ySteps : 1);

  return (
    <div className="grid h-full w-full grid-cols-[320px_minmax(0,1fr)_280px] gap-3 overflow-hidden p-3">
      <aside className="flex flex-col gap-3 overflow-y-auto">
        <Card title="Sweep Setup">
          <div className="space-y-3 text-[11px]">
            <Field label="Strategy">
              <select
                value={strategyId ?? ''}
                onChange={(e) => { setStrategyId(e.target.value ? Number(e.target.value) : null); setActiveSweepId(null); }}
                className="w-full rounded-md border border-slate-700 bg-slate-950 px-2 py-1 text-slate-200"
              >
                <option value="">— pick —</option>
                {(strategiesQ.data?.strategies ?? []).map((s) => (
                  <option key={s.id} value={s.id}>
                    S#{s.id} {s.name?.slice(0, 36)}
                  </option>
                ))}
              </select>
            </Field>
            <ParamPicker
              label="Param X"
              name={paramX}
              onName={setParamX}
              min={xMin}
              max={xMax}
              steps={xSteps}
              onMin={setXMin}
              onMax={setXMax}
              onSteps={setXSteps}
              params={paramsQ.data?.params}
              spec={xSpec}
              minErr={xMinErr}
              maxErr={xMaxErr}
            />
            <ParamPicker
              label="Param Y (optional)"
              name={paramY}
              onName={setParamY}
              min={yMin}
              max={yMax}
              steps={ySteps}
              onMin={setYMin}
              onMax={setYMax}
              onSteps={setYSteps}
              params={paramsQ.data?.params}
              spec={ySpec}
              minErr={yMinErr}
              maxErr={yMaxErr}
              allowEmpty
              excludeName={paramX}
            />
            <div className="text-[10px] text-slate-500">
              Cells: <span className={`font-mono ${!paramsQ.data ? 'text-slate-400' : cellsTotal > paramsQ.data.max_cells ? 'text-rose-300' : 'text-cyan-300'}`}>{cellsTotal}</span> / cap {paramsQ.data?.max_cells ?? 25}
            </div>
            <button
              disabled={!strategyId || createMut.isPending || cellsTotal > (paramsQ.data?.max_cells ?? 25) || !canRun || !paramsLoaded || !boundsFinite}
              onClick={() => { if (strategyId != null) createMut.mutate(strategyId); }}
              className="flex w-full items-center justify-center gap-1 rounded-md border border-cyan-700 bg-cyan-500/20 px-3 py-1.5 text-[11px] font-bold text-cyan-200 hover:bg-cyan-500/30 disabled:opacity-40"
            >
              <Play className="h-3 w-3" /> Run Sweep
            </button>
            {!paramsLoaded && (
              <div className="text-[10px] text-slate-400">
                {paramsQ.isLoading ? 'Loading parameter whitelist…' : 'Parameter whitelist unavailable.'}
              </div>
            )}
            {paramsLoaded && !strategyId && (
              <div className="text-[10px] text-slate-400">Pick a strategy to enable Run.</div>
            )}
            {validationErrors.length > 0 && (
              <ul className="space-y-0.5 rounded-md border border-amber-700/40 bg-amber-500/5 px-2 py-1 text-[10px] text-amber-200">
                {validationErrors.map((msg, i) => (
                  <li key={i}>· {msg}</li>
                ))}
              </ul>
            )}
            {createMut.isError && <div className="text-[10px] text-rose-300">{(createMut.error as Error).message}</div>}
          </div>
        </Card>
      </aside>

      <section className="flex flex-col gap-3 overflow-y-auto">
        <Card title="Active Sweep">
          {sweepQ.data ? (
            <ActiveSweep sweep={sweepQ.data} metric={metric} onMetric={setMetric} onCancel={() => { if (activeSweepId != null) cancelMut.mutate(activeSweepId); }} cancelPending={cancelMut.isPending} />
          ) : (
            <Empty>Configure a sweep on the left and click Run.</Empty>
          )}
          {cancelMut.isError && cancelError && (
            <div className="mt-2 rounded-md border border-rose-700/40 bg-rose-500/10 px-2 py-1.5 text-[10px] text-rose-300">
              Cancel failed: {cancelError}
            </div>
          )}
        </Card>
      </section>

      <aside className="flex flex-col gap-3 overflow-y-auto">
        <Card title="Sweep History">
          <SweepHistory rows={sweepsQ.data?.sweeps ?? []} onPick={setActiveSweepId} activeId={activeSweepId} />
        </Card>
      </aside>
    </div>
  );
}

function DisabledState() {
  return (
    <div className="flex h-full items-center justify-center p-6">
      <div className="max-w-md rounded-2xl border border-slate-800 bg-slate-900/40 p-6 text-center">
        <h1 className="mb-2 text-sm font-bold uppercase tracking-widest text-slate-100">Backtest Lab Disabled</h1>
        <p className="text-xs text-slate-400">
          Set <code className="rounded bg-slate-800 px-1 text-cyan-300">BACKTEST_LAB_ENABLED=1</code> in <code>.env</code> and restart the backend to enable parameter sweeps. Sweeps can launch up to 25 backtests per run.
        </p>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-0.5 block text-[9px] uppercase tracking-wider text-slate-500">{label}</span>
      {children}
    </label>
  );
}

function ParamPicker({
  label, name, onName, min, max, steps, onMin, onMax, onSteps, params, allowEmpty,
  spec, minErr, maxErr, excludeName,
}: {
  label: string; name: string; onName: (v: string) => void;
  min: string; max: string; steps: number;
  onMin: (v: string) => void; onMax: (v: string) => void; onSteps: (v: number) => void;
  params?: BacktestLabParam[];
  allowEmpty?: boolean;
  // P27 — passing the resolved spec lets us show the valid range hint inline
  // and tint the input borders red when the user has typed an out-of-range
  // value. ``excludeName`` removes the X axis from the Y dropdown so a 2D
  // sweep can never collapse into a 1D sweep with stale Y defaults — the
  // very condition that was producing the HTTP 422 reported by the user.
  spec?: BacktestLabParam;
  minErr?: boolean;
  maxErr?: boolean;
  excludeName?: string;
}) {
  const baseInputCls = 'rounded border bg-slate-950 px-1 py-0.5 text-[10px] font-mono text-slate-200';
  const okBorder = 'border-slate-700';
  const errBorder = 'border-rose-500';
  return (
    <Field label={label}>
      <select
        value={name}
        onChange={(e) => onName(e.target.value)}
        className="mb-1 w-full rounded-md border border-slate-700 bg-slate-950 px-2 py-1 text-slate-200"
      >
        {allowEmpty && <option value="">— none —</option>}
        {(params ?? [])
          .filter((p) => !excludeName || p.name !== excludeName)
          .map((p) => (
            <option key={p.name} value={p.name}>{p.label ?? p.name}</option>
          ))}
      </select>
      {name && (
        <>
          <div className="grid grid-cols-3 gap-1">
            <input
              type="number"
              placeholder="min"
              value={min}
              onChange={(e) => onMin(e.target.value)}
              className={`${baseInputCls} ${minErr ? errBorder : okBorder}`}
            />
            <input
              type="number"
              placeholder="max"
              value={max}
              onChange={(e) => onMax(e.target.value)}
              className={`${baseInputCls} ${maxErr ? errBorder : okBorder}`}
            />
            <input
              type="number"
              placeholder="N"
              value={steps}
              onChange={(e) => onSteps(Math.max(2, Math.min(10, Number(e.target.value) || 2)))}
              className={`${baseInputCls} ${okBorder}`}
            />
          </div>
          {spec && (
            <div className="mt-0.5 font-mono text-[9px] text-slate-500">
              valid: [{spec.min} — {spec.max}] · {spec.type}{spec.unit ? ` · ${spec.unit}` : ''}
            </div>
          )}
        </>
      )}
    </Field>
  );
}

function ActiveSweep({ sweep, metric, onMetric, onCancel, cancelPending }: { sweep: BacktestLabSweep; metric: Metric; onMetric: (m: Metric) => void; onCancel: () => void; cancelPending?: boolean }) {
  const pct = sweep.cells_total > 0 ? Math.round((sweep.cells_done / sweep.cells_total) * 100) : 0;
  const tone = sweep.status === 'done' ? 'text-emerald-300' : sweep.status === 'failed' ? 'text-rose-300' : sweep.status === 'partial' ? 'text-amber-300' : 'text-cyan-300';
  const dim = sweep.param_y_name ? '2d' : '1d';
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-[10px]">
          <span className={`font-mono ${tone}`}>{sweep.status.toUpperCase()}</span>{' '}
          <span className="text-slate-500">· {sweep.cells_done}/{sweep.cells_total} cells</span>
        </div>
        <div className="flex items-center gap-2">
          <MetricSwitch value={metric} onChange={onMetric} />
          {(sweep.status === 'running' || sweep.status === 'queued') && (
            <button onClick={onCancel} disabled={cancelPending} className="inline-flex items-center gap-1 rounded border border-rose-700 px-2 py-0.5 text-[10px] text-rose-200 hover:bg-rose-500/10 disabled:opacity-50 disabled:cursor-not-allowed">
              <X className="h-3 w-3" /> Cancel
            </button>
          )}
        </div>
      </div>
      <div className="h-1 overflow-hidden rounded bg-slate-800">
        <div className="h-1 bg-cyan-400" style={{ width: `${pct}%` }} />
      </div>
      {dim === '2d' ? (
        <SweepHeatmap sweep={sweep} metric={metric} />
      ) : (
        <SweepLineChart sweep={sweep} metric={metric} />
      )}
      <SweepFrontier sweep={sweep} />
      <SweepTopCells sweep={sweep} metric={metric} />
      {/* P28 — surface per-cell failures so a sweep where every cell
          errors out doesn't render as a blank white plot box. */}
      <SweepErrors sweep={sweep} />
    </div>
  );
}

function MetricSwitch({ value, onChange }: { value: Metric; onChange: (m: Metric) => void }) {
  const opts: { v: Metric; l: string }[] = [
    { v: 'sharpe', l: 'Sharpe' }, { v: 'max_drawdown', l: 'MaxDD' },
    { v: 'cum_return', l: 'Return' }, { v: 'profit_factor', l: 'PF' }, { v: 'win_rate', l: 'Win%' },
  ];
  return (
    <div className="flex overflow-hidden rounded-md border border-slate-700">
      {opts.map((o) => (
        <button key={o.v} onClick={() => onChange(o.v)} className={`px-2 py-0.5 text-[10px] ${value === o.v ? 'bg-cyan-500/20 text-cyan-200' : 'text-slate-400 hover:bg-slate-800'}`}>
          {o.l}
        </button>
      ))}
    </div>
  );
}

function SweepHeatmap({ sweep, metric }: { sweep: BacktestLabSweep; metric: Metric }) {
  const xs = sweep.param_x_values;
  const ys = sweep.param_y_values ?? [];
  const cellMap = useMemo(() => {
    const m = new Map<string, BacktestLabCell>();
    for (const c of sweep.cells) m.set(`${c.x}|${c.y}`, c);
    return m;
  }, [sweep.cells]);
  const values = sweep.cells.map((c) => c.metrics ? (c.metrics as any)[metric] : null).filter((v) => v != null) as number[];
  // P28 — when every cell errored we still want to render the grid skeleton
  // so the user sees the geometry of the sweep they configured, but skip the
  // colour-scale calculation that depends on at least one numeric value.
  if (!values.length) {
    return (
      <div className="rounded-md border border-slate-800 bg-slate-900/40 p-3 text-center text-[11px] text-slate-500">
        No cells produced metrics. See errors below.
      </div>
    );
  }
  const min = values.length ? Math.min(...values) : 0;
  const max = values.length ? Math.max(...values) : 1;
  return (
    <div className="overflow-x-auto">
      <table className="border-separate border-spacing-1">
        <thead>
          <tr>
            <th scope="col" className="text-[8px] uppercase tracking-wider text-slate-500"></th>
            {xs.map((x) => <th scope="col" key={x} className="text-[9px] font-mono text-slate-400">{x.toFixed(2)}</th>)}
          </tr>
        </thead>
        <tbody>
          {ys.map((y) => (
            <tr key={y}>
              <td className="text-right text-[9px] font-mono text-slate-400 pr-1">{y.toFixed(2)}</td>
              {xs.map((x) => {
                const cell = cellMap.get(`${x}|${y}`);
                const v = cell?.metrics ? (cell.metrics as any)[metric] : null;
                const bg = v == null
                  ? '#1e293b'
                  : interpolate(v, min, max, metric === 'max_drawdown');
                return (
                  <td key={x} title={cell?.error ?? (v == null ? 'pending' : `${metric}=${v.toFixed(3)}`)}
                      className="h-8 w-12 rounded font-mono text-[10px] text-center"
                      style={{ background: bg, color: v != null ? '#0f172a' : '#475569' }}>
                    {v != null ? v.toFixed(2) : (cell?.error ? '!' : '·')}
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

function SweepLineChart({ sweep, metric }: { sweep: BacktestLabSweep; metric: Metric }) {
  const data = sweep.cells
    .filter((c) => c.metrics)
    .map((c) => ({ x: c.x, [metric]: (c.metrics as any)[metric] }));
  // P28 — Recharts BarChart with an empty ``data`` array still allocates the
  // container's full height (220px) and renders as a blank white box. That's
  // the exact symptom the user reported: every cell errored, so ``data`` was
  // empty, and the UI surfaced a totally opaque "FAILED · 5/5 cells" plot
  // with no actionable information. Return a compact fallback instead and
  // let ``SweepErrors`` below carry the diagnostic load.
  if (!data.length) {
    return (
      <div className="rounded-md border border-slate-800 bg-slate-900/40 p-3 text-center text-[11px] text-slate-500">
        No cells produced metrics. See errors below.
      </div>
    );
  }
  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={data} barCategoryGap="20%">
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis dataKey="x" stroke="#475569" tick={{ fontSize: 9 }} />
        <YAxis stroke="#475569" tick={{ fontSize: 9 }} />
        <Tooltip cursor={{ fill: 'rgba(148, 163, 184, 0.08)' }} contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 10 }} itemStyle={{ color: '#e2e8f0' }} />
        <Bar dataKey={metric} maxBarSize={56}>
          {data.map((d: any, i) => {
            const isGood = metric === 'max_drawdown' ? d[metric] > -0.10 : d[metric] >= 0;
            return <Cell key={i} fill={isGood ? '#22d3ee' : '#f43f5e'} />;
          })}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// P28 — Summarise per-cell failures. When several cells share the same error
// message (the common case for "all 5 cells failed with the same sandbox
// import error") we collapse them into one row + a sample-coordinate hint so
// the panel stays compact and actionable. Returns null when no cells errored.
function SweepErrors({ sweep }: { sweep: BacktestLabSweep }) {
  const errs = sweep.cells.filter((c) => c.error);
  if (!errs.length) return null;
  type ErrInfo = { count: number; samples: { x: number; y: number | null }[] };
  const grouped = new Map<string, ErrInfo>();
  for (const c of errs) {
    const key = c.error || 'unknown';
    const entry = grouped.get(key) ?? { count: 0, samples: [] };
    entry.count += 1;
    if (entry.samples.length < 3) {
      entry.samples.push({ x: c.x, y: c.y ?? null });
    }
    grouped.set(key, entry);
  }
  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <div className="text-[10px] uppercase tracking-wider text-amber-400">
          {errs.length} failed cell{errs.length === 1 ? '' : 's'}
        </div>
        {sweep.error_message && (
          <div className="font-mono text-[9px] text-rose-300">
            sweep: {sweep.error_message}
          </div>
        )}
      </div>
      <ul className="space-y-1 text-[10px]">
        {[...grouped.entries()].map(([err, info]) => (
          <li key={err} className="rounded border border-rose-800/40 bg-rose-500/5 p-1.5">
            <div className="break-all font-mono text-rose-200">{err}</div>
            <div className="mt-0.5 text-[9px] text-slate-500">
              {info.count} cell{info.count === 1 ? '' : 's'} ·{' '}
              {sweep.param_y_name ? 'sample (x, y)' : 'sample x'}:{' '}
              {info.samples
                .map((s) =>
                  s.y != null
                    ? `(${formatCoord(s.x)}, ${formatCoord(s.y)})`
                    : formatCoord(s.x),
                )
                .join(', ')}
              {info.count > info.samples.length ? ' …' : ''}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function formatCoord(v: number): string {
  if (!isFinite(v)) return String(v);
  if (Math.abs(v) >= 100 || Number.isInteger(v)) return String(Math.round(v));
  return v.toFixed(3).replace(/\.?0+$/, '');
}

function SweepFrontier({ sweep }: { sweep: BacktestLabSweep }) {
  const data = sweep.cells.filter((c) => c.metrics).map((c) => ({
    dd: Math.abs((c.metrics as any).max_drawdown ?? 0),
    sharpe: (c.metrics as any).sharpe ?? 0,
  }));
  if (!data.length) return null;
  return (
    <div>
      <div className="mb-1 text-[10px] uppercase tracking-wider text-slate-500">Sharpe vs |MaxDD|</div>
      <ResponsiveContainer width="100%" height={150}>
        <ScatterChart>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
          <XAxis dataKey="dd" stroke="#475569" tick={{ fontSize: 9 }} name="MaxDD" />
          <YAxis dataKey="sharpe" stroke="#475569" tick={{ fontSize: 9 }} name="Sharpe" />
          <ZAxis range={[40, 40]} />
          <Tooltip contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 10 }} />
          <ReferenceLine y={0} stroke="#475569" strokeDasharray="3 3" />
          <Scatter data={data} fill="#22d3ee" />
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  );
}

function SweepTopCells({ sweep, metric }: { sweep: BacktestLabSweep; metric: Metric }) {
  const data = useMemo(
    () =>
      sweep.cells
        .filter((c) => c.metrics)
        .sort((a, b) => ((b.metrics as any)[metric] ?? 0) - ((a.metrics as any)[metric] ?? 0))
        .slice(0, 5),
    [sweep.cells, metric],
  );
  if (!data.length) return null;
  return (
    <div>
      <div className="mb-1 text-[10px] uppercase tracking-wider text-slate-500">Top 5 by {metric}</div>
      <table className="w-full text-[10px]">
        <thead className="text-[8px] uppercase tracking-wider text-slate-500">
          <tr>
            <th scope="col" className="text-left">X</th><th scope="col" className="text-left">Y</th>
            <th scope="col" className="text-right">Sharpe</th><th scope="col" className="text-right">DD</th>
            <th scope="col" className="text-right">Return</th><th scope="col" className="text-right">Trades</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {data.map((c, i) => {
            const m: any = c.metrics;
            return (
              <tr key={i} className="border-t border-slate-900">
                <td>{c.x.toFixed(2)}</td>
                <td>{c.y != null ? c.y.toFixed(2) : '—'}</td>
                <td className="text-right text-cyan-200">{Number.isFinite(m.sharpe) ? m.sharpe.toFixed(2) : '—'}</td>
                <td className="text-right text-rose-200">{Number.isFinite(m.max_drawdown) ? `${(m.max_drawdown * 100).toFixed(1)}%` : '—'}</td>
                <td className="text-right">{Number.isFinite(m.cum_return) ? `${(m.cum_return * 100).toFixed(1)}%` : '—'}</td>
                <td className="text-right text-slate-400">{m.trades}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function SweepHistory({ rows, onPick, activeId }: { rows: BacktestLabSweep[]; onPick: (id: number) => void; activeId: number | null }) {
  if (!rows.length) return <Empty>No past sweeps for this strategy.</Empty>;
  return (
    <ul className="space-y-1 text-[10px]">
      {rows.map((s) => (
        <li key={s.sweep_id}>
          <button
            onClick={() => onPick(s.sweep_id)}
            className={`w-full rounded-md border border-slate-900 bg-slate-950/40 px-2 py-1.5 text-left hover:bg-slate-900 ${activeId === s.sweep_id ? 'ring-1 ring-cyan-500' : ''}`}
          >
            <div className="font-mono text-slate-200">#{s.sweep_id} {s.param_x_name}{s.param_y_name ? `×${s.param_y_name}` : ''}</div>
            <div className="text-[9px] text-slate-500">{s.status} · {s.cells_done}/{s.cells_total} cells</div>
          </button>
        </li>
      ))}
    </ul>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col rounded-xl border border-slate-800 bg-slate-900/40 p-3">
      <header className="mb-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">{title}</header>
      <div className="flex-1">{children}</div>
    </section>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="flex h-32 items-center justify-center text-center text-[11px] text-slate-500">{children}</div>;
}

function linspace(min: number, max: number, n: number): number[] {
  if (n < 2 || !isFinite(min) || !isFinite(max)) return [min];
  const step = (max - min) / (n - 1);
  return Array.from({ length: n }, (_, i) => Number((min + i * step).toFixed(6)));
}

function interpolate(v: number, lo: number, hi: number, invert = false): string {
  if (lo === hi) return '#22d3ee';
  let t = (v - lo) / (hi - lo);
  if (invert) t = 1 - t;
  t = Math.max(0, Math.min(1, t));
  const r = Math.round(34 * (1 - t) + 16 * t);
  const g = Math.round(211 * (1 - t) + 185 * t);
  const b = Math.round(238 * (1 - t) + 129 * t);
  return `rgb(${r},${g},${b})`;
}
