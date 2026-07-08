// P8-FIX/F6 — visual helpers for the live Engle-Granger pair graph.
//
// The wire shape (`CointegrationResponse` / `CointegrationPair`) lives in
// `lib/api.ts`. This module owns the pure-presentation helpers that the page
// + visualisation component share, kept separate so they can be unit-tested
// without pulling in the vis-network DOM dependency.

// P13/B-L2 — the underlying p-value → colour mapping moved to
// `lib/pValueColor.ts` so the factor network's Granger overlay can share
// the same tier boundaries. This wrapper preserves the cointegration-
// specific fallback (cyan), and keeps the original symbol name to avoid
// churning consumers.
//
// P16/B-H3 — swapped from `pValueEdgeColor` to `pValueEdgeColorWithWeak`
// so the weak-tier bucket (0.05 ≤ p < p_threshold) renders with the
// lighter cyan that matches the legend swatch (`bg-cyan-400/70`). The
// previous `pValueEdgeColor` returned solid `#22d3ee` for every p ≥ 0.05,
// which made strong-tier and weak-tier edges visually indistinguishable
// whenever the operator picked `p_threshold > 0.05`.

import { pValueEdgeColorWithWeak } from './pValueColor';

/**
 * Map a p-value to the constellation edge colour used by `CointegrationField`.
 * Cointegration scans only return pairs under the operator's chosen
 * `p_threshold`. For pairs in the weak-tier bucket (0.05 ≤ p < threshold)
 * we return a lighter cyan so the legend's `cyan-400/70` swatch and the
 * rendered edge line up visually.
 */
export function edgeColorForPValue(p: number): string {
  return pValueEdgeColorWithWeak(p);
}
