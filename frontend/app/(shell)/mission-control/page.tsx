import AgentTeam from '@/components/mission-control/AgentTeam';
import PipelinePills from '@/components/mission-control/PipelinePills';
import DaemonLog from '@/components/mission-control/DaemonLog';
import KpiRail from '@/components/mission-control/KpiRail';
import AlertChannels from '@/components/mission-control/AlertChannels';
import SystemHealth from '@/components/mission-control/SystemHealth';

/**
 * Mission Control homepage — reference layout.
 *
 *  ┌────────────┬──────────────────────────────────────┐
 *  │  KPI rail  │ Row 1: AGENT TEAM (persona cards)    │
 *  │            ├──────────────────────────────────────┤
 *  │            │ Row 2: STRATEGY PIPELINE (8 buckets) │
 *  ├────────────┼──────────────────────────────────────┤
 *  │ Alert chan.│ Row 3: RESEARCH DAEMON LIVE STREAM   │
 *  └────────────┴──────────────────────────────────────┘
 *
 * No vertical scroll on the main grid — each row owns its own overflow.
 * P19 F3 — left column is now a flex column hosting KpiRail (flex-1) and
 * AlertChannels (auto height) so KpiRail keeps its dense 12-tile layout
 * and the alert rail anchors at the bottom of the rail without forcing a
 * new grid row that would push DaemonLog off-screen.
 */
export default function MissionControlPage() {
  return (
    <div className="grid h-full w-full grid-cols-12 grid-rows-[260px_160px_1fr] gap-3 overflow-hidden p-3">
      <div className="col-span-3 row-span-3 flex min-h-0 flex-col gap-3 overflow-hidden">
        <div className="min-h-0 flex-1">
          <KpiRail />
        </div>
        <SystemHealth />
        <AlertChannels />
      </div>
      <div className="col-span-9 row-span-1">
        <AgentTeam />
      </div>
      <div className="col-span-9 row-span-1">
        <PipelinePills />
      </div>
      <div className="col-span-9 row-span-1 min-h-0">
        <DaemonLog />
      </div>
    </div>
  );
}
