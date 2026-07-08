// Centralized TanStack Query keys + a shared QueryClient factory.
// One factory per browser session — never call `new QueryClient()` outside this
// module, otherwise cross-route caching breaks.

import { QueryClient } from '@tanstack/react-query';

export const queryKeys = {
  health: ['health'] as const,
  strategies: ['strategies'] as const,
  strategy: (id: number) => ['strategy', id] as const,
  graph: ['graph'] as const,
  knowledge: ['knowledge'] as const,
  knowledgeStats: ['knowledge-stats'] as const,
  costSummary: ['cost-summary'] as const,
  knowledgeOne: (id: number) => ['knowledge', id] as const,
  agents: ['agents'] as const,
  pipelineBuckets: ['pipeline-buckets'] as const,
  pipelineStatus: (id: number) => ['pipeline-status', id] as const,
  daemonLog: (limit: number) => ['daemon-log', limit] as const,
  // P2
  portfolioMethods: ['portfolio-methods'] as const,
  portfolioCombine: (ids: number[], method: string) =>
    ['portfolio-combine', method, ids.slice().sort().join(',')] as const,
  gateCriteria: ['gate-criteria'] as const,
  // P3
  sources: ['sources'] as const,
  sourceEvents: (id: number) => ['source-events', id] as const,
  schedulerStatus: ['scheduler-status'] as const,
  kbCategories: ['kb-categories'] as const,
  kbByCategory: (cat: string, limit = 100) => ['kb-by-category', cat, limit] as const,
  factorNetwork: ['factor-network'] as const,
  // P4
  chatSessions: ['chat-sessions'] as const,
  chatSessionDetail: (id: number) => ['chat-session', id] as const,
  paperTradeList: ['paper-trade-list'] as const,
  paperTradeDetail: (id: number) => ['paper-trade-detail', id] as const,
  // P-SIM forward sim account
  simAccountList: ['sim-account-list'] as const,
  simAccountDetail: (id: number) => ['sim-account-detail', id] as const,
  // P7
  paThroughput: (d: number, b: string) => ['pa-throughput', d, b] as const,
  paTimeInStage: (d: number) => ['pa-time-in-stage', d] as const,
  paGatePass: (d: number, w: number) => ['pa-gate-pass', d, w] as const,
  paOccupancy: ['pa-occupancy'] as const,
  missionPanelSnapshot: ['mission-panel-snapshot'] as const,
  missionPanelIncidents: (h: number, l: number) => ['mp-incidents', h, l] as const,
  blParams: ['bl-params'] as const,
  blSweep: (id: number) => ['bl-sweep', id] as const,
  blSweeps: (sid: number) => ['bl-sweeps', sid] as const,
  alphaFlowSankey: (d: number) => ['af-sankey', d] as const,
  alphaFlowTimeline: (id: number) => ['af-timeline', id] as const,
  alphaFlowDropout: (d: number) => ['af-dropout', d] as const,
  irBook: (st: string, bm: string) => ['ir-book', st, bm] as const,
  irByCat: (st: string, bm: string) => ['ir-cat', st, bm] as const,
  irByRegime: (st: string, bm: string) => ['ir-regime', st, bm] as const,
  irByAsset: (st: string, bm: string) => ['ir-asset', st, bm] as const,
  irRolling: (st: string, bm: string, w: number) => ['ir-rolling', st, bm, w] as const,
  irWaterfall: (st: string, bm: string) => ['ir-waterfall', st, bm] as const,
  poCombine: (ids: number[], method: string, c: string) => ['po-combine', method, ids.slice().sort().join(','), c] as const,
  poSaved: ['po-saved'] as const,
  poFrontier: (ids: number[]) => ['po-frontier', ids.slice().sort().join(',')] as const,
  fsList: ['fs-list'] as const,
  fsOne: (id: number) => ['fs-one', id] as const,
  agForest: ['ag-forest'] as const,
  agTree: (id: number) => ['ag-tree', id] as const,
  agLineage: (id: number) => ['ag-lineage', id] as const,
  liveTradeDashboard: ['live-trade-dashboard'] as const,
  liveTradeAudit: (l: number) => ['lt-audit', l] as const,
  ttStatus: ['tt-status'] as const,
  ttSymbols: ['tt-symbols'] as const,
  ttMarket: (sym: string) => ['tt-market', sym] as const,
  ttPositions: (mode: string) => ['tt-positions', mode] as const,
  ttOrders: (status?: string, symbol?: string, mode?: string) => ['tt-orders', status ?? '', symbol ?? '', mode ?? ''] as const,
  // P8-FIX additions
  pipelineBucketsV2: ['pipeline-buckets-v2'] as const,
  autoPipelineStatus: ['auto-pipeline-status'] as const,
  knowledgePostmortems: (id: number) => ['knowledge-postmortems', id] as const,
  agentDialogue: (sid?: number) => ['agent-dialogue', sid ?? 'all'] as const,
  cointegrationPairs: (lookback: number, p: number) =>
    ['cointegration', lookback, p] as const,
  alphaLabSuggested: (limit: number) => ['alpha-lab-suggested', limit] as const,
  telegramRecentIntakes: (limit: number) => ['telegram-recent-intakes', limit] as const,
  discordStatus: ['discord-status'] as const,
  discordRecentIntakes: (limit: number) => ['discord-recent-intakes', limit] as const,
  sourceFiles: (id: number, limit = 200) => ['source-files', id, limit] as const,
  strategyTrades: (id: number, nonzeroOnly?: boolean) =>
    ['strategy-trades', id, nonzeroOnly ?? false] as const,
};

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // 5s stale time keeps cross-route navigation snappy without forcing
        // an immediate refetch on mount.
        staleTime: 5_000,
        // Disabled to avoid surprise refetches when alt-tabbing between the
        // dashboard and an IDE — the user can click "refresh" if needed.
        refetchOnWindowFocus: false,
        retry: 1,
      },
    },
  });
}
