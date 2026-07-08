'use client';

/**
 * P-SYSHEALTH — Mission Control "System Health" rail.
 *
 * Mirrors the reference walkthrough's dependency / config checklist (00:45):
 * runtime, key Python packages, config files, LLM, database, and bot wiring.
 * Data comes from GET /api/system/checklist — a read-only, side-effect-free
 * endpoint (importlib.util.find_spec / file-exists / env probes; no heavy
 * imports). Compact and internally scrollable so it never breaks the
 * no-vertical-scroll main grid.
 *
 * Honest tri-state colouring (matches AlertChannels' philosophy):
 *   ok   = emerald   warn = amber   fail = rose   off = slate (neutral)
 * A default-OFF bot is shown as a neutral "off", never an alarming red.
 */

import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import { api, type SystemCheckItem } from '@/lib/api';

function dotClass(state: SystemCheckItem['state']): string {
  switch (state) {
    case 'ok':
      return 'text-emerald-400';
    case 'warn':
      return 'text-amber-400';
    case 'fail':
      return 'text-rose-400';
    default:
      return 'text-slate-600'; // 'off'
  }
}

export default function SystemHealth(): JSX.Element {
  const q = useQuery({
    queryKey: ['system', 'checklist'],
    queryFn: api.systemChecklist,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
  const items = q.data?.items ?? [];
  // Preserve backend group order without a Set (keeps the runtime → packages →
  // config → llm → database → bots ordering the endpoint emits).
  const groups: string[] = [];
  for (const it of items) if (!groups.includes(it.group)) groups.push(it.group);

  return (
    <aside className="flex max-h-[210px] shrink-0 flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/50 p-2">
      <header className="mb-1.5 flex items-center justify-between">
        <h2 className="text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">
          System Health
        </h2>
        {q.data && (
          <span
            className={clsx(
              'rounded border px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider',
              q.data.fail_count > 0
                ? 'border-rose-700/40 bg-rose-500/10 text-rose-300'
                : 'border-emerald-700/40 bg-emerald-500/10 text-emerald-300',
            )}
            title={`${q.data.ok_count}/${q.data.total} OK · ${q.data.fail_count} failing`}
          >
            {q.data.ok_count}/{q.data.total}
          </span>
        )}
      </header>
      {q.isLoading && <div className="px-1 py-1 text-[10px] text-slate-600">Checking…</div>}
      {q.isError && (
        <div className="px-1 py-1 text-[10px] text-rose-400">Checklist unavailable.</div>
      )}
      <div className="flex flex-col gap-1.5 overflow-y-auto pr-0.5">
        {groups.map((g) => (
          <div key={g}>
            <div className="mb-0.5 text-[8px] font-bold uppercase tracking-widest text-slate-600">
              {g}
            </div>
            <ul className="flex flex-col gap-0.5">
              {items
                .filter((it) => it.group === g)
                .map((it) => (
                  <li
                    key={`${g}:${it.name}`}
                    className="flex items-center gap-1.5 rounded border border-slate-800/70 bg-slate-950/40 px-1.5 py-0.5"
                    title={`${it.name} · ${it.state} · ${it.detail}`}
                  >
                    <span
                      className={clsx('text-[10px] leading-none', dotClass(it.state))}
                      aria-hidden="true"
                    >
                      ●
                    </span>
                    <span className="flex-1 truncate text-[10px] text-slate-300">{it.name}</span>
                    <span className="truncate font-mono text-[8px] text-slate-500">{it.detail}</span>
                  </li>
                ))}
            </ul>
          </div>
        ))}
      </div>
    </aside>
  );
}
