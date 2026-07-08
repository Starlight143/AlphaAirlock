'use client';

/**
 * P19 F3 — Mission Control alert-channel rail.
 *
 * Reference video (Snipaste_2026-05-24_19-49-47.png) shows Telegram / Discord /
 * Email status dots underneath the system metrics column. The data is already
 * exposed at /api/scheduler/status (the same blob /sources renders via
 * BotInboundCard), so this is a purely additive surface that reuses
 * `api.schedulerStatus` + `queryKeys.schedulerStatus` — no new backend, no new
 * cache key.
 *
 * Email is rendered as a permanent "not configured" row because the project
 * ships no email notifier module today (only Telegram/Discord exist). Listing
 * three channels keeps visual parity with the reference UI; the "disabled"
 * dot is honest about the current state instead of hiding the row.
 */

import { useQuery } from '@tanstack/react-query';
import { MessageSquare, Hash, Mail } from 'lucide-react';
import clsx from 'clsx';
import { api } from '@/lib/api';
import { queryKeys } from '@/lib/query';

type ChannelRow = {
  key: 'telegram' | 'discord' | 'email';
  label: string;
  icon: JSX.Element;
  enabled: boolean;
  running: boolean;
  detail: string;
  errorHint?: string;
};

function dotClass(enabled: boolean, running: boolean): string {
  if (!enabled) return 'text-slate-600';
  if (running) return 'text-emerald-400';
  return 'text-amber-400';
}

function statusLabel(enabled: boolean, running: boolean): string {
  if (!enabled) return 'disabled';
  if (running) return 'online';
  return 'configured';
}

export default function AlertChannels(): JSX.Element {
  const q = useQuery({
    queryKey: queryKeys.schedulerStatus,
    queryFn: api.schedulerStatus,
    refetchInterval: 10_000,
  });

  const tg = q.data?.telegram_inbound;
  const dc = q.data?.discord_inbound;

  const rows: ChannelRow[] = [
    {
      key: 'telegram',
      label: 'Telegram',
      icon: <MessageSquare className="h-3 w-3 text-cyan-300" aria-hidden="true" />,
      enabled: !!tg?.enabled,
      running: !!tg?.running,
      detail:
        typeof tg?.allowed_chats_count === 'number'
          ? `${tg.allowed_chats_count} chat${tg.allowed_chats_count === 1 ? '' : 's'}`
          : '—',
      errorHint: tg?.error || undefined,
    },
    {
      key: 'discord',
      label: 'Discord',
      icon: <Hash className="h-3 w-3 text-indigo-300" aria-hidden="true" />,
      enabled: !!dc?.enabled,
      running: !!dc?.running,
      detail:
        typeof dc?.allowed_channels_count === 'number'
          ? `${dc.allowed_channels_count} channel${dc.allowed_channels_count === 1 ? '' : 's'}`
          : '—',
      errorHint: dc?.error || undefined,
    },
    {
      key: 'email',
      label: 'Email',
      icon: <Mail className="h-3 w-3 text-slate-500" aria-hidden="true" />,
      enabled: false,
      running: false,
      detail: 'not configured',
    },
  ];

  return (
    <aside className="flex shrink-0 flex-col rounded-xl border border-slate-800 bg-slate-900/50 p-2">
      <header className="mb-2 flex items-center justify-between">
        <h2 className="text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">
          Alert Channels
        </h2>
        {q.isError && (
          <span
            className="rounded border border-rose-700/40 bg-rose-500/10 px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider text-rose-300"
            title="Scheduler status endpoint returned an error"
          >
            err
          </span>
        )}
      </header>
      <ul className="flex flex-col gap-1">
        {rows.map((r) => {
          const status = statusLabel(r.enabled, r.running);
          const tip =
            r.errorHint
              ? `${r.label} · ${status} · ${r.detail} · error: ${r.errorHint}`
              : `${r.label} · ${status} · ${r.detail}`;
          return (
            <li
              key={r.key}
              className="flex items-center gap-2 rounded-md border border-slate-800 bg-slate-950/50 px-2 py-1.5"
              title={tip}
            >
              <span
                className={clsx('text-base leading-none', dotClass(r.enabled, r.running))}
                aria-hidden="true"
              >
                ●
              </span>
              {r.icon}
              <span className="flex-1 truncate text-[10px] uppercase tracking-wider text-slate-300">
                {r.label}
              </span>
              <span className="truncate font-mono text-[9px] text-slate-500">{r.detail}</span>
            </li>
          );
        })}
      </ul>
    </aside>
  );
}
