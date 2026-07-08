'use client';

import { useCallback } from 'react';
import clsx from 'clsx';
import WeightingPicker from './WeightingPicker';

export type OverlayMode = 'overlaid' | 'combined';

// P11-F3-05 — date-range chips on the charts header.
export type DateRangeKey = '1W' | '1M' | '3M' | '6M' | '1Y' | '5Y' | 'ALL' | 'CUSTOM';

// NOTE: CUSTOM is intentionally excluded until explicit start/end date pickers ship.
const DATE_RANGE_OPTIONS: DateRangeKey[] = ['1W', '1M', '3M', '6M', '1Y', '5Y', 'ALL'];

type Props = {
  mode: OverlayMode;
  onModeChange: (m: OverlayMode) => void;
  method: string;
  onMethodChange: (m: string) => void;
  selectedCount: number;
  /**
   * P11-F3-05 — selected date-range chip. Optional so existing callers
   * continue to render the toolbar without a date-range prop and the chips
   * stay in a "no current range" state until they wire one up.
   */
  range?: DateRangeKey;
  onRangeChange?: (r: DateRangeKey) => void;
};

/**
 * Toolbar above the Performance Charts row on the Backtest Panel.
 * - Mode switch: ALL (overlaid 16 series) vs COMBINED (single weighted curve)
 * - Weighting method dropdown (only relevant in COMBINED mode)
 */
export default function ChartsToolbar({
  mode,
  onModeChange,
  method,
  onMethodChange,
  selectedCount,
  range,
  onRangeChange,
}: Props) {
  // P11-F3-05 — chip click. Logs to console when no parent handler is wired
  // so the operator can confirm the chip is firing in DevTools while we
  // build out the date-range plumbing on the charts themselves.
  const handleRange = useCallback(
    (r: DateRangeKey) => {
      if (onRangeChange) onRangeChange(r);
      else if (
        process.env.NODE_ENV !== 'production' &&
        typeof console !== 'undefined'
      ) {
        console.debug(
          '[ChartsToolbar] range chip fired but no onRangeChange handler is wired:',
          r,
        );
      }
    },
    [onRangeChange],
  );
  return (
    <>
      {/* P11-F3-05 — date-range chip row sits above the main toolbar so the
          range selector is always visible regardless of mode. */}
      <div className="flex flex-wrap items-center gap-1 border-b border-slate-800 bg-slate-950/20 px-3 py-1.5">
        <span className="mr-1 text-[10px] uppercase tracking-widest text-slate-500">
          Range
        </span>
        {DATE_RANGE_OPTIONS.map((r) => {
          const active = r === range;
          return (
            <button
              key={r}
              type="button"
              onClick={() => handleRange(r)}
              className={clsx(
                'rounded-md border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider transition',
                active
                  ? 'border-cyan-700 bg-cyan-500/15 text-cyan-200'
                  : 'border-slate-700 bg-slate-900 text-slate-400 hover:text-slate-200',
              )}
            >
              {r}
            </button>
          );
        })}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 bg-slate-950/30 px-3 py-2">
        <div className="flex items-center gap-2">
          <span className="text-[10px] uppercase tracking-widest text-slate-500">
            Performance Charts
          </span>
          <span className="text-[10px] text-slate-600">·</span>
          <ModeButton
            active={mode === 'overlaid'}
            onClick={() => onModeChange('overlaid')}
          >
            ALL
          </ModeButton>
          <ModeButton
            active={mode === 'combined'}
            onClick={() => onModeChange('combined')}
          >
            COMBINED
          </ModeButton>
          <span className="text-[10px] text-slate-600">
            · {selectedCount} selected
          </span>
        </div>
        <WeightingPicker method={method} onChange={onMethodChange} displayMode="chips" />
      </div>
    </>
  );
}

function ModeButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={clsx(
        'rounded-md border px-2 py-1 text-[10px] font-bold uppercase tracking-wider transition',
        active
          ? 'border-cyan-700 bg-cyan-500/15 text-cyan-200'
          : 'border-slate-700 bg-slate-900 text-slate-400 hover:text-slate-200',
      )}
    >
      {children}
    </button>
  );
}
