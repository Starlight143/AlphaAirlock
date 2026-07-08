'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Sigma, Play, Save, ArrowUpCircle, AlertTriangle } from 'lucide-react';

import { api, cryptoUuid, type FsEvalResult } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import EquityDrawdown from '@/components/charts/EquityDrawdown';
import DailyPnlBars from '@/components/charts/DailyPnlBars';
import DrawdownDistribution from '@/components/charts/DrawdownDistribution';
import RollingSharpe from '@/components/charts/RollingSharpe';
import MonthlyReturnsHeatmap from '@/components/charts/MonthlyReturnsHeatmap';

const DEFAULT_FORMULA = `def compute_factor(df):
    fast = df['close'].rolling(20).mean()
    slow = df['close'].rolling(60).mean()
    return ((fast / slow) - 1.0).fillna(0)
`;

const SNIPPETS: { label: string; code: string }[] = [
  { label: 'mean(20)', code: "df['close'].rolling(20).mean()" },
  { label: 'std(20)', code: "df['close'].rolling(20).std()" },
  { label: 'zscore', code: "((df['close'] - df['close'].rolling(20).mean()) / df['close'].rolling(20).std())" },
  { label: 'rank', code: "df['close'].rank(pct=True)" },
  { label: 'delay(1)', code: "df['close'].shift(1)" },
  { label: 'delta(1)', code: "df['close'].diff(1)" },
  { label: 'sign(diff)', code: "np.sign(df['close'].diff())" },
  { label: 'funding momentum', code: "df['funding_rate'].rolling(24).mean()" },
];

export default function FactorStudioPage() {
  const qc = useQueryClient();
  const listQ = useQuery({ queryKey: queryKeys.fsList, queryFn: api.fsList });

  const [formula, setFormula] = useState<string>(DEFAULT_FORMULA);
  const [name, setName] = useState<string>('');
  // P12/C-L1 — expand the period preset to a 7-tier ladder so operators can
  // sweep IC stability over short / medium / long horizons without dropping
  // to manual ISO strings. `'all'` keeps its previous meaning (omit the
  // period_start parameter entirely and let the backend use the full series).
  const [periodPreset, setPeriodPreset] = useState<
    '30d' | '90d' | '6m' | '1y' | '2y' | '5y' | 'all'
  >('1y');
  const [prevResult, setPrevResult] = useState<FsEvalResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Tracks the formula snapshot that produced the current evalMut.data so the
  // Save button can be gated when the formula has changed since the last eval.
  const lastEvaledFormulaRef = useRef<string | null>(null);

  const periodParams = useMemo(() => {
    if (periodPreset === 'all') return {};
    // P12/C-L1 — translate the 7 presets into rolling-window day counts.
    // 6m ≈ 182 days (half of 365), 1y/2y/5y use 365-day years so the math
    // stays whole-number friendly (calendar precision isn't load-bearing
    // here — the backend resolves to the nearest available bar).
    const days =
      periodPreset === '30d' ? 30
      : periodPreset === '90d' ? 90
      : periodPreset === '6m'  ? 182
      : periodPreset === '1y'  ? 365
      : periodPreset === '2y'  ? 730
      : periodPreset === '5y'  ? 1825
      : 365;
    const start = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
    return { period_start: start };
  }, [periodPreset]);

  const evalMut = useMutation<FsEvalResult, Error, void, { previous: FsEvalResult | null }>({
    mutationFn: async () => {
      setError(null);
      try {
        return await api.fsEvaluate({ formula_code: formula, asset_symbol: 'BTC', ...periodParams });
      } catch (e: any) {
        let msg = e.message ?? 'Eval failed';
        try {
          const parsed = JSON.parse(msg.split(': ').slice(1).join(': '));
          if (parsed?.detail) msg = typeof parsed.detail === 'string' ? parsed.detail : JSON.stringify(parsed.detail);
        } catch {}
        setError(msg);
        throw e;
      }
    },
    // P30-F2: capture the to-be-displaced result in onMutate (which runs
    // BEFORE React rerenders past 'pending' and BEFORE the cache dispatch
    // clears state.data); commit in onSuccess via context so it's immune
    // to React Query v5's lifecycle (onSuccess fires BEFORE the 'success'
    // dispatch, so reading evalMut.data there is timing-dependent).
    // Explicit return annotation on onMutate breaks the self-reference
    // type-inference cycle (`evalMut` referenced inside its own initializer).
    onMutate: (): { previous: FsEvalResult | null } => ({
      previous: evalMut.data ?? null,
    }),
    onSuccess: (_data, _vars, ctx) => {
      if (ctx?.previous) setPrevResult(ctx.previous);
      // Track which formula produced this eval result so the Save button
      // can be disabled when the formula has been edited since the last run.
      lastEvaledFormulaRef.current = formula;
    },
  });

  // D-H8c — persist a stable Idempotency-Key per save target (name+formula)
  // in sessionStorage so a second confirm / post-reload retry replays the
  // cached factor instead of minting a fresh UUID (mirrors paper-trade).
  const saveMut = useMutation({
    mutationFn: () => {
      const storageKey = `fs-save-${name}::${formula}-key`;
      let key = sessionStorage.getItem(storageKey);
      if (!key) {
        key = cryptoUuid();
        sessionStorage.setItem(storageKey, key);
      }
      return api.fsSave(
        { name, formula_code: formula, asset_symbol: 'BTC', eval_result: evalMut.data ?? undefined, overwrite: true },
        { idempotencyKey: key },
      );
    },
    onSuccess: () => {
      sessionStorage.removeItem(`fs-save-${name}::${formula}-key`);
      qc.invalidateQueries({ queryKey: queryKeys.fsList });
    },
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  });
  // P29-F3: fan-out invalidation so downstream pages reflect new strategy.
  // D-H8d — persist a stable Idempotency-Key per factor id so a second
  // confirm (after the first promote resolves and the armed window is still
  // open) replays the cached strategy_id instead of spawning a duplicate
  // background pipeline. Cleared on success so the next *intentional* promote
  // of the same factor gets a fresh key.
  const promoteMut = useMutation({
    mutationFn: (id: number) => {
      const storageKey = `fs-promote-${id}-key`;
      let key = sessionStorage.getItem(storageKey);
      if (!key) {
        key = cryptoUuid();
        sessionStorage.setItem(storageKey, key);
      }
      return api.fsPromote(id, undefined, { idempotencyKey: key });
    },
    onSuccess: (_data, id) => {
      sessionStorage.removeItem(`fs-promote-${id}-key`);
      qc.invalidateQueries({ queryKey: queryKeys.fsList });
      qc.invalidateQueries({ queryKey: queryKeys.strategies });
      qc.invalidateQueries({ queryKey: queryKeys.pipelineBuckets });
      qc.invalidateQueries({ queryKey: queryKeys.pipelineBucketsV2 });
      qc.invalidateQueries({ queryKey: queryKeys.graph });
      qc.invalidateQueries({ queryKey: queryKeys.agForest });
    },
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  });
  const deleteMut = useMutation({
    mutationFn: (id: number) => api.fsDelete(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.fsList }),
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  });

  // P29-F3: armed-confirm pattern. First click arms (3s); second confirms.
  const [deleteArmedId, setDeleteArmedId] = useState<number | null>(null);
  const [promoteArmedId, setPromoteArmedId] = useState<number | null>(null);
  const deleteArmTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const promoteArmTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    return () => {
      if (deleteArmTimerRef.current) {
        clearTimeout(deleteArmTimerRef.current);
        deleteArmTimerRef.current = null;
      }
      if (promoteArmTimerRef.current) {
        clearTimeout(promoteArmTimerRef.current);
        promoteArmTimerRef.current = null;
      }
    };
  }, []);

  function armDelete(id: number) {
    // P31-F4: block re-arm while a mutation is in flight — useMutation only
    // retains ONE variables/error slot, so a second concurrent invocation
    // would clobber the first row's error context (mirrors P30-F3 in
    // trading-terminal).
    if (deleteMut.isPending) return;
    if (deleteArmedId === id) {
      if (deleteArmTimerRef.current) {
        clearTimeout(deleteArmTimerRef.current);
        deleteArmTimerRef.current = null;
      }
      setDeleteArmedId(null);
      deleteMut.mutate(id);
      return;
    }
    if (deleteArmTimerRef.current) clearTimeout(deleteArmTimerRef.current);
    setDeleteArmedId(id);
    deleteArmTimerRef.current = setTimeout(() => {
      setDeleteArmedId(null);
      deleteArmTimerRef.current = null;
    }, 3000);
  }

  function armPromote(id: number) {
    // P31-F4: block re-arm while a mutation is in flight — useMutation only
    // retains ONE variables/error slot, so a second concurrent invocation
    // would clobber the first row's error context (mirrors P30-F3 in
    // trading-terminal).
    if (promoteMut.isPending) return;
    if (promoteArmedId === id) {
      if (promoteArmTimerRef.current) {
        clearTimeout(promoteArmTimerRef.current);
        promoteArmTimerRef.current = null;
      }
      setPromoteArmedId(null);
      promoteMut.mutate(id);
      return;
    }
    if (promoteArmTimerRef.current) clearTimeout(promoteArmTimerRef.current);
    setPromoteArmedId(id);
    promoteArmTimerRef.current = setTimeout(() => {
      setPromoteArmedId(null);
      promoteArmTimerRef.current = null;
    }, 3000);
  }

  return (
    <div className="grid h-full w-full grid-cols-12 gap-3 overflow-hidden p-3">
      <main className="col-span-8 flex flex-col gap-3 overflow-y-auto">
        <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
          <header className="mb-2 flex items-center justify-between">
            <h1 className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
              <Sigma className="h-3.5 w-3.5 text-cyan-400" /> Factor Editor
            </h1>
            <div className="flex flex-wrap gap-1">
              {SNIPPETS.map((s) => (
                <button
                  key={s.label}
                  onClick={() => setFormula((f) => f + (f.endsWith('\n') ? '' : '\n') + '    ' + s.code + '\n')}
                  className="rounded border border-slate-700 px-1.5 py-0.5 text-[9px] text-slate-300 hover:bg-slate-800"
                >
                  {s.label}
                </button>
              ))}
            </div>
          </header>
          <textarea
            value={formula}
            onChange={(e) => setFormula(e.target.value)}
            className="h-72 w-full resize-y rounded-md border border-slate-700 bg-slate-950 p-3 font-mono text-[11px] text-emerald-200"
            spellCheck={false}
          />
          {error && (
            <div className="mt-2 rounded-md border border-rose-700/40 bg-rose-500/10 px-2 py-1.5 text-[11px] text-rose-200">
              <AlertTriangle className="mr-1 inline h-3 w-3" /> {error}
            </div>
          )}
        </section>

        {evalMut.data && (
          <>
            <KpiGrid result={evalMut.data} prev={prevResult} />
            {/* P8-FIX/M-17 — full evaluation chart suite, matching the
                strategy-detail performance grid. */}
            <EquityDrawdown data={evalMut.data.equity_curve} />
            <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
              <DailyPnlBars data={evalMut.data.equity_curve} />
              <DrawdownDistribution data={evalMut.data.equity_curve} />
              <RollingSharpe data={evalMut.data.equity_curve} window={30} windowSelector />
              <MonthlyReturnsHeatmap data={evalMut.data.equity_curve} />
            </div>
          </>
        )}
      </main>

      <aside className="col-span-4 flex flex-col gap-3 overflow-y-auto">
        <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
          <header className="mb-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">Evaluate</header>
          <div className="space-y-2 text-[11px]">
            <div className="flex overflow-hidden rounded-md border border-slate-700">
              {/* P12/C-L1 — 7-button preset row (30d → all). Each button still
                  collapses to `flex-1` so the row stretches across the
                  sidebar without wrapping. */}
              {(['30d', '90d', '6m', '1y', '2y', '5y', 'all'] as const).map((p) => (
                <button key={p} onClick={() => setPeriodPreset(p)} className={`flex-1 px-2 py-1 ${periodPreset === p ? 'bg-cyan-500/20 text-cyan-200' : 'text-slate-400 hover:bg-slate-800'}`}>{p}</button>
              ))}
            </div>
            <button
              disabled={evalMut.isPending}
              onClick={() => evalMut.mutate()}
              className="flex w-full items-center justify-center gap-1 rounded-md border border-cyan-700 bg-cyan-500/20 px-3 py-1.5 text-[11px] font-bold text-cyan-200 hover:bg-cyan-500/30 disabled:opacity-40"
            >
              <Play className="h-3 w-3" /> {evalMut.isPending ? 'Running…' : 'Run Evaluation'}
            </button>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Factor name to save…"
              className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-[11px] text-slate-200"
            />
            {(() => {
              const formulaChanged = !!evalMut.data && formula !== lastEvaledFormulaRef.current;
              return (
                <>
                  <button
                    disabled={!name || !evalMut.data || saveMut.isPending || formulaChanged}
                    onClick={() => saveMut.mutate()}
                    className="flex w-full items-center justify-center gap-1 rounded-md border border-slate-700 px-3 py-1.5 text-[11px] text-slate-300 hover:bg-slate-800 disabled:opacity-40"
                  >
                    <Save className="h-3 w-3" /> Save Factor
                  </button>
                  {formulaChanged && (
                    <p className="text-[10px] text-amber-400">
                      Formula changed — re-run evaluation before saving.
                    </p>
                  )}
                </>
              );
            })()}
          </div>
        </section>

        <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
          <header className="mb-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">Saved Factors</header>
          <ul className="space-y-1 text-[11px]">
            {(listQ.data?.factors ?? []).map((f) => (
              <li key={f.id} className="rounded-md border border-slate-900 bg-slate-950/40 px-2 py-1.5">
                <div className="flex items-center justify-between">
                  <button onClick={() => { setFormula(f.formula_code); setName(f.name); }} className="flex-1 truncate text-left text-slate-200 hover:text-cyan-300">
                    {f.name}
                  </button>
                  <div className="flex items-center gap-1">
                    <span className="font-mono text-[10px] text-cyan-300">IC {Number.isFinite(f.ic_score_cached) ? f.ic_score_cached.toFixed(2) : '—'}</span>
                    <button
                      onClick={() => armPromote(f.id)}
                      title={promoteArmedId === f.id ? 'Click again within 3s to confirm promote' : 'Promote to Strategy'}
                      className={`rounded p-0.5 ${promoteArmedId === f.id ? 'bg-rose-500/20 text-rose-200' : 'text-emerald-300 hover:bg-emerald-500/20'}`}
                    >
                      <ArrowUpCircle className="h-3 w-3" />
                    </button>
                    <button
                      onClick={() => armDelete(f.id)}
                      title={deleteArmedId === f.id ? 'Click again within 3s to confirm delete' : 'Delete'}
                      className={`rounded p-0.5 ${deleteArmedId === f.id ? 'bg-rose-500/30 text-rose-100' : 'text-rose-400 hover:bg-rose-500/20'}`}
                    >×</button>
                  </div>
                </div>
                {f.promoted_strategy_id && (
                  <div className="mt-0.5 text-[9px] text-emerald-400">→ Strategy S#{f.promoted_strategy_id}</div>
                )}
              </li>
            ))}
            {!listQ.data?.factors.length && <li className="text-[10px] text-slate-500">No saved factors yet.</li>}
          </ul>
        </section>
      </aside>
    </div>
  );
}

function KpiGrid({ result, prev }: { result: FsEvalResult; prev: FsEvalResult | null }) {
  const tiles = [
    { label: 'IC', value: result.ic != null && Number.isFinite(result.ic) ? result.ic.toFixed(3) : '—', prev: prev?.ic, cls: 'text-cyan-300', fmt: (n: number) => n.toFixed(3) },
    { label: 'Sharpe', value: Number.isFinite(result.sharpe) ? result.sharpe.toFixed(2) : '—', prev: prev?.sharpe, cls: 'text-cyan-300', fmt: (n: number) => n.toFixed(2) },
    { label: 'Max DD', value: Number.isFinite(result.max_drawdown) ? `${(result.max_drawdown * 100).toFixed(1)}%` : '—', prev: prev?.max_drawdown, cls: 'text-rose-300', fmt: (n: number) => `${(n * 100).toFixed(1)}%` },
    { label: 'Win Rate', value: Number.isFinite(result.win_rate) ? `${(result.win_rate * 100).toFixed(1)}%` : '—', prev: prev?.win_rate, cls: 'text-emerald-300', fmt: (n: number) => `${(n * 100).toFixed(1)}%` },
  ];
  return (
    <div className="grid grid-cols-4 gap-3">
      {tiles.map((t) => (
        <div key={t.label} className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
          <div className="text-[9px] uppercase tracking-widest text-slate-500">{t.label}</div>
          <div className={`mt-1 font-mono text-2xl ${t.cls}`}>{t.value}</div>
          {t.prev != null && Number.isFinite(t.prev) && <div className="mt-0.5 text-[9px] text-slate-500">prev {t.fmt(t.prev)}</div>}
        </div>
      ))}
    </div>
  );
}
