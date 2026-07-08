'use client';

import { useParams } from 'next/navigation';
import StrategyDetail from '@/components/strategy/StrategyDetail';

/**
 * P1 strategy detail route.
 *
 * Renders the left-markdown / right-4tabs layout via StrategyDetail.
 */
export default function StrategyDetailPage() {
  const params = useParams<{ id: string }>();
  const idNum = Number(params?.id);
  const strategyId = Number.isInteger(idNum) && idNum > 0 ? idNum : null;

  if (strategyId == null) {
    return (
      <div className="flex h-full items-center justify-center text-xs text-slate-500">
        Invalid strategy id.
      </div>
    );
  }

  return <StrategyDetail strategyId={strategyId} />;
}
