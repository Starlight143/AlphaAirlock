'use client';

/**
 * /sim-account — Forward SIM console (P-SIM).
 *
 * Unlike /paper-trade (which re-backtests a trailing window of the SAME history
 * every run, so it never tests unseen data), a SIM account PINS a start bar when
 * you send a strategy here and scores only bars AFTER it — a genuine walk-forward
 * that accumulates as real ingest advances. Full-ledger matching with fees,
 * slippage and 8h perp funding. SIMULATION ONLY: it never routes a real order.
 *
 *  ┌──────────────────────────┬────────────────────────────────────┐
 *  │ Eligible strategies      │ SIM accounts + selected detail      │
 *  │ → "Send to sim"          │ (equity, forward stats, fills, P&L) │
 *  └──────────────────────────┴────────────────────────────────────┘
 */

import { useMemo, useState } from 'react';
import Link from 'next/link';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import clsx from 'clsx';
import {
  Wallet,
  Send,
  RefreshCw,
  StopCircle,
  ShieldCheck,
  ShieldAlert,
  Hourglass,
  Loader2,
} from 'lucide-react';

import { api, ApiError, cryptoUuid, type AlphaStrategy, type SimAccount } from '@/lib/api';
import { queryKeys } from '@/lib/query';

// Strategies worth forward-testing. Anything past the critic gate is eligible;
// the backend still rejects a pin if the strategy has no factor code yet.
const ELIGIBLE_STATUSES = new Set(['APPROVED', 'PAPER_TRADE', 'SMALL_CAPITAL', 'LIVE']);

function pct(x: number | undefined | null, d = 2): string {
  if (x == null || Number.isNaN(x)) return '—';
  return `${(x * 100).toFixed(d)}%`;
}
function money(x: number | undefined | null): string {
  if (x == null || Number.isNaN(x)) return '—';
  return `$${x.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}
function num(x: number | undefined | null, d = 2): string {
  if (x == null || Number.isNaN(x)) return '—';
  return x.toFixed(d);
}

type HealthKind = 'healthy' | 'warming' | 'unhealthy';
function healthKind(a: { is_healthy: boolean | null }): HealthKind {
  if (a.is_healthy === null || a.is_healthy === undefined) return 'warming';
  return a.is_healthy ? 'healthy' : 'unhealthy';
}

function HealthBadge({ kind }: { kind: HealthKind }) {
  const map = {
    healthy: { cls: 'text-emerald-400 border-emerald-400/30 bg-emerald-400/10', Icon: ShieldCheck, label: 'healthy' },
    warming: { cls: 'text-amber-400 border-amber-400/30 bg-amber-400/10', Icon: Hourglass, label: 'warming up' },
    unhealthy: { cls: 'text-rose-400 border-rose-400/30 bg-rose-400/10', Icon: ShieldAlert, label: 'unhealthy' },
  }[kind];
  const { Icon } = map;
  return (
    <span className={clsx('inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-medium', map.cls)}>
      <Icon className="h-3 w-3" />
      {map.label}
    </span>
  );
}

/** Minimal inline equity sparkline — no chart-lib coupling. Baseline = initial
 *  capital; green above, red below. ponytail: swap for the shared EquityDrawdown
 *  chart if richer interaction is needed. */
function EquitySpark({ points, initial }: { points: { equity: number }[]; initial: number }) {
  const w = 640;
  const h = 120;
  if (!points || points.length < 2) {
    return (
      <div className="flex h-[120px] items-center justify-center rounded border border-white/10 text-xs text-white/40">
        No forward bars yet — the curve fills in as new market bars are ingested.
      </div>
    );
  }
  const eq = points.map((p) => p.equity);
  const min = Math.min(...eq, initial);
  const max = Math.max(...eq, initial);
  const span = max - min || 1;
  const x = (i: number) => (i / (points.length - 1)) * w;
  const y = (v: number) => h - ((v - min) / span) * h;
  const path = eq.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const last = eq[eq.length - 1];
  const up = last >= initial;
  const baseY = y(initial);
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full" preserveAspectRatio="none" role="img" aria-label="forward equity curve">
      <line x1={0} y1={baseY} x2={w} y2={baseY} stroke="currentColor" className="text-white/15" strokeDasharray="3 3" />
      <path d={path} fill="none" stroke="currentColor" className={up ? 'text-emerald-400' : 'text-rose-400'} strokeWidth={1.5} />
    </svg>
  );
}

export default function SimAccountPage() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const strategiesQ = useQuery({ queryKey: queryKeys.strategies, queryFn: api.strategies, refetchInterval: 15_000 });
  const accountsQ = useQuery({ queryKey: queryKeys.simAccountList, queryFn: api.simAccountList, refetchInterval: 10_000 });

  const accounts: SimAccount[] = accountsQ.data?.accounts ?? [];
  const pinnedIds = useMemo(() => new Set(accounts.map((a) => a.strategy_id)), [accounts]);

  const detailQ = useQuery({
    queryKey: selected != null ? queryKeys.simAccountDetail(selected) : ['sim-account-detail', 'none'],
    queryFn: () => api.simAccountDetail(selected as number),
    enabled: selected != null,
    refetchInterval: 10_000,
  });
  const detail = detailQ.data;

  const eligible: AlphaStrategy[] = useMemo(
    () => (strategiesQ.data?.strategies ?? []).filter((s) => ELIGIBLE_STATUSES.has(s.status)),
    [strategiesQ.data],
  );

  function refresh() {
    qc.invalidateQueries({ queryKey: queryKeys.simAccountList });
    if (selected != null) qc.invalidateQueries({ queryKey: queryKeys.simAccountDetail(selected) });
  }

  const startMut = useMutation({
    mutationFn: (sid: number) => {
      const storeKey = `sim-start-${sid}`;
      const key = sessionStorage.getItem(storeKey) ?? cryptoUuid();
      sessionStorage.setItem(storeKey, key);
      return api.simAccountStart(sid, { idempotencyKey: key }).finally(() => sessionStorage.removeItem(storeKey));
    },
    onSuccess: (acct) => {
      setErr(null);
      setSelected(acct.strategy_id);
      refresh();
    },
    onError: (e) => setErr(e instanceof ApiError ? e.detail || e.message : String(e)),
  });

  const tickMut = useMutation({
    mutationFn: (sid: number) => api.simAccountTick(sid),
    onSuccess: () => { setErr(null); refresh(); },
    onError: (e) => setErr(e instanceof ApiError ? e.detail || e.message : String(e)),
  });

  const stopMut = useMutation({
    mutationFn: (sid: number) => api.simAccountStop(sid),
    onSuccess: () => { setErr(null); refresh(); },
    onError: (e) => setErr(e instanceof ApiError ? e.detail || e.message : String(e)),
  });

  function sendToSim(sid: number) {
    if (pinnedIds.has(sid)) {
      if (!window.confirm('This strategy already has a SIM account. Re-pinning RESETS its ledger and restarts the walk-forward from now. Continue?')) return;
    }
    startMut.mutate(sid);
  }

  const kpis = useMemo(() => {
    const active = accounts.filter((a) => a.status === 'active').length;
    const healthy = accounts.filter((a) => a.is_healthy === true).length;
    const warming = accounts.filter((a) => a.is_healthy === null || a.is_healthy === undefined).length;
    return { total: accounts.length, active, healthy, warming };
  }, [accounts]);

  return (
    <div className="flex h-full w-full flex-col gap-4 overflow-hidden p-4">
      {/* Header */}
      <div className="flex shrink-0 items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-2 text-lg font-semibold text-white">
            <Wallet className="h-5 w-5 text-sky-400" /> Sim Account
          </h1>
          <p className="mt-1 max-w-3xl text-xs text-white/50">
            Pin a strategy to a forward paper account: it scores only bars <em>after</em> the pin — a genuine
            walk-forward as new market data is ingested. Full-ledger matching (fees, slippage, 8h funding).{' '}
            <span className="text-white/70">Simulation only — no real orders, no real money.</span>
          </p>
        </div>
        <button
          onClick={refresh}
          className="inline-flex items-center gap-1.5 rounded border border-white/15 px-2.5 py-1.5 text-xs text-white/70 hover:bg-white/5"
        >
          <RefreshCw className="h-3.5 w-3.5" /> Refresh
        </button>
      </div>

      {/* KPIs */}
      <div className="grid shrink-0 grid-cols-4 gap-3">
        {[
          { label: 'ACCOUNTS', value: kpis.total },
          { label: 'ACTIVE', value: kpis.active },
          { label: 'HEALTHY', value: kpis.healthy },
          { label: 'WARMING UP', value: kpis.warming },
        ].map((k) => (
          <div key={k.label} className="rounded-lg border border-white/10 bg-white/5 p-3">
            <div className="text-2xl font-semibold text-white">{k.value}</div>
            <div className="text-[11px] tracking-wide text-white/40">{k.label}</div>
          </div>
        ))}
      </div>

      {err && (
        <div className="shrink-0 rounded border border-rose-400/30 bg-rose-400/10 px-3 py-2 text-xs text-rose-300">{err}</div>
      )}

      <div className="flex min-h-0 flex-1 gap-4">
        {/* Eligible strategies */}
        <div className="flex w-80 shrink-0 flex-col overflow-hidden rounded-lg border border-white/10 bg-white/5">
          <div className="shrink-0 border-b border-white/10 px-3 py-2 text-xs font-medium text-white/60">
            ELIGIBLE STRATEGIES {eligible.length ? `(${eligible.length})` : ''}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {strategiesQ.isLoading && <div className="p-3 text-xs text-white/40">Loading…</div>}
            {!strategiesQ.isLoading && eligible.length === 0 && (
              <div className="p-3 text-xs text-white/40">No APPROVED strategies yet.</div>
            )}
            {eligible.map((s) => {
              const pinned = pinnedIds.has(s.id);
              const busy = startMut.isPending && startMut.variables === s.id;
              return (
                <div key={s.id} className="flex items-center justify-between gap-2 border-b border-white/5 px-3 py-2">
                  <div className="min-w-0">
                    <div className="truncate text-sm text-white/90">
                      <Link href={`/strategies/${s.id}`} className="hover:underline">S#{s.id}</Link>{' '}
                      <span className="text-white/40">{s.alpha_id ?? ''}</span>
                    </div>
                    <div className="truncate text-[11px] text-white/40">{s.name} · {s.status}</div>
                  </div>
                  <button
                    onClick={() => sendToSim(s.id)}
                    disabled={busy}
                    className={clsx(
                      'inline-flex shrink-0 items-center gap-1 rounded px-2 py-1 text-[11px] font-medium',
                      pinned
                        ? 'border border-amber-400/30 text-amber-300 hover:bg-amber-400/10'
                        : 'border border-sky-400/30 text-sky-300 hover:bg-sky-400/10',
                    )}
                  >
                    {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Send className="h-3 w-3" />}
                    {pinned ? 'Re-pin' : 'Send to sim'}
                  </button>
                </div>
              );
            })}
          </div>
        </div>

        {/* Accounts + detail */}
        <div className="min-w-0 flex-1 space-y-4 overflow-y-auto pr-1">
          {/* Account list */}
          <div className="rounded-lg border border-white/10 bg-white/5">
            <div className="border-b border-white/10 px-3 py-2 text-xs font-medium text-white/60">SIM ACCOUNTS</div>
            {accountsQ.isLoading && <div className="p-3 text-xs text-white/40">Loading…</div>}
            {!accountsQ.isLoading && accounts.length === 0 && (
              <div className="p-3 text-xs text-white/40">No SIM accounts yet — send an eligible strategy here to start one.</div>
            )}
            <div className="divide-y divide-white/5">
              {accounts.map((a) => {
                const m = a.metrics ?? {};
                const fbars = Number(m.forward_bars ?? 0);
                return (
                  <button
                    key={a.strategy_id}
                    onClick={() => setSelected(a.strategy_id)}
                    className={clsx(
                      'flex w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-white/5',
                      selected === a.strategy_id && 'bg-white/10',
                    )}
                  >
                    <div className="min-w-0">
                      <div className="truncate text-sm text-white/90">S#{a.strategy_id} <span className="text-white/40">{a.symbol}</span></div>
                      <div className="truncate text-[11px] text-white/40">{a.name ?? ''}</div>
                    </div>
                    <div className="flex items-center gap-4 text-right text-[11px]">
                      <div>
                        <div className="text-white/40">days</div>
                        <div className="text-white/80">{(fbars / 24).toFixed(1)}</div>
                      </div>
                      <div>
                        <div className="text-white/40">fwd ret</div>
                        <div className={clsx(Number(m.cumulative_return) >= 0 ? 'text-emerald-400' : 'text-rose-400')}>
                          {pct(Number(m.cumulative_return))}
                        </div>
                      </div>
                      <div>
                        <div className="text-white/40">Sharpe</div>
                        <div className="text-white/80">{num(Number(m.annualized_sharpe))}</div>
                      </div>
                      <HealthBadge kind={healthKind(a)} />
                    </div>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Detail */}
          {detail && (
            <div className="rounded-lg border border-white/10 bg-white/5 p-4 space-y-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2 text-sm text-white">
                    <span className="font-semibold">S#{detail.strategy_id}</span>
                    <span className="text-white/40">{detail.symbol}</span>
                    <HealthBadge kind={healthKind(detail)} />
                    <span className="text-[11px] text-white/40">status: {detail.status}</span>
                  </div>
                  <div className="mt-0.5 text-[11px] text-white/40">
                    pinned {detail.start_bar_ts?.slice(0, 16).replace('T', ' ')} · last bar {String(detail.last_bar_ts ?? '—').slice(0, 16).replace('T', ' ')}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => tickMut.mutate(detail.strategy_id)}
                    disabled={tickMut.isPending}
                    className="inline-flex items-center gap-1 rounded border border-white/15 px-2 py-1 text-[11px] text-white/70 hover:bg-white/5"
                  >
                    {tickMut.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />} Tick now
                  </button>
                  {detail.status === 'active' && (
                    <button
                      onClick={() => stopMut.mutate(detail.strategy_id)}
                      disabled={stopMut.isPending}
                      className="inline-flex items-center gap-1 rounded border border-rose-400/30 px-2 py-1 text-[11px] text-rose-300 hover:bg-rose-400/10"
                    >
                      <StopCircle className="h-3 w-3" /> Stop
                    </button>
                  )}
                </div>
              </div>

              {/* Equity curve */}
              <div className="text-sky-300/80">
                <EquitySpark points={detail.equity_curve ?? []} initial={detail.initial_capital} />
              </div>

              {/* Forward metrics grid */}
              <div className="grid grid-cols-4 gap-3 text-sm">
                {[
                  ['Forward return', pct(Number(detail.metrics?.cumulative_return)), Number(detail.metrics?.cumulative_return) >= 0],
                  ['Ann. Sharpe', num(Number(detail.metrics?.annualized_sharpe)), Number(detail.metrics?.annualized_sharpe) >= 0],
                  ['Max drawdown', pct(Number(detail.metrics?.max_drawdown)), false],
                  ['Profit factor', num(Number(detail.metrics?.profit_factor)), Number(detail.metrics?.profit_factor) >= 1],
                  ['Forward trades', String(Number(detail.metrics?.num_trades ?? 0)), true],
                  ['Forward bars', `${Number(detail.metrics?.forward_bars ?? 0)} (${(Number(detail.metrics?.forward_bars ?? 0) / 24).toFixed(1)}d)`, true],
                  ['Funding net', money(detail.funding?.net_quote), (detail.funding?.net_quote ?? 0) >= 0],
                  ['Equity', money(detail.position?.equity), (detail.position?.equity ?? 0) >= detail.initial_capital],
                ].map(([label, value, good]) => (
                  <div key={label as string} className="rounded border border-white/10 bg-black/20 p-2">
                    <div className="text-[11px] text-white/40">{label}</div>
                    <div className={clsx('text-base', good ? 'text-emerald-300' : 'text-rose-300')}>{value}</div>
                  </div>
                ))}
              </div>

              {/* Position + funding line */}
              <div className="text-[11px] text-white/50">
                Position: qty {num(detail.position?.qty_base, 4)} · notional {money(detail.position?.position_notional)} ·{' '}
                funding settlements {detail.funding?.settlements ?? 0} · initial {money(detail.initial_capital)} ·{' '}
                fees {pct(detail.fee_bps, 3)}/side, slippage {pct(detail.slippage_bps, 3)}/side
              </div>

              {/* Health notes */}
              {detail.health_notes?.length > 0 && (
                <ul className="space-y-0.5 text-[11px] text-white/55">
                  {detail.health_notes.map((n, i) => (
                    <li key={i}>· {n}</li>
                  ))}
                </ul>
              )}

              {/* Fills tape */}
              <div>
                <div className="mb-1 text-[11px] font-medium text-white/50">RECENT FILLS ({detail.fills?.length ?? 0})</div>
                <div className="max-h-64 min-h-[8rem] overflow-y-auto rounded border border-white/10">
                  <table className="w-full text-[11px]">
                    <thead className="sticky top-0 bg-black/40 text-white/40">
                      <tr>
                        <th className="px-2 py-1 text-left">time</th>
                        <th className="px-2 py-1 text-left">side</th>
                        <th className="px-2 py-1 text-left">reason</th>
                        <th className="px-2 py-1 text-right">Δqty</th>
                        <th className="px-2 py-1 text-right">price</th>
                        <th className="px-2 py-1 text-right">fee</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(detail.fills ?? []).slice().reverse().map((f, i) => (
                        <tr key={i} className="border-t border-white/5">
                          <td className="px-2 py-1 text-white/60">{f.bar_ts.slice(0, 16).replace('T', ' ')}</td>
                          <td className={clsx('px-2 py-1', f.side === 'buy' ? 'text-emerald-400' : 'text-rose-400')}>{f.side}</td>
                          <td className="px-2 py-1 text-white/50">{f.reason}</td>
                          <td className="px-2 py-1 text-right text-white/70">{f.signed_qty_delta.toFixed(4)}</td>
                          <td className="px-2 py-1 text-right text-white/70">{f.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}</td>
                          <td className="px-2 py-1 text-right text-white/40">{f.fee_quote.toFixed(2)}</td>
                        </tr>
                      ))}
                      {(detail.fills ?? []).length === 0 && (
                        <tr><td colSpan={6} className="px-2 py-12 text-center text-white/30">No fills yet — fills appear here once the strategy trades</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
