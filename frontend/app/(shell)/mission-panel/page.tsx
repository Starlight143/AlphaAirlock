'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Activity,
  AlertTriangle,
  Bell,
  Bot,
  ListChecks,
  Minus,
  RefreshCcw,
  Send,
  TrendingDown,
  TrendingUp,
} from 'lucide-react';

import { api, cryptoUuid, type DaemonRow, type MissionPanelSnapshot } from '@/lib/api';
import { queryKeys } from '@/lib/query';

/**
 * /mission-panel — P7-02
 * Tactical ops console mirroring the Telegram 6h-pulse. 30s refresh.
 */
export default function MissionPanelPage() {
  const qc = useQueryClient();
  const snapQ = useQuery({
    queryKey: queryKeys.missionPanelSnapshot,
    queryFn: api.missionPanelSnapshot,
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
  });
  const [showConfirm, setShowConfirm] = useState(false);
  const [fireError, setFireError] = useState<string | null>(null);
  const fireMut = useMutation({
    mutationFn: () => {
      // D-H9 parity — stable idempotency key per fire-intent. Survives a mid-
      // request reload / rapid double-confirm so the server replays the cached
      // send instead of double-posting to the operator Telegram channel.
      const storageKey = 'mission-panel-fire-six-hour-key';
      let key = sessionStorage.getItem(storageKey);
      if (!key) {
        key = cryptoUuid();
        sessionStorage.setItem(storageKey, key);
      }
      return api.missionPanelFireSixHour(true, { idempotencyKey: key });
    },
    onSuccess: () => {
      // Clear so the NEXT distinct fire-intent mints a fresh key (next UTC hour
      // is a fresh first-compute path on the server anyway).
      sessionStorage.removeItem('mission-panel-fire-six-hour-key');
      setFireError(null);
      qc.invalidateQueries({ queryKey: queryKeys.missionPanelSnapshot });
    },
    onError: (e) => setFireError(e instanceof Error ? e.message : String(e)),
  });
  const data = snapQ.data;

  return (
    <div className="flex h-full w-full flex-col gap-3 overflow-y-auto p-3">
      <header className="flex items-center justify-between rounded-xl border border-slate-800 bg-slate-900/40 px-3 py-2">
        <div>
          <h1 className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
            <ListChecks className="h-3.5 w-3.5 text-cyan-400" /> Mission Panel
          </h1>
          <p className="mt-0.5 text-[11px] text-slate-500">
            Tactical ops console · web mirror of the Telegram 6h pulse
          </p>
        </div>
        <button
          onClick={() => qc.invalidateQueries({ queryKey: queryKeys.missionPanelSnapshot })}
          className="inline-flex items-center gap-1.5 rounded-md border border-slate-700 px-2 py-1 text-[10px] text-slate-400 hover:bg-slate-800"
        >
          <RefreshCcw className="h-3 w-3" /> Refresh
        </button>
      </header>

      <DailyTickerStrip today={data?.daily_ticker.today} yesterday={data?.daily_ticker.yesterday} />

      <div className="grid grid-cols-12 gap-3">
        <Card title="Incidents" icon={<AlertTriangle className="h-3 w-3" />} className="col-span-4">
          <IncidentsCard incidents={data?.incidents} />
        </Card>
        <Card title="Daemon Health" icon={<Bot className="h-3 w-3" />} className="col-span-4">
          <DaemonHealthGrid daemons={data?.daemons ?? []} />
        </Card>
        <Card title="Recent Dispatches" icon={<Activity className="h-3 w-3" />} className="col-span-4">
          <DispatchFeed transitions={data?.dispatches.recent_transitions ?? []} />
        </Card>

        <Card title="System Pulse Preview" icon={<Bell className="h-3 w-3" />} className="col-span-8">
          <SystemPulsePreview
            pulse={data?.pulse_preview}
            isFiring={fireMut.isPending}
            onFire={() => setShowConfirm(true)}
            fireError={fireError}
          />
        </Card>
        <Card title="Last Telegram Reports" icon={<Send className="h-3 w-3" />} className="col-span-4">
          <LastReports rows={data?.last_telegram_reports ?? []} />
        </Card>
      </div>

      {showConfirm && (
        <ConfirmFireModal
          onCancel={() => setShowConfirm(false)}
          onConfirm={() => {
            setShowConfirm(false);
            fireMut.mutate();
          }}
        />
      )}
    </div>
  );
}

function Card({ title, icon, className = '', children }: { title: string; icon?: React.ReactNode; className?: string; children: React.ReactNode }) {
  return (
    <section className={`flex flex-col rounded-xl border border-slate-800 bg-slate-900/40 p-3 ${className}`}>
      <header className="mb-2 flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">
        {icon}{title}
      </header>
      <div className="flex-1 min-h-[180px]">{children}</div>
    </section>
  );
}

function DailyTickerStrip({ today, yesterday }: { today?: { new_strategies: number; ic_ingested: number; paper_pnl_pct: number | null }; yesterday?: { new_strategies: number; ic_ingested: number; paper_pnl_pct: number | null } }) {
  const items = [
    { label: 'New Strategies', t: today?.new_strategies ?? 0, y: yesterday?.new_strategies ?? 0, color: 'text-cyan-300' },
    { label: 'IC Ingested', t: today?.ic_ingested ?? 0, y: yesterday?.ic_ingested ?? 0, color: 'text-emerald-300' },
    { label: 'Paper PnL %', t: today?.paper_pnl_pct ?? 0, y: yesterday?.paper_pnl_pct ?? 0, color: 'text-amber-300', isPct: true },
  ];
  return (
    <div className="grid grid-cols-3 gap-3">
      {items.map((it) => {
        const delta = it.t - it.y;
        const fmt = (v: number) => (it.isPct ? `${v.toFixed(2)}%` : String(v));
        return (
          <div key={it.label} className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
            <div className="text-[10px] uppercase tracking-widest text-slate-500">{it.label}</div>
            <div className={`mt-1 font-mono text-2xl ${it.color}`}>{fmt(it.t)}</div>
            <div className="mt-0.5 text-[9px] text-slate-500">
              vs {fmt(it.y)} yesterday
              <span
                className={`ml-2 inline-flex items-center gap-0.5 font-mono ${
                  delta > 0
                    ? 'text-emerald-400'
                    : delta < 0
                    ? 'text-rose-400'
                    : 'text-slate-500'
                }`}
              >
                {delta > 0 ? (
                  <TrendingUp className="h-2.5 w-2.5" />
                ) : delta < 0 ? (
                  <TrendingDown className="h-2.5 w-2.5" />
                ) : (
                  <Minus className="h-2.5 w-2.5" />
                )}
                {fmt(Math.abs(delta))}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function IncidentsCard({ incidents }: { incidents?: MissionPanelSnapshot['incidents'] }) {
  if (!incidents) return <Empty>Loading…</Empty>;
  const list: { kind: string; severity: string; summary: string }[] = [];
  for (const e of incidents.ingest_failures_recent.slice(0, 5)) {
    list.push({ kind: 'ingest_fail', severity: 'warn', summary: e.error_msg ?? `source ${e.source_id} failed` });
  }
  for (const d of incidents.daemon_errors.slice(0, 5)) {
    list.push({ kind: 'daemon_error', severity: 'error', summary: `${d.task_id}: ${d.last_error}` });
  }
  for (const p of incidents.unhealthy_paper_runs.slice(0, 5)) {
    list.push({ kind: 'paper_unhealthy', severity: 'error', summary: `S#${p.strategy_id}: ${p.reason}` });
  }
  if (!list.length) return <Empty>No incidents</Empty>;
  return (
    <ul className="space-y-1 text-[11px]">
      {list.slice(0, 8).map((it, i) => (
        // P16 A-L6 — stable key combining kind + summary prefix +
        // positional index. Pure `key={i}` caused React to reuse
        // stale rows when the incidents feed reordered, producing a
        // visible flicker every 30s refresh.
        <li
          key={`${it.kind}-${it.summary.slice(0, 40)}-${i}`}
          className="flex items-start gap-2 rounded-md border border-slate-900 bg-slate-950/40 px-2 py-1.5"
        >
          <span className={`mt-1 h-2 w-2 shrink-0 rounded-full ${it.severity === 'error' ? 'bg-rose-400' : 'bg-amber-400'}`} />
          <div className="flex-1 overflow-hidden">
            <div className="truncate text-slate-200">{it.summary}</div>
            <div className="text-[9px] uppercase tracking-wider text-slate-500">{it.kind}</div>
          </div>
        </li>
      ))}
    </ul>
  );
}

function DaemonHealthGrid({ daemons }: { daemons: DaemonRow[] }) {
  if (!daemons.length) return <Empty>No periodic tasks registered</Empty>;
  return (
    <ul className="space-y-1 text-[11px]">
      {daemons.map((d) => {
        const tone =
          d.schedule_status === 'on_time' ? 'text-cyan-200 bg-cyan-500/15' :
          d.schedule_status === 'lagging' ? 'text-amber-200 bg-amber-500/15' :
          d.schedule_status === 'overdue' ? 'text-rose-200 bg-rose-500/15' :
          d.schedule_status === 'never_ran' ? 'text-slate-400 bg-slate-700/30' :
          'text-slate-500 bg-slate-800/30';
        return (
          <li key={d.task_id} className="flex items-center justify-between rounded-md border border-slate-900 bg-slate-950/40 px-2 py-1.5">
            <div className="flex-1 overflow-hidden">
              <div className="truncate font-mono text-slate-200">{d.task_id}</div>
              <div className="text-[9px] text-slate-500">
                {d.interval_seconds ? `every ${d.interval_seconds}s` : 'disabled'} ·{' '}
                {d.last_run_at ? new Date(d.last_run_at).toLocaleTimeString() : 'never'}
              </div>
            </div>
            <span className={`shrink-0 rounded px-1.5 py-0.5 text-[9px] uppercase tracking-wider ${tone}`}>
              {d.schedule_status ?? '—'}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function DispatchFeed({ transitions }: { transitions: { strategy_id: number; name: string; status: string; updated_at: string | null }[] }) {
  if (!transitions.length) return <Empty>No recent transitions</Empty>;
  return (
    <ul className="space-y-1 text-[11px]">
      {transitions.slice(0, 12).map((t) => (
        <li key={`${t.strategy_id}-${t.updated_at}`} className="flex items-center justify-between rounded-md border border-slate-900 bg-slate-950/40 px-2 py-1.5">
          <Link href={`/strategies/${t.strategy_id}`} className="truncate text-slate-200 hover:text-cyan-300">
            S#{t.strategy_id} {t.name}
          </Link>
          <div className="ml-2 flex items-center gap-2 text-[9px]">
            <span className="rounded-sm bg-slate-800 px-1.5 py-0.5 font-mono text-cyan-200">→ {t.status}</span>
            <span className="text-slate-500">{t.updated_at ? new Date(t.updated_at).toLocaleTimeString() : ''}</span>
          </div>
        </li>
      ))}
    </ul>
  );
}

function SystemPulsePreview({ pulse, isFiring, onFire, fireError }: { pulse?: MissionPanelSnapshot['pulse_preview']; isFiring: boolean; onFire: () => void; fireError?: string | null }) {
  if (!pulse) return <Empty>Loading…</Empty>;
  return (
    <div className="flex h-full flex-col">
      <div className="mb-2 flex items-center justify-between">
        <div className="text-[10px] text-slate-500">
          Next auto-fire:{' '}
          <span className="font-mono text-cyan-300">
            {pulse.would_send_at ? new Date(pulse.would_send_at).toLocaleString() : '—'}
          </span>
        </div>
        <button
          disabled={!pulse.telegram_configured || isFiring}
          onClick={onFire}
          className="rounded-md border border-cyan-700 bg-cyan-500/10 px-3 py-1 text-[10px] font-bold uppercase tracking-wider text-cyan-200 hover:bg-cyan-500/20 disabled:opacity-50"
        >
          {isFiring ? 'Sending…' : 'Fire 6h Pulse Now'}
        </button>
      </div>
      {!pulse.telegram_configured && (
        <div className="mb-2 rounded-md border border-amber-700/40 bg-amber-500/10 px-2 py-1.5 text-[10px] text-amber-200">
          Telegram not configured — preview only. Set TELEGRAM_ENABLED + TOKEN + CHAT_ID to enable sending.
        </div>
      )}
      {fireError && (
        <div className="mb-2 rounded-md border border-rose-700/40 bg-rose-500/10 px-2 py-1.5 text-[10px] text-rose-200">
          Fire failed: {fireError}
        </div>
      )}
      <pre className="flex-1 overflow-auto whitespace-pre-wrap rounded-md border border-slate-800 bg-slate-950/80 p-3 font-mono text-[11px] text-slate-300">
        {pulse.rendered_markdown}
      </pre>
    </div>
  );
}

function LastReports({ rows }: { rows: MissionPanelSnapshot['last_telegram_reports'] }) {
  if (!rows.length) return <Empty>No reports sent yet</Empty>;
  return (
    <ul className="space-y-1 text-[11px]">
      {rows.map((r) => (
        <li key={r.id} className="flex items-center justify-between rounded-md border border-slate-900 bg-slate-950/40 px-2 py-1.5">
          <div>
            <div className="font-mono text-slate-200">{r.report_type}</div>
            <div className="text-[9px] text-slate-500">{r.sent_at ? new Date(r.sent_at).toLocaleString() : '—'}</div>
          </div>
          <span
            role="img"
            aria-label={r.success ? 'Sent successfully' : 'Send failed'}
            className={`h-2 w-2 rounded-full ${r.success ? 'bg-emerald-400' : 'bg-rose-400'}`}
          />
        </li>
      ))}
    </ul>
  );
}

function ConfirmFireModal({ onCancel, onConfirm }: { onCancel: () => void; onConfirm: () => void }) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);

  // A11y — Escape closes the modal (parity with backdrop click / Cancel).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  // A11y — trap Tab/Shift+Tab within the two modal buttons.
  function handleTabTrap(e: React.KeyboardEvent<HTMLDivElement>) {
    if (e.key !== 'Tab') return;
    const focusable = [cancelRef.current, confirmRef.current].filter(
      (el): el is HTMLButtonElement => el !== null,
    );
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey) {
      if (document.activeElement === first) {
        e.preventDefault();
        last.focus();
      }
    } else {
      if (document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70" onClick={onCancel}>
      <div
        className="w-full max-w-md rounded-2xl border border-slate-700 bg-slate-900 p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={handleTabTrap}
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-fire-title"
      >
        <h2 id="confirm-fire-title" className="text-sm font-bold uppercase tracking-widest text-cyan-300">Fire 6h Pulse Now?</h2>
        <p className="mt-3 text-[12px] text-slate-300">
          This bypasses the normal cooldown and sends the pulse markdown to Telegram immediately. Audit-logged.
        </p>
        <div className="mt-5 flex justify-end gap-2">
          {/* autoFocus lands keyboard focus inside the modal on open; cancelRef + confirmRef enable the Tab trap above. */}
          <button ref={cancelRef} autoFocus onClick={onCancel} className="rounded-md border border-slate-700 px-3 py-1 text-[11px] text-slate-300 hover:bg-slate-800">Cancel</button>
          <button ref={confirmRef} onClick={onConfirm} className="rounded-md border border-cyan-700 bg-cyan-500/20 px-3 py-1 text-[11px] font-bold text-cyan-200 hover:bg-cyan-500/30">Confirm Fire</button>
        </div>
      </div>
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full min-h-[120px] items-center justify-center text-center text-[11px] text-slate-500">
      {children}
    </div>
  );
}
