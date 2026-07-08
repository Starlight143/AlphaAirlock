'use client';

import { useState } from 'react';
import dynamic from 'next/dynamic';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Loader2, RefreshCcw, Sigma } from 'lucide-react';

import { api, type CointegrationResponse } from '@/lib/api';
import { queryKeys } from '@/lib/query';

// vis-network is browser-only; dynamic import with ssr:false matches the
// /factor-network pattern and prevents the "window is not defined" SSR error.
const CointegrationField = dynamic(
  () => import('@/components/cointegration/CointegrationField'),
  { ssr: false },
);

const LOOKBACK_CHOICES = [30, 90, 180, 360] as const;
const PVALUE_CHOICES = [0.01, 0.025, 0.05, 0.1] as const;

// P14/B-L4 — explicit method label map. The previous
// `data.method.replace('_', '–')` produced "engle–granger" (en-dash typo).
// Add a new entry when the backend introduces additional cointegration
// methods (e.g. johansen) so the operator sees a polished label.
const METHOD_LABELS: Record<string, string> = {
  engle_granger: 'Engle-Granger',
};

/**
 * /cointegration — Engle-Granger pair scan constellation (P8-FIX/F6).
 *
 * Lives inside the (shell) route group so the sidebar stays visible — that
 * way the operator can switch back to Mission Control / Factor Network
 * without losing wayfinding (P6-A1 used the (fullscreen) group, which hid
 * the sidebar entirely and confused testers).
 *
 * Data comes from `GET /api/cointegration/pairs?lookback_days=&p_threshold=`.
 * When the backend can't produce a real series (insufficient history etc.)
 * it returns `is_synthetic: true`; we mirror that with a "Synthetic" chip
 * so the operator knows the constellation is illustrative.
 */
export default function CointegrationPage() {
  const qc = useQueryClient();
  const [lookback, setLookback] = useState<number>(180);
  const [pThreshold, setPThreshold] = useState<number>(0.05);

  const cointQ = useQuery({
    queryKey: queryKeys.cointegrationPairs(lookback, pThreshold),
    queryFn: () =>
      api.cointegrationPairs({ lookback_days: lookback, p_threshold: pThreshold }),
    // Compute on demand — the underlying scan is expensive and the operator
    // changes lookback/p-threshold rarely.
    refetchInterval: false,
    staleTime: 60_000,
  });

  // Manual refresh re-runs the scan server-side (refresh=true bypasses the
  // backend cache), then resets the cached query so React Query re-fetches.
  const refreshMut = useMutation({
    // Thread the lookback/p-threshold through React Query's mutation variables
    // so onSuccess writes the fresh scan back under the SAME key the request
    // was issued with. mutationFn/onSuccess are inline closures recreated every
    // render; in react-query v5 the observer always invokes the LATEST onSuccess
    // closure when the mutation settles. Reading lookback/pThreshold from that
    // closure means a mid-flight selector change would write the old params'
    // result under the new key, silently corrupting the now-displayed cache
    // entry. Passing the params as `vars` makes the write key-correct regardless
    // of render timing or mid-flight dropdown changes.
    mutationFn: (vars: { lookback: number; p: number }) =>
      api.cointegrationPairs({
        lookback_days: vars.lookback,
        p_threshold: vars.p,
        refresh: true,
      }),
    onSuccess: (fresh: CointegrationResponse, vars) => {
      qc.setQueryData(queryKeys.cointegrationPairs(vars.lookback, vars.p), fresh);
    },
  });

  const data = cointQ.data;
  const isSynthetic = Boolean(data?.is_synthetic);
  const hasError = Boolean(data?.error || cointQ.error || refreshMut.error);
  // P12/C-L4 — when the scan returns a successful response with an empty
  // asset list (no overlap with the lookback / threshold), highlight the
  // Refresh button so the operator notices there's nothing to render and
  // is nudged to retry with a wider window.
  const isEmpty = !!data && Array.isArray(data.assets) && data.assets.length === 0;

  return (
    <div className="relative flex h-full w-full flex-col overflow-hidden">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 bg-slate-950/70 px-3 py-2">
        <div className="flex items-center gap-2">
          <Sigma className="h-3.5 w-3.5 text-cyan-300" />
          <span className="font-mono text-[11px] font-bold uppercase tracking-widest text-slate-100">
            Cointegration Analysis
          </span>
          {data?.method && (
            <span className="rounded border border-slate-700 bg-slate-900 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider text-slate-400">
              {METHOD_LABELS[data.method] ?? data.method}
            </span>
          )}
          {isSynthetic && (
            <span className="rounded border border-amber-700/40 bg-amber-500/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-amber-200">
              Synthetic
            </span>
          )}
          {data && (
            <span
              className="text-[10px] text-slate-500"
              title={`${data.pair_count} pairs passed the p<${pThreshold} threshold out of ${data.tested_pair_count} candidates tested`}
            >
              · {data.pair_count}/{data.tested_pair_count} pairs · computed{' '}
              {data.computed_at ? new Date(data.computed_at).toLocaleTimeString() : '—'}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1 text-[10px] text-slate-500">
            Lookback
            <select
              value={lookback}
              onChange={(e) => setLookback(Number(e.target.value))}
              className="rounded border border-slate-700 bg-slate-950 px-1.5 py-1 font-mono text-[10px] text-slate-200"
            >
              {LOOKBACK_CHOICES.map((d) => (
                <option key={d} value={d}>
                  {d}d
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1 text-[10px] text-slate-500">
            p &lt;
            <select
              value={pThreshold}
              onChange={(e) => setPThreshold(Number(e.target.value))}
              className="rounded border border-slate-700 bg-slate-950 px-1.5 py-1 font-mono text-[10px] text-slate-200"
            >
              {PVALUE_CHOICES.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
          <button
            onClick={() => refreshMut.mutate({ lookback, p: pThreshold })}
            disabled={refreshMut.isPending || cointQ.isFetching}
            className={`inline-flex items-center gap-1 rounded-md border ${
              isEmpty
                ? 'animate-pulse border-cyan-500 ring-1 ring-cyan-500/40'
                : 'border-slate-700'
            } px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-slate-300 hover:bg-slate-800 disabled:opacity-40`}
          >
            {refreshMut.isPending || cointQ.isFetching ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <RefreshCcw className="h-3 w-3" />
            )}
            {isEmpty ? 'Retry Scan' : 'Refresh'}
          </button>
        </div>
      </header>

      {hasError && (
        <div className="line-clamp-2 border-b border-rose-800/60 bg-rose-500/10 px-3 py-1.5 text-[11px] text-rose-300" title={String(data?.error ?? (cointQ.error as Error)?.message ?? (refreshMut.error as Error)?.message ?? '')}>
          {data?.error ?? (cointQ.error as Error)?.message ?? (refreshMut.error as Error)?.message ?? 'Failed to load cointegration scan.'}
        </div>
      )}

      <div className="relative flex-1 overflow-hidden">
        {cointQ.isLoading && !data ? (
          <div className="flex h-full items-center justify-center text-[11px] text-slate-500">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Computing pair scan…
          </div>
        ) : (
          <CointegrationField data={data} />
        )}
        {/* P16/B-L1 — dropped the duplicate floating "Synthetic data —
            illustrative only" banner; the same indicator already shows as a
            chip in the header next to the method label. */}

        {/* P12/C-M3 — legend now honours the active p-threshold. The rose /
            amber buckets are only meaningful when the threshold itself is
            permissive enough to include them; when the operator picks a
            tighter threshold (e.g. 0.01) the wider-bucket swatches would
            otherwise imply edges that the scan never returns. The cyan
            swatch is always shown because it tracks the active threshold. */}
        <footer className="pointer-events-none absolute bottom-3 left-3 z-10 inline-flex items-center gap-3 rounded-md border border-slate-800/80 bg-slate-950/70 px-3 py-1.5 text-[10px] font-mono uppercase tracking-wider text-slate-400 backdrop-blur">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-2 w-2 rounded-full bg-rose-500" /> p &lt; 0.01
          </span>
          {pThreshold > 0.01 && (
            <span className="flex items-center gap-1.5">
              <span className="inline-block h-2 w-2 rounded-full bg-amber-500" /> 0.01 ≤ p &lt; 0.025
            </span>
          )}
          {pThreshold > 0.025 && (
            <span className="flex items-center gap-1.5">
              <span className="inline-block h-2 w-2 rounded-full bg-cyan-400" />
              0.025 ≤ p &lt; 0.05
            </span>
          )}
          {pThreshold > 0.05 && (
            <span className="flex items-center gap-1.5">
              <span className="inline-block h-2 w-2 rounded-full bg-cyan-400/70" />
              0.05 ≤ p &lt; {pThreshold}
            </span>
          )}
        </footer>
      </div>
    </div>
  );
}
