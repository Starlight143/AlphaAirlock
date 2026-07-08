// P13/B-L2 — shared p-value → edge-colour helper, extracted from the
// near-identical implementations in `lib/cointegration.ts` and
// `components/factor-network/FactorGraph.tsx`. Both call sites used the same
// tier boundaries (<0.01 / <0.025 / weak) but disagreed on the fallback
// colour for p ≥ 0.05 — the cointegration scan never returns those edges
// (the operator's `p_threshold` filters them server-side) and the factor
// network treated them as "not statistically meaningful" with a slate dim.
//
// The shared helper keeps the tier boundaries identical but lets callers
// pick a fallback so we don't quietly regress the factor network's dim
// styling when re-using this module.

/**
 * Map a p-value to an edge colour:
 *   p < 0.01   → rose-500   (strong)
 *   p < 0.025  → amber-500  (medium)
 *   p < 0.05   → cyan-400   (weak — still statistically meaningful)
 *   otherwise  → `defaultColor` (caller-controlled; defaults to cyan-400)
 *
 * Non-finite or negative p-values clamp to `defaultColor` so the legend
 * never shows an unstyled or black edge.
 */
export function pValueEdgeColor(p: number, defaultColor: string = '#22d3ee'): string {
  if (!Number.isFinite(p) || p < 0) {
    if (process.env.NODE_ENV !== 'production') {
      // eslint-disable-next-line no-console
      console.warn('[pValueEdgeColor] non-finite or negative p-value:', p);
    }
    return defaultColor;
  }
  if (p < 0.01) return '#f43f5e';
  if (p < 0.025) return '#f59e0b';
  if (p < 0.05) return '#22d3ee';
  return defaultColor;
}

/**
 * P16/B-H3 — same tier ladder as `pValueEdgeColor` but the weak / fallback
 * tier (p ≥ 0.05) is rendered with a lighter cyan so it visually matches
 * the cointegration legend's "0.05 ≤ p < threshold" swatch
 * (`bg-cyan-400/70`). Without this distinction, edges with p∈[0.05, 0.1)
 * render identical to edges with p∈[0.025, 0.05), making the operator
 * unable to tell strong-tier and weak-tier pairs apart at a glance.
 *
 * The weak colour is configurable so callers can match other legend
 * swatches; the default mirrors the Tailwind `cyan-400/70` rgba value.
 */
export function pValueEdgeColorWithWeak(
  p: number,
  weakColor: string = 'rgba(34, 211, 238, 0.7)',
): string {
  if (!Number.isFinite(p) || p < 0) {
    if (process.env.NODE_ENV !== 'production') {
      // eslint-disable-next-line no-console
      console.warn('[pValueEdgeColorWithWeak] non-finite or negative p-value:', p);
    }
    return weakColor;
  }
  if (p < 0.01) return '#f43f5e';
  if (p < 0.025) return '#f59e0b';
  if (p < 0.05) return '#22d3ee';
  return weakColor;
}
