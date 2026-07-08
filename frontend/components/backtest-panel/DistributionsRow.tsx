'use client';

import type { EquityPoint, TradeTapeRow } from '@/lib/api';
import DailyPnlDistribution from '@/components/charts/DailyPnlDistribution';
import PositionDistribution from '@/components/charts/PositionDistribution';
import RollingSharpe from '@/components/charts/RollingSharpe';

type Props = {
  equity: EquityPoint[];
  trades: TradeTapeRow[];
  rollingWindow?: number;
  rollingWindowSelector?: boolean;
};

/**
 * P17/M7 — three-up distribution row used on the Performance tab so the
 * operator can compare Rolling Sharpe, Daily PnL distribution, and Position
 * distribution side-by-side without scrolling.
 *
 * Each child renders its own ChartShell and finite-guards; we only own the
 * grid layout. SSR-safe: no window/document references.
 */
export default function DistributionsRow({
  equity,
  trades,
  rollingWindow = 30,
  rollingWindowSelector = true,
}: Props) {
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
      <RollingSharpe
        data={equity}
        window={rollingWindow}
        windowSelector={rollingWindowSelector}
      />
      <DailyPnlDistribution data={equity} />
      <PositionDistribution data={trades} />
    </div>
  );
}
