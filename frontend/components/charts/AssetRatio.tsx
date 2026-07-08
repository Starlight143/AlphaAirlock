'use client';

import type { CombinedPortfolio } from '@/lib/api';
import CategoryDoughnut from './CategoryDoughnut';

type Props = {
  data: CombinedPortfolio;
  title?: string;
};

/**
 * Asset / strategy weight pie (P6-A3).
 *
 * Wraps the existing CategoryDoughnut with `variant="pie"` and the weight
 * payload from a CombinedPortfolio response. Filters out zero-weight legs so
 * the slice palette doesn't waste a colour on every strategy the allocator
 * decided to skip.
 *
 * Distinct from `<CategoryDoughnut slices={categoryMix}>`:
 *   - AssetRatio    : one slice per strategy (label = "S#N")
 *   - CategoryDoughnut: one slice per alpha_category (label = "vol / liquidation / ...")
 *
 * The reference screenshot shows both side-by-side because they answer
 * different questions ("which strategies got weight" vs "which alpha buckets").
 */
export default function AssetRatio({ data, title = 'Asset Ratio' }: Props) {
  const slices = Object.entries(data.weights ?? {})
    .map(([sid, w]) => ({ name: `S#${sid}`, value: Number(w) || 0 }))
    .filter((s) => s.value > 0)
    .sort((a, b) => b.value - a.value);
  return (
    <CategoryDoughnut slices={slices} title={title} variant="pie" valueUnit="fraction" />
  );
}
