// P16/B-L9 + B-H4 — shared IC colour ladder.
//
// Several factor / explorer surfaces had inlined their own ad-hoc IC
// colour buckets, and most of them were asymmetric — a 4-bucket positive
// ladder (emerald / cyan / slate / zero-grey) paired with a single
// catch-all rose for negatives. That collapsed strong inverse-signal
// factors (IC=-0.42) into the same colour as noise (IC=-0.01), which is
// visually misleading because operators explicitly hunt for high
// |IC| factors regardless of sign (mean-reversion / inverted signals
// are valuable).
//
// This module mirrors the positive ladder on the negative side so the
// colour intensity tracks |IC| symmetrically.

/**
 * Map an IC score to a Tailwind text-colour class. Symmetric ladder:
 *
 *   ic >=  0.10  → emerald (strong positive)
 *   ic >=  0.05  → cyan    (medium positive)
 *   ic >   0     → slate-200 (weak positive)
 *   ic === 0     → slate-500 (zero — noise)
 *   ic >  -0.05  → rose/60   (weak negative)
 *   ic >  -0.10  → rose/80   (medium negative)
 *   ic <= -0.10  → rose      (strong negative)
 *   non-finite / null / undefined → slate-500 (unknown)
 */
export function icColorClass(ic: number | null | undefined): string {
  if (ic == null || !Number.isFinite(ic)) return 'text-slate-500';
  if (ic >= 0.10) return 'text-emerald-300';
  if (ic >= 0.05) return 'text-cyan-200';
  if (Math.abs(ic) < 1e-9) return 'text-slate-500';
  if (ic > 0) return 'text-slate-200';
  if (ic > -0.05) return 'text-rose-300/60';
  if (ic > -0.10) return 'text-rose-300/80';
  return 'text-rose-300';
}
