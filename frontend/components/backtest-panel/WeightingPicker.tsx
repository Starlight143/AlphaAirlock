'use client';

import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import { Check, ChevronDown, Scale } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { api, type WeightingMethod } from '@/lib/api';
import { queryKeys } from '@/lib/query';

type Props = {
  method: string;
  onChange: (method: string) => void;
  /**
   * P11-F3-04 — switches the picker between the existing dropdown UI
   * (default) and an inline chip cluster used by ChartsToolbar's
   * COMBINED mode header. Chip mode surfaces the 7 most-used methods as
   * single-click chips and pushes the long-tail methods into a "More"
   * dropdown so the user never has to hunt them down.
   */
  displayMode?: 'dropdown' | 'chips';
};

// P8-FIX/H-12 — the three "muscle-memory" weighting methods. Operator can
// flip them with one click without opening the full dropdown.
const QUICK_METHODS: { key: string; label: string; sub: string }[] = [
  { key: 'equal_weight', label: 'Even (1/N)', sub: 'EW' },
  { key: 'risk_parity', label: 'Risk Parity', sub: 'ERC' },
  { key: 'inverse_vol', label: 'Inverse Vol', sub: 'IV' },
];

// P11-F3-04 — chip mode surfaces these in-order when the corresponding key
// exists in the backend method list. Anything else falls into the "More"
// overflow dropdown so we never silently hide a registered method.
const CHIP_METHOD_KEYS: string[] = [
  'equal_weight',
  'risk_parity',
  'mean_variance',
  'use_target_etl',
  'half_kelly',
  'min_variance',
  'cvar',
];

/**
 * Dropdown picker for the 11 weighting methods (8 reference + 3 P6-M16 BETA).
 * Pulls the canonical list from /api/portfolio/methods so the UI never drifts
 * from the backend registry.
 *
 * P8-FIX/H-12: Adds a QuickToggle row above the dropdown so the three
 * most-used methods are one click away.
 */
export default function WeightingPicker({ method, onChange, displayMode = 'dropdown' }: Props) {
  const q = useQuery({
    queryKey: queryKeys.portfolioMethods,
    queryFn: api.portfolioMethods,
    staleTime: Infinity,
  });
  const methods: WeightingMethod[] = q.data?.methods ?? [];
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  const active = methods.find((m) => m.key === method) ?? methods[0] ?? null;

  // Close on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  // C-M3 — when the backend method registry doesn't contain the currently
  // selected method (e.g. stale value in parent state after a backend
  // upgrade), surface the fallback to the parent so the dropdown label and
  // chip highlight stay in sync.
  useEffect(() => {
    if (!methods.length) return;
    const known = methods.some((m) => m.key === method);
    if (!known && active && active.key !== method) {
      onChange(active.key);
    }
  }, [methods, method, active, onChange]);

  // ---- P11-F3-04 chip mode ----------------------------------------------
  if (displayMode === 'chips') {
    const byKey = new Map(methods.map((m) => [m.key, m] as const));
    const chipMethods: WeightingMethod[] = CHIP_METHOD_KEYS.map((k) => byKey.get(k)).filter(
      (m): m is WeightingMethod => !!m,
    );
    const overflowMethods: WeightingMethod[] = methods.filter(
      (m) => !CHIP_METHOD_KEYS.includes(m.key),
    );
    const activeKey = method;
    return (
      <div ref={ref} className="flex items-center gap-1">
        {chipMethods.map((m) => {
          const sel = m.key === activeKey;
          return (
            <button
              key={m.key}
              type="button"
              onClick={() => onChange(m.key)}
              title={`${m.label} · ${m.key}`}
              className={clsx(
                'rounded-md border px-2 py-1 text-[10px] font-bold uppercase tracking-wider transition',
                sel
                  ? 'border-cyan-700 bg-cyan-500/15 text-cyan-200 ring-1 ring-cyan-500/40'
                  : 'border-slate-700 bg-slate-900 text-slate-400 hover:text-slate-200',
              )}
            >
              {m.label}
            </button>
          );
        })}
        {overflowMethods.length > 0 && (
          <div className="relative">
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              className={clsx(
                'flex items-center gap-1 rounded-md border px-2 py-1 text-[10px] font-bold uppercase tracking-wider transition',
                overflowMethods.some((m) => m.key === activeKey)
                  ? 'border-cyan-700 bg-cyan-500/15 text-cyan-200 ring-1 ring-cyan-500/40'
                  : 'border-slate-700 bg-slate-900 text-slate-400 hover:text-slate-200',
              )}
            >
              <Scale className="h-3 w-3" />
              More
              <ChevronDown className="h-3 w-3 opacity-60" />
            </button>
            {open && (
              <ul className="absolute right-0 z-30 mt-1 w-56 rounded-md border border-slate-700 bg-slate-950/95 p-1 shadow-2xl backdrop-blur">
                {overflowMethods.map((m) => {
                  const sel = m.key === activeKey;
                  return (
                    <li key={m.key}>
                      <button
                        type="button"
                        onClick={() => {
                          onChange(m.key);
                          setOpen(false);
                        }}
                        className={clsx(
                          'flex w-full items-center gap-2 rounded px-2 py-1 text-left text-[11px]',
                          sel
                            ? 'bg-cyan-500/15 text-cyan-200'
                            : 'text-slate-300 hover:bg-slate-900',
                        )}
                      >
                        {sel ? <Check className="h-3 w-3 text-cyan-300" /> : <span className="h-3 w-3" />}
                        <span className="flex-1">{m.label}</span>
                        <span className="text-[9px] text-slate-600">{m.key}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        )}
      </div>
    );
  }
  // ---- end chip mode ----------------------------------------------------

  return (
    <div ref={ref} className="flex flex-col items-end gap-1.5">
      <div className="flex items-center gap-1">
        {QUICK_METHODS.map((m) => {
          const sel = m.key === method;
          return (
            <button
              key={m.key}
              type="button"
              onClick={() => onChange(m.key)}
              title={`${m.label} · ${m.sub}`}
              className={clsx(
                'inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[10px] font-bold uppercase tracking-wider transition',
                sel
                  ? 'border-cyan-700 bg-cyan-500/15 text-cyan-200 ring-1 ring-cyan-500/40'
                  : 'border-slate-700 bg-slate-900 text-slate-400 hover:text-slate-200',
              )}
            >
              <span>{m.label}</span>
              {/* C-L1 — surface the .sub field (EW / ERC / IV) in the quick
                 button instead of letting it sit unrendered in the
                 QUICK_METHODS table. Operators recognise these shorthand
                 codes faster than the long label. */}
              <span className={clsx('font-mono text-[9px] tracking-normal', sel ? 'text-cyan-300/80' : 'text-slate-600')}>
                {m.sub}
              </span>
            </button>
          );
        })}
        <div className="relative">
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="flex items-center gap-2 rounded-md border border-cyan-700/60 bg-cyan-500/10 px-3 py-1 text-[11px] font-bold uppercase tracking-wider text-cyan-200 hover:bg-cyan-500/20"
          >
            <Scale className="h-3.5 w-3.5" />
            {active?.label ?? 'Choose method'}
            <ChevronDown className="h-3 w-3 opacity-60" />
          </button>
          {open && methods.length > 0 && (
            <ul className="absolute right-0 z-30 mt-1 w-56 rounded-md border border-slate-700 bg-slate-950/95 p-1 shadow-2xl backdrop-blur">
              {methods.map((m) => {
                const sel = m.key === method;
                return (
                  <li key={m.key}>
                    <button
                      type="button"
                      onClick={() => {
                        onChange(m.key);
                        setOpen(false);
                      }}
                      className={clsx(
                        'flex w-full items-center gap-2 rounded px-2 py-1 text-left text-[11px]',
                        sel
                          ? 'bg-cyan-500/15 text-cyan-200'
                          : 'text-slate-300 hover:bg-slate-900',
                      )}
                    >
                      {sel ? <Check className="h-3 w-3 text-cyan-300" /> : <span className="h-3 w-3" />}
                      <span className="flex-1">{m.label}</span>
                      <span className="text-[9px] text-slate-600">{m.key}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
