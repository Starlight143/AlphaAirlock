'use client';

import { SOURCE_TYPE_LABELS } from '@/lib/sourceTypes';
import type { SourceType } from '@/lib/api';

type Props = {
  value: SourceType | 'all';
  onChange: (v: SourceType | 'all') => void;
  supported: SourceType[];
};

/**
 * Source-type dropdown filter (RSS / Patreon / Medium / ...). Sits next to
 * the +ADD SOURCE button on /sources.
 */
export default function SourceTypeFilter({ value, onChange, supported }: Props) {
  return (
    <label className="flex items-center gap-2 text-[10px] uppercase tracking-widest text-slate-400">
      Filter:
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as SourceType | 'all')}
        className="rounded-md border border-slate-700 bg-slate-950 px-2 py-1 text-[11px] text-slate-200 outline-none focus:border-cyan-600"
      >
        {/* P13 A-M3 — relabelled from "All Types" to a neutral placeholder
            so the dropdown matches the reference UI (screenshot 20-05-20),
            which has no synthetic top option. Functionality preserved: the
            `'all'` state still disables the filter in /sources page.tsx. */}
        <option value="all">— filter by type —</option>
        {supported.map((t) => (
          <option key={t} value={t}>
            {SOURCE_TYPE_LABELS[t] ?? t}
          </option>
        ))}
      </select>
    </label>
  );
}
