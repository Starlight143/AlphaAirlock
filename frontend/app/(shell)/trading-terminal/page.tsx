'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Joystick, Send, AlertTriangle } from 'lucide-react';

import { api, cryptoUuid, type TtMarketInfo, type TtOrderPreview, type TtOrderReq, type TtPosition, type TtStatus, type TtSymbol } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import CandlestickChart, { type PriceTick } from '@/components/charts/CandlestickChart';

function useSessionId(): string {
  const [sid, setSid] = useState<string>('');
  useEffect(() => {
    let s = sessionStorage.getItem('tt-session');
    if (!s) { s = cryptoUuid(); sessionStorage.setItem('tt-session', s); }
    setSid(s);
  }, []);
  return sid;
}

// P15/C-M16 — standard "Escape closes the modal" accessibility hook. Wired
// to PreviewModal here and to PauseConfirmModal / DeployConfirmModal on the
// live-trade page.
function useEscapeKey(active: boolean, onEscape: () => void) {
  const cbRef = useRef<() => void>(onEscape);
  useEffect(() => { cbRef.current = onEscape; });
  useEffect(() => {
    if (!active) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') cbRef.current();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [active]);
}

export default function TradingTerminalPage() {
  const qc = useQueryClient();
  const sessionId = useSessionId();
  const statusQ = useQuery({ queryKey: queryKeys.ttStatus, queryFn: api.ttStatus });
  const symsQ = useQuery({ queryKey: queryKeys.ttSymbols, queryFn: api.ttSymbols, enabled: !!statusQ.data?.enabled });

  const [symbol, setSymbol] = useState<string>('BTC-USDT');
  const [side, setSide] = useState<'buy' | 'sell'>('buy');
  const [orderType, setOrderType] = useState<'market' | 'limit' | 'stop'>('market');
  const [qty, setQty] = useState<string>('0.01');
  const [limitPrice, setLimitPrice] = useState<string>('');
  // P15/C-L19 — separate stop-trigger price input. Backend validate() requires
  // stop_price when order_type === 'stop'; the form would silently 400 before.
  const [stopPrice, setStopPrice] = useState<string>('');
  const [tif, setTif] = useState<'gtc' | 'ioc' | 'fok'>('ioc');
  const [mode, setMode] = useState<'paper' | 'live'>('paper');
  const [showPreview, setShowPreview] = useState<TtOrderPreview | null>(null);
  // tt-preview-submit-decoupled — freeze the EXACT request that was priced so
  // Submit routes what the operator confirmed, not a re-derived live snapshot.
  const [previewedReq, setPreviewedReq] = useState<TtOrderReq | null>(null);
  const [mutError, setMutError] = useState<string | null>(null);
  const [showLiveConfirm, setShowLiveConfirm] = useState<boolean>(false);  // R5/UX-04
  const [cancelAllArmed, setCancelAllArmed] = useState<boolean>(false);
  const cancelAllArmTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Auto-disarm after 3 s (matches per-order cancelArmTimerRef) and disarm
  // immediately if the active mode changes, preventing a confirmation against
  // the wrong mode's order list.
  useEffect(() => {
    setCancelAllArmed(false);
    if (cancelAllArmTimerRef.current) {
      clearTimeout(cancelAllArmTimerRef.current);
      cancelAllArmTimerRef.current = null;
    }
  }, [mode]);
  useEffect(() => {
    return () => {
      if (cancelAllArmTimerRef.current) {
        clearTimeout(cancelAllArmTimerRef.current);
        cancelAllArmTimerRef.current = null;
      }
    };
  }, []);
  function armCancelAll() {
    if (cancelAllArmed) {
      if (cancelAllArmTimerRef.current) {
        clearTimeout(cancelAllArmTimerRef.current);
        cancelAllArmTimerRef.current = null;
      }
      setCancelAllArmed(false);
      cancelAllMut.mutate(mode);
      return;
    }
    if (cancelAllArmTimerRef.current) clearTimeout(cancelAllArmTimerRef.current);
    setCancelAllArmed(true);
    cancelAllArmTimerRef.current = setTimeout(() => {
      setCancelAllArmed(false);
      cancelAllArmTimerRef.current = null;
    }, 3000);
  }
  // P-flatten — one-shot flag so a prefilled "Close position" order auto-opens
  // the Preview after the ticket state commits (avoids a stale buildReq read).
  const [pendingPreview, setPendingPreview] = useState(false);

  // P15/C-M17 — staggered cadences are intentional: market data is the
  // operator's price ticker and warrants 3s freshness; positions/orders only
  // change after submit confirmation and tolerate 5s polling without UX harm.
  const marketQ = useQuery({
    queryKey: queryKeys.ttMarket(symbol),
    queryFn: () => api.ttMarket(symbol),
    enabled: !!statusQ.data?.enabled,
    refetchInterval: 3_000,
  });
  const posQ = useQuery({
    queryKey: queryKeys.ttPositions(mode),
    queryFn: () => api.ttPositions(mode),
    enabled: !!statusQ.data?.enabled,
    refetchInterval: 5_000,
  });
  const ordersQ = useQuery({
    queryKey: queryKeys.ttOrders('', symbol, mode),
    queryFn: () => api.ttOrders(50, undefined, symbol, mode),
    enabled: !!statusQ.data?.enabled,
    refetchInterval: 5_000,
  });

  // Accumulate live price ticks from the 3s polling query into a rolling
  // window (max 200 points ≈ 10 minutes). Each new TtMarketInfo snapshot is
  // appended; the oldest ticks are dropped once the cap is reached. The array
  // is keyed by symbol so switching symbols starts a fresh series.
  const [priceTicks, setPriceTicks] = useState<PriceTick[]>([]);
  const prevSymbolRef = useRef<string>('');
  useEffect(() => {
    if (!marketQ.data) return;
    const snap = marketQ.data;
    if (!Number.isFinite(snap.last) || snap.last <= 0) return;
    setPriceTicks((prev) => {
      // Reset the accumulator when the operator switches symbols.
      const base = prevSymbolRef.current === symbol ? prev : [];
      prevSymbolRef.current = symbol;
      const next: PriceTick[] = [
        ...base,
        { ts: snap.ts ?? new Date().toISOString(), last: snap.last, bid: snap.bid, ask: snap.ask },
      ];
      // Keep at most 200 ticks (~10 min at 3s cadence).
      return next.length > 200 ? next.slice(next.length - 200) : next;
    });
  }, [marketQ.data, symbol]);

  const buildReq = (): TtOrderReq => ({
    symbol,
    side,
    order_type: orderType,
    qty: Number(qty),
    limit_price: orderType === 'limit' && limitPrice ? Number(limitPrice) : undefined,
    stop_price: orderType === 'stop' && stopPrice ? Number(stopPrice) : undefined,
    tif: orderType === 'market' ? 'ioc' : tif,
    mode,
  });

  // tt-submit/cancel idempotency: a stable hash of the *intended* request so a
  // double-click replays the SAME Idempotency-Key (server short-circuits) instead
  // of minting a fresh UUID per mutationFn call. FNV-1a 32-bit over canonical JSON.
  const stableHash = (obj: unknown): string => {
    const str = JSON.stringify(obj);
    let h = 0x811c9dc5;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 0x01000193);
    }
    return (h >>> 0).toString(16).padStart(8, '0');
  };
  const submitKeyStorage = (req: TtOrderReq): string => `tt-submit-${sessionId}-${stableHash(req)}`;

  const previewMut = useMutation({
    mutationFn: async () => {
      const req = buildReq();
      const p = await api.ttPreview(req, sessionId);
      return { req, preview: p };
    },
    onMutate: () => setMutError(null),
    onSuccess: ({ req, preview }) => {
      setMutError(null);
      if (!preview.ok) {
        setMutError(preview.validation.join('; '));
        setPreviewedReq(null);
        setShowPreview(null);
        return;
      }
      setPreviewedReq(req);
      setShowPreview(preview);
    },
    onError: (e) => { setPreviewedReq(null); setShowPreview(null); setMutError(e instanceof Error ? e.message : String(e)); },
  });
  const submitMut = useMutation({
    mutationFn: () => {
      // tt-preview-submit-decoupled — submit the FROZEN previewed request, never re-derive.
      // Persist a per-intended-order Idempotency-Key so a fast double-click
      // (button isPending flips on a later async re-render) replays the SAME
      // key and the backend's lookup_or_record short-circuits the duplicate
      // instead of executing — and filling — the order twice. Mirrors
      // live-trade deployMut / paper-trade runPaper.
      const req = previewedReq;
      if (!req) throw new Error('No previewed order to submit. Preview again.');
      const storageKey = submitKeyStorage(req);
      let key = sessionStorage.getItem(storageKey);
      if (!key) {
        key = cryptoUuid();
        sessionStorage.setItem(storageKey, key);
      }
      return api.ttSubmit(req, key, sessionId);
    },
    onSuccess: () => {
      // Invalidate by the FROZEN mode so the refreshed positions match what was
      // routed; clear the per-order key so the next intentional submit mints fresh.
      const submittedMode = previewedReq?.mode ?? mode;
      if (previewedReq) sessionStorage.removeItem(submitKeyStorage(previewedReq));
      setMutError(null);
      setShowPreview(null);
      setPreviewedReq(null);
      qc.invalidateQueries({ queryKey: queryKeys.ttPositions(submittedMode) });
      // P15/C-H6 — partial-match invalidation refreshes ALL ['tt-orders', ...]
      // caches across status/symbol combinations after a submit, so the "All
      // orders" view stays in sync with the per-symbol view without a refresh.
      qc.invalidateQueries({ queryKey: ['tt-orders'], exact: false });
    },
    onError: (e) => setMutError(e instanceof Error ? e.message : String(e)),
  });
  // P30-F3: useMutation only holds ONE variables/error slot. If we render
  // an inline error keyed by `cancelMut.variables === o.order_uid`, then
  // cancelling order B clobbers A's still-unacknowledged error.
  // Track errors keyed by uid in component state so each row keeps its
  // own failure context independently.
  const [cancelErrors, setCancelErrors] = useState<Record<string, string>>({});
  const [cancellingUid, setCancellingUid] = useState<string | null>(null);
  const cancelMut = useMutation({
    mutationFn: (uid: string) => {
      // Persist a per-order Idempotency-Key so a double-fire of the armed
      // confirm click replays the same key instead of issuing two cancels.
      const storageKey = `tt-cancel-${uid}`;
      let key = sessionStorage.getItem(storageKey);
      if (!key) {
        key = cryptoUuid();
        sessionStorage.setItem(storageKey, key);
      }
      return api.ttCancel(uid, key);
    },
    onMutate: (uid: string) => {
      setCancellingUid(uid);
      setCancelErrors((prev) => {
        if (!(uid in prev)) return prev;
        const { [uid]: _drop, ...rest } = prev;
        return rest;
      });
    },
    onSuccess: (_data, uid) => {
      setCancellingUid(null);
      sessionStorage.removeItem(`tt-cancel-${uid}`);
      setCancelErrors((prev) => {
        if (!(uid in prev)) return prev;
        const { [uid]: _drop, ...rest } = prev;
        return rest;
      });
      qc.invalidateQueries({ queryKey: ['tt-orders'], exact: false });
    },
    onError: (err, uid) => {
      setCancellingUid(null);
      setCancelErrors((prev) => ({
        ...prev,
        [uid]: err instanceof Error ? err.message : String(err),
      }));
    },
  });

  // P31-KILL — bulk kill-switch. Persists the idempotency key in sessionStorage
  // so a fast double-click (before isPending disables the button) replays the
  // SAME key and the backend deduplicates instead of executing cancel-all twice.
  // The key is cleared on success so the next intentional kill-switch invocation
  // mints a fresh key. Mirrors the submitMut / cancelMut sessionStorage pattern.
  const cancelAllMut = useMutation({
    mutationFn: (frozenMode: 'paper' | 'live') => {
      const storageKey = `tt-cancel-all-${frozenMode}`;
      let key = sessionStorage.getItem(storageKey);
      if (!key) {
        key = cryptoUuid();
        sessionStorage.setItem(storageKey, key);
      }
      return api.ttCancelAll(key, frozenMode);
    },
    onSuccess: (_data, frozenMode) => {
      sessionStorage.removeItem(`tt-cancel-all-${frozenMode}`);
      setMutError(null);
      setCancelAllArmed(false);
      qc.invalidateQueries({ queryKey: ['tt-orders'], exact: false });
      qc.invalidateQueries({ queryKey: queryKeys.ttPositions(frozenMode) });
    },
    onError: (e) => { setCancelAllArmed(false); setMutError(e instanceof Error ? e.message : String(e)); },
  });

  // P-flatten — when the "Close position" button prefills the ticket it sets
  // pendingPreview; this effect fires the Preview AFTER the ticket state has
  // committed, so buildReq() inside previewMut reads the prefilled values
  // (not a stale closure). One-shot: it clears the flag before mutating.
  useEffect(() => {
    if (!pendingPreview) return;
    setPendingPreview(false);
    previewMut.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingPreview]);

  // P29-F4: armed-confirm for order cancels.
  const [cancelArmedUid, setCancelArmedUid] = useState<string | null>(null);
  const cancelArmTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    return () => {
      if (cancelArmTimerRef.current) {
        clearTimeout(cancelArmTimerRef.current);
        cancelArmTimerRef.current = null;
      }
    };
  }, []);
  function armCancel(uid: string) {
    if (cancelArmedUid === uid) {
      if (cancelArmTimerRef.current) {
        clearTimeout(cancelArmTimerRef.current);
        cancelArmTimerRef.current = null;
      }
      setCancelArmedUid(null);
      cancelMut.mutate(uid);
      return;
    }
    if (cancelArmTimerRef.current) clearTimeout(cancelArmTimerRef.current);
    setCancelArmedUid(uid);
    cancelArmTimerRef.current = setTimeout(() => {
      setCancelArmedUid(null);
      cancelArmTimerRef.current = null;
    }, 3000);
  }

  if (statusQ.data && !statusQ.data.enabled) {
    return <DisabledState />;
  }

  return (
    <div className="grid h-full w-full grid-cols-12 gap-3 overflow-hidden p-3">
      <aside className="col-span-4 flex flex-col gap-3 overflow-y-auto">
        <Card title="Symbol">
          <select
            aria-label="Symbol"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-[11px] text-slate-200"
          >
            {/* Fallback option keeps the controlled select showing the selected
                symbol while the list is still loading or failed to load, instead
                of rendering a blank control with an out-of-range value. */}
            {!(symsQ.data?.symbols ?? []).some((s: TtSymbol) => s.symbol === symbol) && (
              <option value={symbol}>{symbol}</option>
            )}
            {(symsQ.data?.symbols ?? []).map((s: TtSymbol) => (
              <option key={s.symbol} value={s.symbol}>{s.symbol} ({Number.isFinite(s.last) ? s.last.toFixed(2) : '—'})</option>
            ))}
          </select>
          {symsQ.isLoading && (
            <div className="mt-1 text-[9px] text-slate-500">Loading symbols…</div>
          )}
          {symsQ.isError && (
            <div className="mt-1 text-[9px] text-rose-300">Failed to load symbol list — showing default only.</div>
          )}
        </Card>

        <Card title="Mode">
          <div className="flex overflow-hidden rounded-md border border-slate-700">
            <button onClick={() => { setMode('paper'); setPreviewedReq(null); setShowPreview(null); setMutError(null); }} className={`flex-1 px-2 py-1 text-[11px] ${mode === 'paper' ? 'bg-cyan-500/20 text-cyan-200' : 'text-slate-400'}`}>PAPER</button>
            <button
              onClick={() => { setShowLiveConfirm(true); }}  /* R5/UX-04: in-app typed-confirm modal instead of native window.confirm */
              disabled={!statusQ.data?.live_enabled}
              title={!statusQ.data?.live_enabled ? 'Set LIVE_TRADE_ENABLED=1 to enable' : ''}
              className={`flex-1 px-2 py-1 text-[11px] ${mode === 'live' ? 'bg-emerald-500/20 text-emerald-200' : 'text-slate-400'} disabled:opacity-40`}
            >
              LIVE
            </button>
          </div>
        </Card>

        {mode === 'live' && (
          <div
            role="alert"
            aria-live="assertive"
            className="flex items-center gap-2 rounded-lg border border-rose-600 bg-rose-500/20 px-3 py-2 text-[11px] font-bold uppercase tracking-wider text-rose-100"
          >
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            LIVE MODE — real capital at risk
          </div>
        )}

        <Card title="Order Ticket">
          <div className="space-y-2 text-[11px]">
            <div className="flex gap-2">
              {(['buy', 'sell'] as const).map((s) => (
                <button
                  key={s}
                  onClick={() => setSide(s)}
                  className={`flex-1 rounded-md py-1.5 font-bold uppercase tracking-wider ${side === s ? (s === 'buy' ? 'bg-emerald-500/30 text-emerald-200' : 'bg-rose-500/30 text-rose-200') : 'border border-slate-700 text-slate-400'}`}
                >
                  {s}
                </button>
              ))}
            </div>
            <select aria-label="Order type" value={orderType} onChange={(e) => setOrderType(e.target.value as any)} className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-slate-200">
              <option value="market">Market</option>
              <option value="limit">Limit</option>
              <option value="stop">Stop</option>
            </select>
            <input
              aria-label="Quantity"
              type="number"
              value={qty}
              step="0.0001"
              onChange={(e) => setQty(e.target.value)}
              placeholder="Qty"
              className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-slate-200"
            />
            {orderType === 'limit' && (
              <input
                aria-label="Limit price"
                type="number"
                value={limitPrice}
                step="0.01"
                onChange={(e) => setLimitPrice(e.target.value)}
                placeholder="Limit price"
                className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-slate-200"
              />
            )}
            {orderType === 'stop' && (
              <input
                aria-label="Stop trigger price"
                type="number"
                value={stopPrice}
                step="0.01"
                onChange={(e) => setStopPrice(e.target.value)}
                placeholder="Stop trigger price"
                className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-slate-200"
              />
            )}
            {orderType === 'limit' && (
              <select aria-label="Time in force" value={tif} onChange={(e) => setTif(e.target.value as any)} className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-slate-200">
                <option value="gtc">GTC</option><option value="ioc">IOC</option><option value="fok">FOK</option>
              </select>
            )}
            <button
              onClick={() => previewMut.mutate()}
              disabled={
                !sessionId ||
                previewMut.isPending ||
                !(Number(qty) > 0) ||
                (orderType === 'limit' && !(Number(limitPrice) > 0)) ||
                (orderType === 'stop' && !(Number(stopPrice) > 0))
              }
              className="flex w-full items-center justify-center gap-1 rounded-md border border-cyan-700 bg-cyan-500/20 px-3 py-1.5 font-bold text-cyan-200 hover:bg-cyan-500/30 disabled:opacity-40"
            >
              <Send className="h-3 w-3" /> Preview
            </button>
            {mutError && (
              <div className="rounded-md border border-rose-700/40 bg-rose-500/10 px-2 py-1.5 text-[10px] text-rose-200">
                {mutError}
              </div>
            )}
          </div>
        </Card>

        <Card title="Recent Orders">
          {(ordersQ.data?.orders ?? []).filter((o) => o.status === 'pending').length > 0 && (
            <button
              type="button"
              onClick={armCancelAll}
              disabled={cancelAllMut.isPending}
              title="Kill-switch: cancel ALL pending orders for the active mode"
              className={`mb-2 w-full rounded border px-2 py-1 text-[10px] font-bold uppercase tracking-wider transition disabled:cursor-not-allowed disabled:opacity-40 ${cancelAllArmed ? 'border-rose-500 bg-rose-500/20 text-rose-200 ring-1 ring-rose-500/50' : 'border-rose-700/60 bg-rose-500/10 text-rose-300 hover:bg-rose-500/20'}`}
            >
              {cancelAllMut.isPending ? 'CANCELLING…' : cancelAllArmed ? 'CONFIRM — KILL ALL PENDING' : 'CANCEL ALL PENDING'}
            </button>
          )}
          <ul className="space-y-1 text-[11px]">
            {(ordersQ.data?.orders ?? []).slice(0, 20).map((o) => (
              <li key={o.order_uid} className="rounded border border-slate-900 bg-slate-950/40 px-2 py-1.5">
                <div className="flex items-center justify-between">
                  <span className={`font-mono ${o.side === 'buy' ? 'text-emerald-300' : 'text-rose-300'}`}>{o.side.toUpperCase()}</span>
                  <span className={`rounded px-1 py-0.5 text-[9px] font-bold uppercase ${o.mode === 'live' ? 'bg-emerald-500/20 text-emerald-200' : 'bg-cyan-500/20 text-cyan-200'}`}>{(o.mode ?? '').toUpperCase()}</span>
                  <span className="font-mono text-slate-300">{Number.isFinite(o.qty) ? o.qty.toFixed(4) : '—'}</span>
                  <span className={`rounded px-1 py-0.5 text-[9px] uppercase ${statusCls(o.status)}`}>{o.status}</span>
                </div>
                <div className="text-[9px] text-slate-500">{o.requested_at ? new Date(o.requested_at).toLocaleTimeString() : ''} · {o.order_type}{o.limit_price ? ` @ ${o.limit_price}` : ''}</div>
                {o.status === 'pending' && (o.order_type === 'limit' || o.order_type === 'stop') && (
                  // P31-T4: extend cancel UI to stop orders. Backend cancel_order
                  // already supports any pending status; previously the UI only
                  // gated 'limit', trapping operators with a wrong-priced stop.
                  <button
                    onClick={() => armCancel(o.order_uid)}
                    title={cancelArmedUid === o.order_uid ? 'Click again within 3s to confirm cancel' : `Cancel this ${o.order_type} order`}
                    className={`mt-1 text-[9px] ${cancelArmedUid === o.order_uid ? 'font-bold text-rose-100' : 'text-rose-300 hover:text-rose-200'}`}
                    disabled={cancellingUid === o.order_uid}
                  >
                    {cancelArmedUid === o.order_uid ? 'Click again to confirm' : 'Cancel'}
                  </button>
                )}
                {cancelErrors[o.order_uid] && (
                  <div className="mt-1 rounded border border-rose-700/40 bg-rose-500/10 px-1 py-0.5 text-[9px] text-rose-200">
                    Cancel failed: {cancelErrors[o.order_uid]}
                  </div>
                )}
              </li>
            ))}
            {!ordersQ.data?.orders.length && <li className="text-[10px] text-slate-500">No orders yet.</li>}
          </ul>
        </Card>
      </aside>

      <main className="col-span-8 flex flex-col gap-3 overflow-y-auto">
        <Card title="Market">
          <MarketStrip info={marketQ.data} isError={marketQ.isError} />
        </Card>
        <Card title={`Price · ${symbol}`}>
          <CandlestickChart
            ticks={priceTicks}
            currentPrice={marketQ.data?.last}
            height={200}
          />
        </Card>
        <Card title="Position">
          <PositionStrip
            symbol={symbol}
            positions={posQ.data?.positions ?? []}
            last={marketQ.data?.last ?? 0}
            onClose={(closeSide, absQty) => {
              // P-flatten: first-class flatten. Pre-fill an opposing MARKET order
              // and route it through the SAME Preview->confirm flow (so the
              // fat-finger / cost preview still applies). Reuses buildReq()/
              // previewMut; does NOT change the order-submission contract.
              setSide(closeSide);
              setOrderType('market');
              setQty(String(absQty));
              setMutError(null);
              setPendingPreview(true);
            }}
          />
        </Card>
      </main>

      {showPreview && (
        <PreviewModal
          preview={showPreview}
          mode={previewedReq?.mode ?? 'paper'}
          onCancel={() => { setShowPreview(null); setPreviewedReq(null); }}
          onConfirm={() => submitMut.mutate()}
          isPending={submitMut.isPending}
          error={submitMut.error?.message}
        />
      )}

      {showLiveConfirm && (
        <LiveModeConfirmModal
          onCancel={() => setShowLiveConfirm(false)}
          onConfirm={() => { setMode('live'); setPreviewedReq(null); setShowPreview(null); setMutError(null); setShowLiveConfirm(false); }}
        />
      )}
    </div>
  );
}

function DisabledState() {
  return (
    <div className="flex h-full items-center justify-center p-6">
      <div className="max-w-md rounded-2xl border border-slate-800 bg-slate-900/40 p-6 text-center">
        <Joystick className="mx-auto mb-3 h-8 w-8 text-cyan-400" />
        <h1 className="mb-2 text-sm font-bold uppercase tracking-widest text-slate-100">Trading Terminal Disabled</h1>
        <p className="text-xs text-slate-400">
          Set <code className="rounded bg-slate-800 px-1 text-cyan-300">TRADING_TERMINAL_ENABLED=1</code> in <code>.env</code> and restart the backend to enable manual order entry. Default is PAPER mode; live routing remains gated behind the separate <code className="rounded bg-slate-800 px-1 text-cyan-300">LIVE_TRADE_ENABLED</code> flag.
        </p>
      </div>
    </div>
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

function MarketStrip({ info, isError }: { info?: TtMarketInfo; isError?: boolean }) {
  if (isError) return <div className="text-[11px] text-rose-300">Failed to load market data.</div>;
  if (!info) return <div className="text-[11px] text-slate-500">Loading…</div>;
  return (
    <div className="space-y-1">
      {info.price_source === 'mock_synthetic' && (
        <div className="rounded border border-amber-700/40 bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-300">
          MOCK PRICE — synthetic ratio from BTC; not a real {info.symbol} market feed.
        </div>
      )}
      <div className="grid grid-cols-6 gap-2 text-[11px]">
        <Item label="Last" value={Number.isFinite(info.last) ? info.last.toFixed(2) : '—'} cls="text-cyan-300" />
        <Item label="Bid" value={Number.isFinite(info.bid) ? info.bid.toFixed(2) : '—'} cls="text-emerald-300" />
        <Item label="Ask" value={Number.isFinite(info.ask) ? info.ask.toFixed(2) : '—'} cls="text-rose-300" />
        <Item label="Spread bps" value={Number.isFinite(info.spread_bps) ? info.spread_bps.toFixed(1) : '—'} cls="text-amber-300" />
        <Item label={`24h Vol (${info.symbol.split('-')[0] ?? 'base'})`} value={Number.isFinite(info.vol_24h) ? info.vol_24h.toFixed(0) : '—'} cls="text-slate-300" />
        <Item label="24h Δ" value={Number.isFinite(info.change_24h_pct) ? `${(info.change_24h_pct * 100).toFixed(2)}%` : '—'} cls={!Number.isFinite(info.change_24h_pct) ? 'text-slate-400' : info.change_24h_pct >= 0 ? 'text-emerald-300' : 'text-rose-300'} />
      </div>
    </div>
  );
}

function PositionStrip({ symbol, positions, last, onClose }: { symbol: string; positions: TtPosition[]; last: number; onClose: (side: 'buy' | 'sell', absQty: number) => void }) {
  const cur = positions.find((p) => p.symbol === symbol);
  if (!cur || Math.abs(cur.qty_signed) < 1e-9) {
    return <div className="text-[11px] text-slate-500">FLAT — no position in {symbol}</div>;
  }
  const side = cur.qty_signed > 0 ? 'LONG' : 'SHORT';
  const upnl = (last - cur.avg_entry_price) * cur.qty_signed;
  const closeSide: 'buy' | 'sell' = cur.qty_signed > 0 ? 'sell' : 'buy';
  const absQty = Math.abs(cur.qty_signed);
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-5 gap-2 text-[11px]">
        <Item label="Side" value={side} cls={cur.qty_signed > 0 ? 'text-emerald-300' : 'text-rose-300'} />
        <Item label="Qty" value={Number.isFinite(cur.qty_signed) ? absQty.toFixed(4) : '—'} cls="text-slate-200" />
        <Item label="Avg Entry" value={Number.isFinite(cur.avg_entry_price) ? cur.avg_entry_price.toFixed(2) : '—'} cls="text-slate-200" />
        <Item label="uPnL" value={Number.isFinite(upnl) && last > 0 ? `${upnl >= 0 ? '+' : ''}${upnl.toFixed(2)}` : '—'} cls={Number.isFinite(upnl) && last > 0 ? (upnl >= 0 ? 'text-emerald-300' : 'text-rose-300') : 'text-slate-400'} />
        <Item label="Realized" value={Number.isFinite(cur.realized_pnl_quote) ? cur.realized_pnl_quote.toFixed(2) : '—'} cls="text-slate-300" />
      </div>
      <button
        onClick={() => onClose(closeSide, absQty)}
        title={`Flatten: prefill a ${closeSide.toUpperCase()} MARKET ${absQty.toFixed(4)} order, then Preview to confirm`}
        className="flex w-full items-center justify-center gap-1 rounded-md border border-amber-700 bg-amber-500/15 px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider text-amber-200 hover:bg-amber-500/25"
      >
        Close position ({closeSide.toUpperCase()} {absQty.toFixed(4)})
      </button>
    </div>
  );
}

function Item({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-950/50 p-2">
      <div className="text-[9px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className={`mt-0.5 font-mono ${cls ?? 'text-slate-200'}`}>{value}</div>
    </div>
  );
}

function PreviewModal({ preview, mode, onCancel, onConfirm, isPending, error }: { preview: TtOrderPreview; mode: 'paper' | 'live'; onCancel: () => void; onConfirm: () => void; isPending: boolean; error?: string }) {
  // P15/C-M16 — Escape closes the preview unless a submit is in flight.
  useEscapeKey(true, () => { if (!isPending) onCancel(); });
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70" onClick={() => { if (!isPending) onCancel(); }}>
      <div
        className="w-full max-w-md rounded-2xl border border-slate-700 bg-slate-900 p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="tt-preview-title"
      >
        <h2 id="tt-preview-title" className="flex items-center gap-2 text-sm font-bold uppercase tracking-widest text-cyan-300">
          Confirm Order · <span className={mode === 'live' ? 'text-emerald-300' : 'text-cyan-300'}>{mode.toUpperCase()}</span>
        </h2>
        {preview.validation.length > 0 && (
          <div className="mt-2 rounded-md border border-rose-700/40 bg-rose-500/10 px-2 py-1.5 text-[11px] text-rose-200">
            {preview.validation.join('; ')}
          </div>
        )}
        <ul className="mt-3 space-y-1 text-[11px] font-mono">
          <li className="flex justify-between"><span className="text-slate-500">Est cost</span><span className="text-slate-200">{Number.isFinite(preview.est_cost_quote) ? preview.est_cost_quote.toFixed(2) : '—'}</span></li>
          <li className="flex justify-between"><span className="text-slate-500">Fee ({preview.est_fee_bps}bps)</span><span className="text-slate-200">{Number.isFinite(preview.est_fee_quote) ? preview.est_fee_quote.toFixed(2) : '—'}</span></li>
          <li className="flex justify-between"><span className="text-slate-500">Slippage est</span><span className="text-slate-200">{Number.isFinite(preview.est_slippage_bps) ? `${preview.est_slippage_bps.toFixed(1)}bps` : '—'}</span></li>
          <li className="flex justify-between"><span className="text-slate-500">Mark</span><span className="text-slate-200">{Number.isFinite(preview.mark_price) ? preview.mark_price.toFixed(2) : '—'}</span></li>
        </ul>
        {(preview.fat_finger_warning || preview.slippage_warning) && (
          <div className="mt-2 rounded-md border border-amber-700/40 bg-amber-500/10 px-2 py-1.5 text-[11px] text-amber-200">
            <AlertTriangle className="mr-1 inline h-3 w-3" />
            {preview.fat_finger_warning ? 'Price >5% from last. ' : ''}
            {preview.slippage_warning ? 'Order is >10% of last-hour volume.' : ''}
          </div>
        )}
        {error && <div className="mt-2 text-[10px] text-rose-300">{error}</div>}
        <div className="mt-4 flex justify-end gap-2">
          <button onClick={() => { if (!isPending) onCancel(); }} disabled={isPending} className="rounded-md border border-slate-700 px-3 py-1 text-[11px] text-slate-300 hover:bg-slate-800 disabled:opacity-40">Cancel</button>
          <button
            onClick={onConfirm}
            disabled={!preview.ok || isPending}
            className="rounded-md border border-cyan-700 bg-cyan-500/20 px-3 py-1 text-[11px] font-bold text-cyan-200 hover:bg-cyan-500/30 disabled:opacity-40"
          >
            {isPending ? 'Submitting…' : 'Submit'}
          </button>
        </div>
      </div>
    </div>
  );
}

// R5/UX-04 — in-app LIVE-mode confirmation (typed phrase + 3s countdown),
// replacing native window.confirm() which is inconsistent with the app's modal
// pattern and is suppressed in headless/embedded contexts.
function useTimer(value: number, setter: (v: number) => void) {
  useEffect(() => {
    if (value <= 0) return;
    const t = setTimeout(() => setter(value - 1), 1000);
    return () => clearTimeout(t);
  }, [value, setter]);
}

function LiveModeConfirmModal({ onCancel, onConfirm }: { onCancel: () => void; onConfirm: () => void }) {
  const [typed, setTyped] = useState('');
  const [counter, setCounter] = useState(3);
  const okPhrase = 'LIVE TRADE';
  useTimer(counter, setCounter);
  useEscapeKey(true, onCancel);
  const ready = typed === okPhrase && counter <= 0;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70"
      onClick={onCancel}
    >
      <div
        className="w-full max-w-md rounded-2xl border border-emerald-700 bg-slate-900 p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="live-mode-confirm-title"
      >
        <h2 id="live-mode-confirm-title" className="flex items-center gap-2 text-sm font-bold uppercase tracking-widest text-emerald-300">
          Confirm LIVE Mode
        </h2>
        <p className="mt-3 text-[12px] text-slate-300">
          All subsequent order submissions will route to the <span className="font-mono text-emerald-300">real exchange</span>. This cannot be undone without switching back to PAPER mode.
        </p>
        <input
          autoFocus
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder={`Type "${okPhrase}" to enable`}
          className="mt-3 w-full rounded border border-slate-700 bg-slate-950 px-3 py-1.5 text-[11px] text-slate-200"
        />
        <div className="mt-3 flex items-center justify-between">
          <span className="text-[10px] text-slate-500">
            {counter > 0 ? `Wait ${counter}s` : 'Ready'}
          </span>
          <div className="flex gap-2">
            <button onClick={onCancel} className="rounded-md border border-slate-700 px-3 py-1 text-[11px] text-slate-300 hover:bg-slate-800">Cancel</button>
            <button
              onClick={onConfirm}
              disabled={!ready}
              className="rounded-md border border-emerald-700 bg-emerald-500/20 px-3 py-1 text-[11px] font-bold text-emerald-200 hover:bg-emerald-500/30 disabled:opacity-40"
            >
              Confirm LIVE
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function statusCls(s: string): string {
  if (s === 'filled') return 'bg-emerald-500/20 text-emerald-200';
  if (s === 'cancelled' || s === 'rejected') return 'bg-slate-700/30 text-slate-400';
  if (s === 'pending') return 'bg-amber-500/20 text-amber-200';
  if (s === 'orphaned') return 'bg-rose-500/20 text-rose-200';
  return 'bg-slate-800 text-slate-300';
}
