// P17/A-H5 — Centralised semantic stage labels for alpha strategies.
//
// The numeric stage (0..6) is paired with a human-readable "concept" label that
// reflects what the operator is actually doing at that point in the pipeline.
// Surfaces:
//   - StrategyDetail header (StageBadge)
//   - StrategyTable Stage column (concatenated with numeric pill)
//   - alpha-flow Timeline events
//
// Tones are Tailwind class fragments grouped per stage so each surface can pick
// border / bg / text consistently. `stageToneClass(stage)` returns the joined
// className string for callers that want one-line styling.

export type StageNumber = 0 | 1 | 2 | 3 | 4 | 5 | 6;

export const STAGE_LABELS: Record<number, string> = {
  0: 'Alpha Idea',
  1: 'Story Drafted',
  2: 'Code Generated',
  3: 'Backtest in Loop',
  4: 'Approved / Paper',
  5: 'Live Trade',
  6: 'Graveyard',
};

export const STAGE_TONES: Record<
  number,
  { border: string; bg: string; text: string }
> = {
  0: { border: 'border-cyan-700/40',    bg: 'bg-cyan-500/10',    text: 'text-cyan-300' },
  1: { border: 'border-emerald-700/40', bg: 'bg-emerald-500/10', text: 'text-emerald-300' },
  2: { border: 'border-purple-700/40',  bg: 'bg-purple-500/10',  text: 'text-purple-300' },
  3: { border: 'border-blue-700/40',    bg: 'bg-blue-500/10',    text: 'text-blue-300' },
  4: { border: 'border-amber-700/40',   bg: 'bg-amber-500/10',   text: 'text-amber-300' },
  5: { border: 'border-emerald-700/60', bg: 'bg-emerald-500/15', text: 'text-emerald-300' },
  6: { border: 'border-slate-700',      bg: 'bg-slate-900',      text: 'text-slate-400' },
};

/**
 * Return the semantic label for a stage number. Falls back to "Stage N" when
 * the value is outside the documented 0..6 range so unknown future stages
 * still render something meaningful instead of "undefined".
 */
export function stageLabel(stage: number | null | undefined): string {
  if (stage == null || !Number.isFinite(Number(stage))) return 'Unknown';
  const n = Math.trunc(Number(stage));
  return STAGE_LABELS[n] ?? `Stage ${n}`;
}

/**
 * Return a Tailwind className string ("border-... bg-... text-...") for the
 * given stage. Unknown stages fall through to the Graveyard (stage 6) tone.
 */
export function stageToneClass(stage: number | null | undefined): string {
  if (stage == null || !Number.isFinite(Number(stage))) {
    const t = STAGE_TONES[6];
    return `${t.border} ${t.bg} ${t.text}`;
  }
  const n = Math.trunc(Number(stage));
  const t = STAGE_TONES[n] ?? STAGE_TONES[6];
  return `${t.border} ${t.bg} ${t.text}`;
}
