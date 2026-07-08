'use client';

import clsx from 'clsx';
import type { IngestSource } from '@/lib/api';
import { CATEGORY_LABELS, SOURCE_CATEGORIES, type SourceCategory } from '@/lib/sourceTypes';

type Props = {
  sources: IngestSource[];
  active: SourceCategory | 'all';
  onChange: (next: SourceCategory | 'all') => void;
};

/**
 * Top categorical tabs on /sources (Apps / YouTube / DPS / ... / AI), plus
 * an "All" pseudo-tab. Counts reflect the live sources payload.
 */
export default function SourcesTopTabs({ sources, active, onChange }: Props) {
  const counts: Record<string, number> = { all: sources.length };
  for (const s of sources) {
    const cat = s.category || 'uncategorised';
    counts[cat] = (counts[cat] || 0) + 1;
  }

  // P13 A-L2 — use the fixed declaration order from SOURCE_CATEGORIES
  // (lib/sourceTypes.ts). Tabs no longer reflow when counts change, so
  // operators build muscle-memory for "the 4th tab is always Invoice".
  const ordered = [...SOURCE_CATEGORIES];

  return (
    <div role="tablist" className="flex flex-wrap gap-1 overflow-x-auto border-b border-slate-800 bg-slate-950/30 px-3 py-2">
      <Tab
        active={active === 'all'}
        onClick={() => onChange('all')}
        count={sources.length}
      >
        All Sources
      </Tab>
      {ordered.map((cat) => (
        <Tab
          key={cat}
          active={active === cat}
          onClick={() => onChange(cat)}
          count={counts[cat] || 0}
        >
          {CATEGORY_LABELS[cat]}
        </Tab>
      ))}
    </div>
  );
}

function Tab({
  active,
  onClick,
  count,
  children,
}: {
  active: boolean;
  onClick: () => void;
  count: number;
  children: React.ReactNode;
}) {
  const disabled = count === 0 && !active;
  return (
    <button
      role="tab"
      aria-selected={active}
      onClick={onClick}
      disabled={disabled}
      className={clsx(
        'rounded-md border px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider transition',
        active
          ? 'border-cyan-700 bg-cyan-500/15 text-cyan-200'
          : disabled
          ? 'cursor-not-allowed border-slate-900 bg-slate-950 text-slate-700'
          : 'border-slate-700 bg-slate-900 text-slate-400 hover:text-slate-200',
      )}
    >
      {children}
      {/* P12-B-L1 — only render the [N] badge when N > 0 so empty categories
          don't carry a noisy "[0]" suffix that reads like a broken filter. */}
      {count > 0 && (
        <span className={clsx('ml-1', active ? 'text-cyan-400' : 'text-slate-600')}>
          [{count}]
        </span>
      )}
    </button>
  );
}
