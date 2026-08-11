// NO 'use client' directive — this module is pure fetch helpers and is safe to
// import from both server and client components. Adding 'use client' here would
// poison every consumer into client-only land.

export const API_BASE =
  (typeof process !== 'undefined' && process.env.NEXT_PUBLIC_API_BASE) ||
  'http://127.0.0.1:8000';

// P-FIX api-base-localhost-baked — NEXT_PUBLIC_* is inlined at build time, so a
// production image built without NEXT_PUBLIC_API_BASE silently ships the
// localhost fallback and every browser request targets the end-user's own
// 127.0.0.1:8000 (connection-refused). Surface a single, non-blocking console
// warning in production browser bundles when the fallback is still in effect.
// Dev behaviour is unchanged; SSR/server import is a no-op (typeof window guard);
// the exported API_BASE value is NOT modified.
if (
  typeof window !== 'undefined' &&
  typeof process !== 'undefined' &&
  process.env.NODE_ENV === 'production' &&
  API_BASE === 'http://127.0.0.1:8000' &&
  !process.env.NEXT_PUBLIC_API_BASE
) {
  const w = window as unknown as { __apiBaseWarned?: boolean };
  if (!w.__apiBaseWarned) {
    w.__apiBaseWarned = true;
    // eslint-disable-next-line no-console
    console.warn(
      '[api] NEXT_PUBLIC_API_BASE is not set; falling back to ' +
        'http://127.0.0.1:8000. In a deployed build this points at the ' +
        "browser's OWN localhost, not the server — all API requests will fail. " +
        'Rebuild the frontend with NEXT_PUBLIC_API_BASE set to the public API URL.',
    );
  }
}

// ---------------------------------------------------------------------------
// Domain types
// ---------------------------------------------------------------------------

export type NodeKind = 'concept' | 'past_alpha' | 'postmortem' | 'active';

export type KnowledgeNode = {
  id: number;
  title: string;
  content: string;
  tags: string[];
  links: number[];
  ic_score: number;
  kind: NodeKind;
  source_url?: string | null;
  source_type?: string | null;
  category?: string | null;
  content_hash?: string | null;
  origin_strategy_id?: number | null;
  // P6 — auto-pipeline / queue / IC-decay bookkeeping (was previously missing
  // from the TS type even though the backend `to_dict()` already emits them).
  auto_pipeline_strategy_id?: number | null;
  last_queue_eval_at?: string | null;
  ic_decayed_at?: string | null;
  ingested_at?: string | null;
  // D-L11/P16 — optional for symmetry with the other timestamp/audit fields;
  // legacy rows or older API versions may omit the key entirely rather than
  // sending an explicit `null`.
  created_at?: string | null;
};

export type AlphaStrategy = {
  id: number;
  // P17/H1 — canonical human-readable id ("ALPHA-XXXXXXXX"). Backfilled by migration.
  alpha_id?: string | null;
  slug?: string;
  name: string;
  stage: number;
  formula_code: string;
  config: Record<string, unknown>;
  metrics: Record<string, number>;
  team_b_review: string;
  status: string;
  updated_at: string | null;
  // D-M16/P16 — backend now emits `created_at` alongside `updated_at`.
  // Optional for backward compatibility with older API responses.
  created_at?: string | null;
  equity_curve?: EquityPoint[];
  // P8-FIX/H-13: raw_backtest now includes fee_per_side / slippage_per_side
  // so the strategy detail page can render a "costs included" chip without
  // a separate endpoint.
  raw_backtest?: {
    metrics?: Record<string, number>;
    trades?: number;
    per_bar_available?: boolean;
    fee_per_side?: number;
    slippage_per_side?: number;
    round_trip_cost?: number;
    bars?: number;
    annualization_factor?: number;
  };
};

// Per-bar trade tape row served by GET /api/strategies/{id}/trades (P5-BE-04).
// P8-FIX/H-11 extended with direction / mark_price / position_delta so the
// BACKTEST CSV tab can render coloured long/short chips and the position bar
// chart can be derived from the same payload.
export type TradeTapeRow = {
  start_time: string;
  signal: number;
  pnl_pct: number;
  cum_pnl_pct: number;
  drawdown_pct: number;
  direction?: 'long' | 'short' | 'flat';
  mark_price?: number;
  position_delta?: number;
};

export type TradeTapeResponse = {
  strategy_id: number;
  total: number;
  offset: number;
  limit: number;
  nonzero_only: boolean;
  rows: TradeTapeRow[];
};

export type EquityPoint = {
  timestamp: string;
  equity: number;
  drawdown: number;
};

export type GraphNode = {
  id: string;
  label: string;
  title: string;
  kind: 'knowledge' | 'strategy';
  node_kind?: NodeKind | 'strategy';
  stage: number;
  status: string;
  color: string;
  // P12-B-H4 — surfaced by /api/graph for the NodeInspector overlay so the
  // operator can triage a node without leaving the canvas. All optional so
  // older payloads still parse.
  category?: string | null;
  tags?: string[];
  ic_score?: number;
  source_url?: string | null;
  out_degree?: number;
};
export type GraphEdge = { from: string; to: string };

export type PipelineStatusPayload = {
  strategy_id: number;
  current_status: string;
  execution_time_seconds: number;
  active_agent: string;
  terminal_logs: string[];
  // D-L13/P16 — backend always emits this flag; True means the process was
  // restarted and execution_time_seconds is 0.0 (wall-clock unavailable).
  restored_from_db?: boolean;
};

export type LLMConfig = {
  requested: string;
  resolved: string | null;
  configured: boolean;
  model?: string;
  base_url?: string;
  key_env_var?: string;
  key_present?: boolean;
  supports_vision?: boolean;
  error?: string;
};

export type AgentPersona = {
  key: string;
  name: string;
  role: string;
  // P8-FIX/H-9: Team A (research/exec) vs Team B (critic) vs ops.
  team?: 'A' | 'B' | 'ops';
  color: string;
  description: string;
  capabilities: string[];
};

export type PipelineBucket = {
  index: number;
  key: string;
  label: string;
  statuses: string[];
  count: number;
};

// P8-FIX/C-4: V2 buckets merge SMALL_CAPITAL + LIVE into "Live Trade"
// (Stage 5) and expose sub_pills for the UI breakdown.
export type PipelineBucketV2 = PipelineBucket & {
  sub_pills?: { key: string; label: string; statuses: string[]; count: number }[];
};
export type PipelineBucketsV2Response = {
  schema_version: 2;
  buckets: PipelineBucketV2[];
  total: number;
};

// P8-FIX/H-? — F3 mission-control consumer for the auto-pipeline source hook.
// Mirrors backend/core/auto_pipeline.py::status_snapshot() exactly. Unknown
// extra keys are tolerated (Record<string, unknown> spread on .extras).
export type AutoPipelineTrigger = {
  ts?: string;
  source_id?: number;
  source_name?: string;
  node_ids?: number[];
  strategy_ids?: number[];
  status?: string;
  reason?: string;
  error?: string;
  [k: string]: unknown;
};
export type AutoPipelineStatus = {
  enabled: boolean;
  ic_threshold?: number;
  daily_quota_per_source?: number;
  batch_size?: number;
  concurrency_limit?: number;
  recent_triggers: AutoPipelineTrigger[];
  // The backend may add `dispatched_today` (and similar) in the future; we
  // surface them defensively to avoid `as any` casts at call sites.
  dispatched_today?: number;
};

// P8-FIX/H-7: agent self-dialogue transcript.
export type DialogueIntent =
  | 'question' | 'answer' | 'handoff' | 'critique' | 'approval' | 'veto' | 'note';
export type DialogueTurn = {
  // P16 A-M11 — strategy_id may be null for system-wide dialogue
  // entries that don't belong to a specific pipeline run. The
  // AgentDialogueResponse already typed `strategy_id: number | null`
  // but the individual turn was incorrectly narrowed to `number`,
  // which caused "S#null" to render on the buffer's null rows.
  strategy_id: number | null;
  turn: number;
  from_agent: string;
  to_agent: string;
  intent: DialogueIntent;
  payload: string;
  ts: string;
};
export type AgentDialogueResponse = {
  turns: DialogueTurn[];
  buffer_size: number;
  strategy_id: number | null;
  limit: number;
};

// P8-FIX/C-2: Engle-Granger cointegration pair scan.
export type CointegrationPair = {
  src: string;
  dst: string;
  p_value: number;
  beta: number;
  half_life_bars: number | null;
};
export type CointegrationResponse = {
  assets: string[];
  pairs: CointegrationPair[];
  method: 'engle_granger';
  lookback_days: number;
  p_threshold: number;
  computed_at: string;
  is_synthetic: boolean;
  pair_count: number;
  tested_pair_count: number;
  error?: string;
};

// P8-FIX/H-15: Alpha Lab Suggested Topics rail.
export type SuggestedTopic = {
  id: string;
  title: string;
  chip: 'queued' | 'in-progress' | 'proven' | string;
  source: 'knowledge_base' | 'seed_fallback';
  kb_node_id: number | null;
  ic_score: number;
};
export type SuggestedTopicsResponse = {
  topics: SuggestedTopic[];
  generated_at: string;
  limit: number;
};

// P8-FIX/H-2: postmortem feedback loop reverse-link.
export type KnowledgePostmortemsResponse = {
  node_id: number;
  strategy_ids: number[];
  postmortems: KnowledgeNode[];
};

// P8-FIX/H-17: telegram recent intakes.
export type TelegramRecentIntakesResponse = {
  items: KnowledgeNode[];
  total: number;
  limit: number;
};
export type DiscordRecentIntakesResponse = TelegramRecentIntakesResponse;

// P8-FIX/H-17 + C-5: scheduler status now includes telegram + discord blocks.
export type TelegramInboundStatus = {
  enabled: boolean;
  running: boolean;
  allowed_chats_count?: number;
  rate_interval_seconds?: number;
  last_update_id?: number;
  error?: string;
};
export type DiscordInboundStatus = {
  enabled: boolean;
  running: boolean;
  allowed_channels_count?: number;
  last_event_at?: string | null;
  error?: string;
};
export type SchedulerStatusResponse = {
  enabled: boolean;
  running: boolean;
  tick_seconds: number;
  telegram_inbound?: TelegramInboundStatus;
  discord_inbound?: DiscordInboundStatus;
};

// P8-FIX/C-5: /api/discord/status full snapshot.
export type DiscordStatusEvent = {
  ts: string;
  kind: string;
  channel?: number;
  node_id?: number;
  title?: string;
  error?: string;
  user?: string;
};
export type DiscordStatusResponse = {
  enabled: boolean;
  running: boolean;
  allowed_channels_count: number;
  last_event_at: string | null;
  recent_events: DiscordStatusEvent[];
  error?: string;
};

export type DaemonLogEvent = {
  strategy_id: number;
  status: string;
  agent: string;
  line: string;
};

// ---- P2 Backtest Panel ----

export type WeightingMethod = {
  key: string;
  label: string;
};

export type CombinedPortfolio = {
  method: string;
  method_label: string;
  weights: Record<string, number>;
  equity_curve: EquityPoint[];
  metrics: Record<string, number>;
  n_strategies: number;
  n_aligned_days: number;
  missing: number[];
};

export type GateCriteriaEditable = {
  // Operator-facing display value (e.g. -25 for MaxDD shown as "%").
  value: number;
  unit: string;
  kind: 'float' | 'int';
  step: number;
  min: number | null;
  max: number | null;
  // display = threshold * scale; canonical = value / scale.
  scale: number;
};

export type GateCriteriaRule = {
  key: string;
  label: string;
  metric: string;
  operator: '>' | '>=' | '<' | '<=';
  threshold: number;
  severity: 'blocker' | 'warning';
  passed: number;
  total: number;
  ratio: number;
  // P-EDIT — present on every rule from /api/gate-criteria; drives the inline
  // threshold editor on the Gate Criteria panel.
  editable?: GateCriteriaEditable;
};

// ---- P3 Sources / KB / Factor Network ----

export type SourceType =
  | 'rss'
  | 'patreon'
  | 'medium'
  | 'substack'
  | 'reddit'
  | 'twitter_tag'
  | 'twitter_article'
  | 'youtube_video'
  | 'tiktok'
  | 'arxiv'
  | 'glassnode'
  | 'manual';

// P6-A2 — per-source file drawer payload.
export type SourceFilesResponse = {
  source_id: number;
  source_name: string;
  nodes: KnowledgeNode[];
  total: number;
  // P12-B-M5 — `total` is now the *true* count of distinct files attached to
  // the source; `returned` is how many of those came back in this page (≤
  // `limit`). Optional so older backends still parse.
  returned?: number;
  limit: number;
};

export type IngestSource = {
  id: number;
  name: string;
  source_type: SourceType;
  url: string;
  cadence_minutes: number;
  enabled: boolean;
  is_stub?: boolean;
  category?: string | null;
  events_24h?: number;
  last_polled_at: string | null;
  last_success_at: string | null;
  last_item_hash: string | null;
  consecutive_failures: number;
  disabled_until: string | null;
  last_error_message: string | null;
  created_at: string | null;
};

export type IngestEvent = {
  id: number;
  source_id: number;
  fetched_at: string | null;
  item_url: string | null;
  item_hash: string | null;
  status: 'ok' | 'skip' | 'fail';
  error_msg: string | null;
  resulting_node_id: number | null;
};

export type KbCategory = {
  key: string;
  label: string;
  count: number;
};

export type FactorGraphNode = {
  id: string;
  label: string;
  title: string;
  kind: string;
  category: string | null;
  ic_score: number;
  color: string;
  size: number;
  // P5-BE-07 enrichments
  pagerank?: number;
  out_degree?: number;
  community_id?: number | null;
  source_url?: string | null;
  data_points?: number;
  granger_p?: number | null;
  // P6-M11 — heuristic risk score (0..1, 1 = riskiest). Backend computed.
  risk_score?: number | null;
  // P11-F4-04 — list of upstream data-source labels (e.g. ["glassnode",
  // "coinglass"]) the backend derived for this factor / concept node. Surfaced
  // in NodeInspector as a "Data Sources" KV row so operators can audit which
  // ingest pipelines feed a given factor without opening the source drawer.
  data_sources?: string[];
};

// P6-M17 — strategy → source concept reverse-lookup
export type StrategyConcept = {
  id: number;
  title: string;
  kind: string;
  category: string | null;
  ic_score: number;
  source_url?: string | null;
  tags: string[];
};

export type StrategyConceptsResponse = {
  strategy_id: number;
  concepts: StrategyConcept[];
};

export type FactorGraphEdge = {
  from: string;
  to: string;
  label?: string;
};

export type FactorGraphResponse = {
  nodes: FactorGraphNode[];
  edges: FactorGraphEdge[];
  n_factors: number;
  n_edges: number;
  n_communities?: number;
  n_granger_p?: number;
  n_factor_factor_edges?: number;   // A-M3
  n_isolates?: number;               // A-M3
  n_granger_new_24h?: number;        // A-M3 (P10)
  computed_at?: string;              // A-M3 (P10) ISO-8601
  edge_distribution?: { category: string; count: number }[];
  kind_palette?: Record<string, string>;
  strategy_status_palette?: Record<string, string>;
};

// ---- P4 Alpha Lab / Paper Trade ----

export type ChatSession = {
  id: number;
  title: string;
  started_at: string | null;
  last_msg_at: string | null;
  message_count: number;
  extracted_to_strategy_id: number | null;
};

// P11-F4-11 — tool-call attribution rendered as a chip strip above each
// assistant message. The backend may emit one entry per fetch the assistant
// performed (e.g. `clickhouse_query → btc_perp_1h` with 2,340 rows in 87ms).
// All fields except `tool` + `target` are optional so older payloads remain
// type-compatible.
export type ChatToolCall = {
  tool: string;
  target: string;
  rows?: number;
  duration_ms?: number;
};

export type ChatMessage = {
  id: number;
  session_id: number;
  role: 'user' | 'assistant' | 'system';
  content: string;
  image_paths: string[];
  tokens_in: number;
  tokens_out: number;
  ts: string | null;
  // P11-F4-11 — optional list of tool calls the assistant performed while
  // producing this message. Rendered as a chip strip in the message bubble.
  tool_calls?: ChatToolCall[];
};

export type PaperTradeRun = {
  strategy_id: number;
  name?: string;
  window_days: number;
  metrics: Record<string, number>;
  equity_curve: EquityPoint[];
  trades: number;
  is_healthy: boolean;
  health_notes: string[];
  run_at: string;
};

// P-SIM — forward SIM account (genuine walk-forward; simulation only).
export type SimEquityPoint = { t: string; equity: number; ret: number };
export type SimAccountFill = {
  bar_ts: string;
  side: 'buy' | 'sell';
  signed_qty_delta: number;
  price: number;
  fee_quote: number;
  slippage_bps: number;
  reason: string;
};
export type SimAccount = {
  strategy_id: number;
  name?: string;
  symbol: string;
  status: string; // active | stopped | liquidated
  started_at?: string | null;
  start_bar_ts?: string | null;
  last_bar_ts?: string | null;
  initial_capital: number;
  metrics: Record<string, number>;
  equity_curve?: SimEquityPoint[];
  fills?: SimAccountFill[];
  funding?: { settlements: number; net_quote: number };
  position?: { qty_base: number; position_notional: number; equity: number };
  is_healthy: boolean | null;
  health_notes: string[];
  run_at?: string;
  fee_bps?: number;
  slippage_bps?: number;
  updated_at?: string | null;
  idempotent_replay?: boolean;
};

// ---------------------------------------------------------------------------
// Fetch core
// ---------------------------------------------------------------------------

/** Error thrown by json() on a non-2xx response. Carries the numeric HTTP
 *  status and the backend's parsed `detail` so consumers can branch on a
 *  status code and render the server's actionable message verbatim instead
 *  of substring-matching `.message`. The `.message` format is preserved for
 *  backward compatibility with existing string-based callers. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  constructor(status: number, statusText: string, body: string, detail: string) {
    super(`HTTP ${status} ${statusText}: ${body.slice(0, 200)}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

// Widened init type: accepts a standard RequestInit plus an optional per-call
// timeoutMs. Default 30s; expensive compute routes (cointegration, sweeps,
// optimizer, factor evaluate) may pass a larger value, e.g. { timeoutMs: 120_000 }.
type JsonInit = RequestInit & { timeoutMs?: number };

async function fetchWithTimeout(
  input: RequestInfo,
  init: (RequestInit & { timeoutMs?: number }) | undefined,
  extraHeaders?: Record<string, string>,
): Promise<Response> {
  const ctrl = new AbortController();
  const timeoutMs = init?.timeoutMs ?? 30_000;
  let timedOut = false;
  const t = setTimeout(() => { timedOut = true; ctrl.abort(); }, timeoutMs);
  // Forward a caller-supplied signal's abort into our controller so both the
  // timeout and an external cancel trigger the same fetch abort.
  if (init?.signal) {
    if (init.signal.aborted) ctrl.abort();
    else init.signal.addEventListener('abort', () => ctrl.abort(), { once: true });
  }
  try {
    return await fetch(input, {
      ...init,
      headers: { ...(init?.headers || {}), ...(extraHeaders || {}) },
      cache: 'no-store',
      signal: ctrl.signal,
    });
  } catch (e) {
    // Only reclassify as timeout when our internal timer actually fired.
    // An external abort (navigation, component unmount) re-throws a plain
    // AbortError so React Query ignores it instead of burning the retry budget.
    if (e instanceof DOMException && e.name === 'AbortError') {
      if (timedOut) throw new Error(`Request timed out after ${timeoutMs}ms`);
      throw e;
    }
    throw e;
  } finally {
    clearTimeout(t);
  }
}

async function json<T>(input: RequestInfo, init?: JsonInit): Promise<T> {
  const hasBody = init?.body !== undefined && init?.body !== null;
  const extraHeaders: Record<string, string> = { ...operatorHeaders(), ...(hasBody ? { 'Content-Type': 'application/json' } : {}) };
  const r = await fetchWithTimeout(input, init, extraHeaders);
  if (!r.ok) {
    const body = await r.text().catch(() => '');
    // FastAPI errors are `{"detail": "..."}`; fall back to raw body text.
    let detail = body;
    try {
      const parsed = JSON.parse(body);
      if (parsed && typeof parsed.detail === 'string') detail = parsed.detail;
      // R5/BE-API-006: idempotency-cached error payloads arrive as
      // { detail: { error, detail } } (FastAPI wraps the dict). Extract the
      // inner human-readable string instead of falling back to the raw body.
      else if (parsed && parsed.detail && typeof parsed.detail === 'object') {
        detail = typeof parsed.detail.detail === 'string' ? parsed.detail.detail : JSON.stringify(parsed.detail);
      }
    } catch {
      /* body is not JSON — keep raw text as detail */
    }
    throw new ApiError(r.status, r.statusText, body, detail);
  }
  return (await r.json()) as T;
}

// ---------------------------------------------------------------------------
// API surface
// ---------------------------------------------------------------------------

// P-SYSHEALTH — one row of the Mission Control System Health checklist.
export type SystemCheckItem = {
  group: string;
  name: string;
  state: 'ok' | 'warn' | 'off' | 'fail';
  detail: string;
};

// T2-C — KB vital signs + true count (the /api/graph payload caps at 1000, so
// the KPI rail's graph-derived count saturates; this exposes the real total).
export type KbVitalSigns = {
  diversity_index: number;
  gap_pressure: number;
  orphan_rate: number;
  connectivity: number;
  largest_component_fraction: number;
  bridge_count: number;
  node_count: number;
  edge_count: number;
  category_count: number;
  bridges: { node_id: number; score: number; degree: number; category: string | null }[];
  gaps: { category: string; concept_count: number; realized_count: number; gap_pressure: number }[];
};

export type KnowledgeStatsResponse = {
  total_knowledge_nodes: number;
  by_kind: Record<string, number>;
  vital_signs: KbVitalSigns;
};

export type CostSummaryResponse = {
  window_days: number;
  total_cost_usd: number;
  total_calls: number;
  total_input_chars: number;
  total_output_chars: number;
  real_cost_calls: number;
  estimated_calls: number;
  by_agent: { agent: string; cost_usd: number; calls: number }[];
  by_model: { model: string; cost_usd: number; calls: number }[];
  top_strategies: { strategy_id: number; cost_usd: number; calls: number }[];
  today_cost_usd: number;
  today_calls: number;
};

export const api = {
  health: () =>
    json<{ status: string; anthropic_key_present: boolean; llm: LLMConfig }>(
      `${API_BASE}/api/health`,
    ),
  // P-SYSHEALTH — dependency / config readiness checklist for Mission Control.
  systemChecklist: () =>
    json<{ items: SystemCheckItem[]; ok_count: number; fail_count: number; total: number }>(
      `${API_BASE}/api/system/checklist`,
    ),
  knowledge: () => json<{ nodes: KnowledgeNode[] }>(`${API_BASE}/api/knowledge`),
  knowledgeOne: (id: number) =>
    json<KnowledgeNode>(`${API_BASE}/api/knowledge/${id}`),
  // Newest page, unchanged — safe to pass directly as a TanStack `queryFn`
  // (which would otherwise inject its context object as the first argument).
  strategies: () =>
    json<{ strategies: AlphaStrategy[]; total?: number }>(`${API_BASE}/api/strategies`),
  // Searches the WHOLE strategy table server-side, so entries older than the
  // newest page are reachable instead of being capped out of the client list.
  strategiesSearch: (opts: { q?: string; status?: string; limit?: number; offset?: number }) => {
    const params = new URLSearchParams();
    if (opts.q) params.set('q', opts.q);
    if (opts.status && opts.status !== 'all') params.set('status', opts.status);
    if (opts.limit != null) params.set('limit', String(opts.limit));
    if (opts.offset != null) params.set('offset', String(opts.offset));
    const qs = params.toString();
    return json<{ strategies: AlphaStrategy[]; total?: number; limit?: number; offset?: number }>(
      `${API_BASE}/api/strategies${qs ? `?${qs}` : ''}`,
    );
  },
  strategy: (id: number) => json<AlphaStrategy>(`${API_BASE}/api/strategies/${id}`),
  strategyConcepts: (id: number) =>
    json<StrategyConceptsResponse>(`${API_BASE}/api/strategies/${id}/concepts`),
  strategyTrades: (id: number, opts?: { offset?: number; limit?: number; nonzeroOnly?: boolean }) => {
    const params = new URLSearchParams();
    if (opts?.offset != null) params.set('offset', String(opts.offset));
    if (opts?.limit != null) params.set('limit', String(opts.limit));
    if (opts?.nonzeroOnly) params.set('nonzero_only', 'true');
    const qs = params.toString();
    return json<TradeTapeResponse>(
      `${API_BASE}/api/strategies/${id}/trades${qs ? `?${qs}` : ''}`,
    );
  },
  graph: () => json<{ nodes: GraphNode[]; edges: GraphEdge[] }>(`${API_BASE}/api/graph`),
  // T2-C — true KB node count + vital signs.
  knowledgeStats: () => json<KnowledgeStatsResponse>(`${API_BASE}/api/knowledge/stats`),
  costSummary: (days = 7) =>
    json<CostSummaryResponse>(`${API_BASE}/api/cost/summary?days=${days}`),
  // D-H5 — intake now requires Idempotency-Key. Same-text retry replays the
  // cached node instead of spawning a duplicate KnowledgeNode.
  intake: (raw_text: string, opts?: { idempotencyKey?: string }) =>
    json<{ node: KnowledgeNode; idempotent_replay?: boolean }>(`${API_BASE}/api/intake`, {
      method: 'POST',
      headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
      body: JSON.stringify({ raw_text }),
    }),
  pipelineRun: (
    raw_text: string,
    node_ids?: number[],
    opts?: { idempotencyKey?: string },
  ) =>
    json<{ strategy_id: number; status: string; status_url: string; idempotent_replay?: boolean }>(
      `${API_BASE}/api/pipeline/run`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
        body: JSON.stringify({ raw_text, node_ids }),
      },
    ),
  pipelineStatus: (id: number) =>
    json<PipelineStatusPayload>(`${API_BASE}/api/pipeline/status/${id}`),

  // Mission Control endpoints (P0)
  agents: () => json<{ agents: AgentPersona[] }>(`${API_BASE}/api/agents`),
  pipelineBuckets: () =>
    json<{ buckets: PipelineBucket[]; total: number }>(
      `${API_BASE}/api/pipeline/buckets`,
    ),
  daemonLog: (limit = 50) =>
    json<{ events: DaemonLogEvent[] }>(
      `${API_BASE}/api/daemon-log?limit=${limit}`,
    ),

  // Backtest Panel endpoints (P2)
  portfolioMethods: () =>
    json<{ methods: WeightingMethod[]; default: string }>(
      `${API_BASE}/api/portfolio/methods`,
    ),
  portfolioCombine: (strategy_ids: number[], method: string) =>
    json<CombinedPortfolio>(`${API_BASE}/api/portfolio/combine`, {
      method: 'POST',
      body: JSON.stringify({ strategy_ids, method }),
    }),
  gateCriteria: () =>
    json<{ rules: GateCriteriaRule[] }>(`${API_BASE}/api/gate-criteria`),
  // P-EDIT — persist operator threshold overrides. `value` is the display
  // value (matching rule.editable.value); the backend converts via scale and
  // clamps to [min, max]. Affects only the gate-criteria checklist evaluation.
  gateCriteriaUpdate: (rules: { key: string; value: number }[]) =>
    json<{ ok: boolean; updated: string[]; rules: GateCriteriaRule[] }>(
      `${API_BASE}/api/gate-criteria`,
      { method: 'PUT', body: JSON.stringify({ rules }) },
    ),
  gateCriteriaReset: () =>
    json<{ ok: boolean; rules: GateCriteriaRule[] }>(
      `${API_BASE}/api/gate-criteria/reset`,
      { method: 'POST' },
    ),

  // P3 Sources
  sources: () =>
    json<{
      sources: IngestSource[];
      supported_types: SourceType[];
      supported_categories?: string[];
    }>(`${API_BASE}/api/sources`),
  // D-M12/P16 — source CRUD now requires Idempotency-Key (matching the new
  // backend wrappers). Same payload + same key replays the cached response
  // (no second create / no duplicate cascade delete). Callers that wish to
  // dedupe across reloads should pass a persisted key via `opts.idempotencyKey`.
  sourceCreate: (
    payload: {
      name: string;
      source_type: SourceType;
      url: string;
      cadence_minutes: number;
      enabled: boolean;
      category?: string | null;
    },
    opts?: { idempotencyKey?: string },
  ) =>
    json<IngestSource & { warning?: string; idempotent_replay?: boolean }>(
      `${API_BASE}/api/sources`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
        body: JSON.stringify(payload),
      },
    ),
  sourceUpdate: (
    id: number,
    payload: {
      name?: string;
      cadence_minutes?: number;
      enabled?: boolean;
      category?: string | null;
    },
    opts?: { idempotencyKey?: string },
  ) =>
    json<IngestSource & { idempotent_replay?: boolean }>(`${API_BASE}/api/sources/${id}`, {
      method: 'PATCH',
      headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
      body: JSON.stringify(payload),
    }),
  sourceDelete: (id: number, opts?: { idempotencyKey?: string }) =>
    json<{ deleted: boolean; id: number; idempotent_replay?: boolean }>(
      `${API_BASE}/api/sources/${id}`,
      {
        method: 'DELETE',
        headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
      },
    ),
  // D-H11 — source poll requires Idempotency-Key. Retry replays cached result
  // and respects the 10s per-source rate limit on the backend.
  sourcePollNow: (id: number, opts?: { idempotencyKey?: string }) =>
    json<{ source_id: number; status: string; idempotent_replay?: boolean }>(
      `${API_BASE}/api/sources/${id}/poll`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
      },
    ),
  sourceEvents: (id: number, limit = 50) =>
    json<{ events: IngestEvent[] }>(
      `${API_BASE}/api/sources/${id}/events?limit=${limit}`,
    ),
  sourceFiles: (id: number, limit = 200) =>
    json<SourceFilesResponse>(
      `${API_BASE}/api/sources/${id}/files?limit=${limit}`,
    ),
  schedulerStatus: () =>
    json<SchedulerStatusResponse>(
      `${API_BASE}/api/scheduler/status`,
    ),
  telegramRecentIntakes: (limit = 20) =>
    json<TelegramRecentIntakesResponse>(
      `${API_BASE}/api/telegram/recent-intakes?limit=${limit}`,
    ),
  discordStatus: () =>
    json<DiscordStatusResponse>(`${API_BASE}/api/discord/status`),
  discordRecentIntakes: (limit = 20) =>
    json<DiscordRecentIntakesResponse>(
      `${API_BASE}/api/discord/recent-intakes?limit=${limit}`,
    ),

  // P8-FIX/H-2 — knowledge node postmortems reverse-link
  knowledgePostmortems: (id: number) =>
    json<KnowledgePostmortemsResponse>(`${API_BASE}/api/knowledge/${id}/postmortems`),

  // P8-FIX/C-4 — pipeline buckets V2
  pipelineBucketsV2: () =>
    json<PipelineBucketsV2Response>(`${API_BASE}/api/v2/pipeline/buckets`),

  // F3 — auto-pipeline status snapshot (Mission Control Stage 0 queue overlay)
  autoPipelineStatus: () =>
    json<AutoPipelineStatus>(`${API_BASE}/api/auto-pipeline/status`),

  // P8-FIX/H-7 — agent self-dialogue transcript
  agentDialogue: (strategy_id?: number, limit = 200) =>
    json<AgentDialogueResponse>(
      `${API_BASE}/api/agent-dialogue${qs({
        strategy_id: strategy_id ?? '',
        limit,
      })}`,
    ),

  // P8-FIX/C-2 — Engle-Granger cointegration pair scan
  cointegrationPairs: (opts?: { lookback_days?: number; p_threshold?: number; refresh?: boolean }) =>
    json<CointegrationResponse>(
      `${API_BASE}/api/cointegration/pairs${qs({
        lookback_days: opts?.lookback_days ?? '',
        p_threshold: opts?.p_threshold ?? '',
        refresh: opts?.refresh ? 'true' : '',
      })}`,
    ),

  // P8-FIX/H-15 — Alpha Lab Suggested Topics rail
  alphaLabSuggestedTopics: (limit = 6) =>
    json<SuggestedTopicsResponse>(
      `${API_BASE}/api/alpha-lab/suggested-topics?limit=${limit}`,
    ),

  // P3 KB Explorer
  kbCategories: () =>
    json<{ categories: KbCategory[]; total: number }>(
      `${API_BASE}/api/kb-spaces/categories`,
    ),
  kbByCategory: (category: string, limit = 100) =>
    json<{ nodes: KnowledgeNode[]; category: string }>(
      `${API_BASE}/api/kb-spaces/by-category/${encodeURIComponent(category)}?limit=${limit}`,
    ),

  // P3 Factor Network
  factorNetwork: () => json<FactorGraphResponse>(`${API_BASE}/api/factor-network`),

  // P4 Alpha Lab
  chatSessions: (limit = 200) =>
    json<{ sessions: ChatSession[]; limit: number }>(`${API_BASE}/api/chat/sessions?limit=${limit}`),
  // P-MODEL-SEL — selectable models for the Alpha Lab picker (server default first).
  chatModels: () =>
    json<{ models: string[]; default: string | null }>(`${API_BASE}/api/chat/models`),
  chatSessionDetail: (id: number) =>
    json<{ session: ChatSession; messages: ChatMessage[] }>(
      `${API_BASE}/api/chat/sessions/${id}`,
    ),
  // D-H-chat-send — chat send now requires Idempotency-Key. A double-click or
  // slow-response retry replays the cached reply instead of double-billing the
  // LLM + double-inserting rows. Mint ONE key per user submit (see ChatComposer).
  chatSend: (
    payload: {
      session_id?: number | null;
      user_text: string;
      image_paths?: string[] | null;
      model?: string | null;
    },
    opts?: { idempotencyKey?: string },
  ) =>
    json<{
      session: ChatSession;
      user_message: ChatMessage;
      assistant_message: ChatMessage;
      idempotent_replay?: boolean;
    }>(`${API_BASE}/api/chat/send`, {
      method: 'POST',
      headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
      body: JSON.stringify(payload),
      // P-MINIMAX — reasoning models (e.g. minimax-m3) can think for 30–90s on a
      // hard prompt; the 30s default would abort a perfectly good reply. Match
      // the backend's per-call LLM budget (~120s).
      timeoutMs: 120_000,
    }),
  chatSessionDelete: (id: number, opts?: { idempotencyKey?: string }) =>
    // P21 — backend returns 200 (with `{deleted, id}` body), not 204.
    // D-M4/P16 — DELETE /api/chat/sessions/{id} requires Idempotency-Key.
    json<{ deleted: boolean; id: number; idempotent_replay?: boolean }>(
      `${API_BASE}/api/chat/sessions/${id}`,
      { method: 'DELETE', headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() } },
    ),
  // D-H8a — chat extract requires Idempotency-Key. Retry replays cached
  // strategy_id instead of spawning a duplicate pipeline run.
  chatExtract: (sessionId: number, title?: string, opts?: { idempotencyKey?: string }) =>
    json<{ strategy_id: number; status: string; status_url: string; session_id: number; idempotent_replay?: boolean }>(
      `${API_BASE}/api/chat/sessions/${sessionId}/extract`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
        body: JSON.stringify(title ? { title } : {}),
      },
    ),
  chatUpload: async (file: File): Promise<{ path: string; filename: string; size: number; mime: string }> => {
    const form = new FormData();
    form.append('file', file);
    // Note: do NOT set Content-Type — browser must set multipart boundary.
    const r = await fetchWithTimeout(`${API_BASE}/api/chat/upload`, {
      method: 'POST',
      body: form,
      timeoutMs: 120_000,
    }, operatorHeaders());
    if (!r.ok) {
      const body = await r.text().catch(() => '');
      let detail = body;
      try {
        const parsed = JSON.parse(body);
        if (parsed && typeof parsed.detail === 'string') detail = parsed.detail;
      } catch {
        /* body is not JSON — keep raw text as detail */
      }
      throw new ApiError(r.status, r.statusText, body, detail);
    }
    return r.json() as Promise<{ path: string; filename: string; size: number; mime: string }>;
  },

  // P4 Paper Trade
  // P29-F12: backend returns {runs, total, limit, offset}; bump ?limit=500.
  paperTradeList: () =>
    json<{ runs: PaperTradeRun[]; total?: number; limit?: number; offset?: number }>(
      `${API_BASE}/api/paper-trade?limit=500`,
    ),
  paperTradeDetail: (strategy_id: number) =>
    json<PaperTradeRun>(`${API_BASE}/api/paper-trade/${strategy_id}`),
  // D-H6 — paper-trade run requires Idempotency-Key. Retry replays cached result.
  paperTradeRun: (strategy_id: number, window_days = 30, opts?: { idempotencyKey?: string }) =>
    json<PaperTradeRun & { idempotent_replay?: boolean }>(`${API_BASE}/api/paper-trade/run`, {
      method: 'POST',
      headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
      body: JSON.stringify({ strategy_id, window_days }),
    }),

  // P-SIM — forward SIM account. start() pins from the latest bar; tick is
  // naturally idempotent (recomputes from the immutable ledger) so it needs no
  // Idempotency-Key. SIMULATION ONLY — never routes to a live venue.
  simAccountList: () =>
    json<{ accounts: SimAccount[]; total: number }>(`${API_BASE}/api/sim-account`),
  simAccountDetail: (strategy_id: number) =>
    json<SimAccount>(`${API_BASE}/api/sim-account/${strategy_id}`),
  simAccountStart: (
    strategy_id: number,
    opts?: { initial_capital?: number; fee_bps?: number; slippage_bps?: number; idempotencyKey?: string },
  ) =>
    json<SimAccount>(`${API_BASE}/api/sim-account/start`, {
      method: 'POST',
      headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
      body: JSON.stringify({
        strategy_id,
        ...(opts?.initial_capital != null ? { initial_capital: opts.initial_capital } : {}),
        ...(opts?.fee_bps != null ? { fee_bps: opts.fee_bps } : {}),
        ...(opts?.slippage_bps != null ? { slippage_bps: opts.slippage_bps } : {}),
      }),
    }),
  simAccountTick: (strategy_id: number) =>
    json<SimAccount>(`${API_BASE}/api/sim-account/${strategy_id}/tick`, { method: 'POST' }),
  simAccountStop: (strategy_id: number) =>
    json<SimAccount>(`${API_BASE}/api/sim-account/${strategy_id}/stop`, { method: 'POST' }),

  // P4 Operator actions. P13/D-H5 — promote/retire now require an
  // Idempotency-Key header so a retry from a flaky network is a replay, not
  // a duplicate state transition. We mint a fresh UUID per call by default.
  strategyPromote: (
    id: number,
    target: 'PAPER_TRADE' | 'SMALL_CAPITAL' | 'LIVE',
    opts?: { force?: boolean; force_reason?: string; idempotencyKey?: string },
  ) =>
    json<AlphaStrategy & { idempotent_replay?: boolean }>(
      `${API_BASE}/api/strategies/${id}/promote`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
        body: JSON.stringify({
          target,
          ...(opts?.force ? { force: true } : {}),
          ...(opts?.force_reason ? { force_reason: opts.force_reason } : {}),
        }),
      },
    ),
  strategyRetire: (id: number, opts?: { idempotencyKey?: string }) =>
    json<AlphaStrategy & { idempotent_replay?: boolean }>(
      `${API_BASE}/api/strategies/${id}/retire`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
      },
    ),

  // -------------------------------------------------------------------------
  // P7 — 11 pages turned live
  // -------------------------------------------------------------------------

  // /pipeline-analytics
  paThroughput: (days = 30, bucket: 'day' | 'week' = 'day') =>
    json<PAThroughput>(`${API_BASE}/api/pipeline-analytics/throughput?days=${days}&bucket=${bucket}`),
  paTimeInStage: (days = 90) =>
    json<PATimeInStage>(`${API_BASE}/api/pipeline-analytics/time-in-stage?days=${days}`),
  paGatePassRate: (days = 90, window = 14) =>
    json<PAGatePassRate>(`${API_BASE}/api/pipeline-analytics/gate-pass-rate?days=${days}&window=${window}`),
  paOccupancy: () =>
    json<PAOccupancy>(`${API_BASE}/api/pipeline-analytics/occupancy`),

  // /mission-panel
  missionPanelSnapshot: () => json<MissionPanelSnapshot>(`${API_BASE}/api/mission-panel/snapshot`),
  missionPanelIncidents: (hours = 24, limit = 50) =>
    json<MissionPanelIncidents>(`${API_BASE}/api/mission-panel/incidents?hours=${hours}&limit=${limit}`),
  // D-H9 — manual six-hour pulse requires Idempotency-Key. Retry within the
  // same hour replays the cached send result instead of double-sending.
  missionPanelFireSixHour: (force = false, opts?: { idempotencyKey?: string }) =>
    json<{ ok: boolean; sent: boolean; idempotent_replay?: boolean }>(
      `${API_BASE}/api/mission-panel/fire-six-hour-now${force ? '?force=true' : ''}`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
      },
    ),

  // /backtest-lab
  backtestLabParams: () => json<BacktestLabParamsResp>(`${API_BASE}/api/backtest-lab/params`),
  // D-H10 — backtest-lab sweep create requires Idempotency-Key. Retry replays
  // cached sweep_id instead of spawning a duplicate sweep run.
  backtestLabCreateSweep: (body: BacktestLabSweepReq, opts?: { idempotencyKey?: string }) =>
    json<{ sweep_id: number; cells_total: number; status: string; idempotent_replay?: boolean }>(
      `${API_BASE}/api/backtest-lab/sweep`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
        body: JSON.stringify(body),
      },
    ),
  backtestLabGetSweep: (id: number) => json<BacktestLabSweep>(`${API_BASE}/api/backtest-lab/sweep/${id}`),
  backtestLabListSweeps: (strategy_id: number, limit = 20) =>
    json<{ sweeps: BacktestLabSweep[] }>(`${API_BASE}/api/backtest-lab/sweeps?strategy_id=${strategy_id}&limit=${limit}`),
  backtestLabCancel: (id: number, opts?: { idempotencyKey?: string }) =>
    json<{ cancelled: boolean; id: number; deleted: boolean; idempotent_replay?: boolean }>(
      `${API_BASE}/api/backtest-lab/sweep/${id}`,
      { method: 'DELETE', headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() } },
    ),

  // /alpha-flow
  alphaFlowSankey: (days = 30) => json<AlphaFlowSankey>(`${API_BASE}/api/alpha-flow/sankey?days=${days}`),
  alphaFlowStrategyTimeline: (id: number) =>
    json<AlphaFlowTimeline>(`${API_BASE}/api/alpha-flow/strategy/${id}/timeline`),
  alphaFlowDropoutStats: (days = 30) =>
    json<AlphaFlowDropout>(`${API_BASE}/api/alpha-flow/dropout-stats?days=${days}`),

  // /ir-explorer
  // Guard: an empty array [] must NOT silently fall through to backend's 4-status default.
  // `[].join(',')` produces '' which qs() strips, causing the backend _parse_statuses() to
  // treat it as "no filter" and return data for all 4 default statuses. We normalise by
  // converting [] → undefined so callers always get consistent behaviour: undefined and []
  // both omit the param (= backend default). If a future need arises to express "zero statuses",
  // a dedicated backend sentinel must be introduced.
  irBook: (statuses?: string[], benchmark: 'btc' | 'none' = 'btc') =>
    json<IrBookResponse>(`${API_BASE}/api/ir-explorer/book-ir${qs({ statuses: statuses && statuses.length > 0 ? statuses.join(',') : undefined, benchmark })}`),
  irByCategory: (statuses?: string[], benchmark: 'btc' | 'none' = 'btc') =>
    json<IrByCategory>(`${API_BASE}/api/ir-explorer/by-category${qs({ statuses: statuses && statuses.length > 0 ? statuses.join(',') : undefined, benchmark })}`),
  irByRegime: (statuses?: string[], benchmark: 'btc' | 'none' = 'btc') =>
    json<IrByRegime>(`${API_BASE}/api/ir-explorer/by-regime${qs({ statuses: statuses && statuses.length > 0 ? statuses.join(',') : undefined, benchmark })}`),
  irByAsset: (statuses?: string[], benchmark: 'btc' | 'none' = 'btc') =>
    json<IrByAsset>(`${API_BASE}/api/ir-explorer/by-asset${qs({ statuses: statuses && statuses.length > 0 ? statuses.join(',') : undefined, benchmark })}`),
  irRolling: (statuses?: string[], benchmark: 'btc' | 'none' = 'btc', window = 30) =>
    json<IrRolling>(`${API_BASE}/api/ir-explorer/rolling${qs({ statuses: statuses && statuses.length > 0 ? statuses.join(',') : undefined, benchmark, window })}`),
  irWaterfall: (statuses?: string[], benchmark: 'btc' | 'none' = 'btc') =>
    json<IrWaterfall>(`${API_BASE}/api/ir-explorer/waterfall${qs({ statuses: statuses && statuses.length > 0 ? statuses.join(',') : undefined, benchmark })}`),

  // /portfolio-optimizer
  poCombine: (body: PoCombineReq) =>
    json<PoCombineResp>(`${API_BASE}/api/portfolio-optimizer/combine`, { method: 'POST', body: JSON.stringify(body) }),
  // D-H8b — portfolio save requires Idempotency-Key. Retry replays cached row.
  poSave: (body: PoSaveReq, opts?: { idempotencyKey?: string }) =>
    json<SavedPortfolio & { idempotent_replay?: boolean }>(
      `${API_BASE}/api/portfolio-optimizer/save`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
        body: JSON.stringify(body),
      },
    ),
  poSaved: () => json<{ portfolios: SavedPortfolio[] }>(`${API_BASE}/api/portfolio-optimizer/saved`),
  poDelete: (id: number, opts?: { idempotencyKey?: string }) =>
    json<{ deleted: boolean; id: number; idempotent_replay?: boolean }>(
      `${API_BASE}/api/portfolio-optimizer/saved/${id}`,
      { method: 'DELETE', headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() } },
    ),
  poFrontier: (strategy_ids: number[], vol_min = 0.05, vol_max = 0.30, steps = 10) =>
    json<PoFrontier>(`${API_BASE}/api/portfolio-optimizer/frontier?strategies=${strategy_ids.join(',')}&vol_min=${vol_min}&vol_max=${vol_max}&steps=${steps}`),

  // /factor-studio
  fsEvaluate: (body: FsEvalReq) =>
    json<FsEvalResult>(`${API_BASE}/api/factor-studio/evaluate`, { method: 'POST', body: JSON.stringify(body) }),
  // D-H8c — factor save requires Idempotency-Key. Retry replays cached factor.
  fsSave: (body: FsSaveReq, opts?: { idempotencyKey?: string }) =>
    json<{ factor: SavedFactor; idempotent_replay?: boolean }>(
      `${API_BASE}/api/factor-studio/save`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
        body: JSON.stringify(body),
      },
    ),
  fsList: () => json<{ factors: SavedFactor[] }>(`${API_BASE}/api/factor-studio/factors`),
  fsOne: (id: number) => json<SavedFactor>(`${API_BASE}/api/factor-studio/factors/${id}`),
  fsDelete: (id: number, opts?: { idempotencyKey?: string }) =>
    // D-M7/P16 — factor delete requires Idempotency-Key; migrate to json() to
    // match sourceDelete / chatSessionDelete patterns and parse the response.
    json<{ deleted: boolean; id: number; idempotent_replay?: boolean }>(
      `${API_BASE}/api/factor-studio/factors/${id}`,
      { method: 'DELETE', headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() } },
    ),
  // D-H8d — factor promote requires Idempotency-Key. Retry replays cached
  // strategy_id instead of spawning a duplicate background pipeline.
  fsPromote: (id: number, name_override?: string, opts?: { idempotencyKey?: string }) =>
    json<{ strategy_id: number; status_url: string; idempotent_replay?: boolean }>(
      `${API_BASE}/api/factor-studio/promote/${id}${name_override ? `?name_override=${encodeURIComponent(name_override)}` : ''}`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
      },
    ),

  // /alpha-genealogy
  agForest: () => json<AgForest>(`${API_BASE}/api/alpha-genealogy/forest`),
  agTree: (id: number) => json<{ tree: AgTreeNode | null }>(`${API_BASE}/api/alpha-genealogy/tree/${id}`),
  agLineage: (id: number) => json<AgLineage>(`${API_BASE}/api/alpha-genealogy/lineage/${id}`),

  // /live-trade
  liveTradeDashboard: () => json<LiveTradeDashboard>(`${API_BASE}/api/live-trade/dashboard`),
  liveTradeExchangePing: () =>
    json<{ status: string; latency_ms: number | null; venue: string | null; note: string | null }>(
      `${API_BASE}/api/live-trade/exchange-ping`,
    ),
  liveTradePauseAll: (idempotencyKey: string) =>
    json<{ paused_count: number; ids: number[]; idempotent_replay: boolean }>(
      `${API_BASE}/api/live-trade/pause-all`,
      { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey } },
    ),
  // P13/D-H5 — resume requires Idempotency-Key to match pause-all's retry safety.
  liveTradeResume: (id: number, opts?: { idempotencyKey?: string }) =>
    json<AlphaStrategy & { idempotent_replay?: boolean }>(
      `${API_BASE}/api/live-trade/resume/${id}`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': opts?.idempotencyKey ?? cryptoUuid() },
      },
    ),
  liveTradeAudit: (limit = 50) =>
    json<{ items: AuditLogRow[] }>(`${API_BASE}/api/live-trade/audit?limit=${limit}`),

  // /trading-terminal
  ttStatus: () => json<TtStatus>(`${API_BASE}/api/trading-terminal/status`),
  ttSymbols: () => json<{ symbols: TtSymbol[] }>(`${API_BASE}/api/trading-terminal/symbols`),
  ttMarket: (symbol: string, init?: RequestInit) => json<TtMarketInfo>(`${API_BASE}/api/trading-terminal/symbols/${symbol}/market`, init),
  ttPositions: (mode: 'paper' | 'live' = 'paper', init?: RequestInit) =>
    json<{ positions: TtPosition[] }>(`${API_BASE}/api/trading-terminal/positions?mode=${mode}`, init),
  // D-M8 / D-L4 — preview is read-only (no DB writes). No Idempotency-Key.
  ttPreview: (body: TtOrderReq, terminalSessionId: string) =>
    json<TtOrderPreview>(`${API_BASE}/api/trading-terminal/preview`, {
      method: 'POST',
      body: JSON.stringify(body),
      headers: { 'X-Terminal-Session': terminalSessionId },
    }),
  ttSubmit: (body: TtOrderReq, idempotencyKey: string, terminalSessionId: string) =>
    json<TtOrderSubmitResp>(`${API_BASE}/api/trading-terminal/submit`, {
      method: 'POST',
      body: JSON.stringify(body),
      headers: { 'X-Terminal-Session': terminalSessionId, 'Idempotency-Key': idempotencyKey },
    }),
  ttCancel: (order_uid: string, idempotencyKey: string) =>
    json<TtOrder>(`${API_BASE}/api/trading-terminal/orders/${order_uid}/cancel`, {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
    }),
  // P31-KILL — bulk kill-switch. Cancels ALL pending manual orders for `mode`.
  // Caller MUST mint a fresh Idempotency-Key per intended bulk-cancel so a
  // double-click replays the same key (backend returns the cached summary)
  // rather than re-scanning. Response also carries idempotent_replay.
  ttCancelAll: (idempotencyKey: string, mode: 'paper' | 'live' = 'paper') =>
    json<TtCancelAllResp>(`${API_BASE}/api/trading-terminal/orders/cancel-all?mode=${encodeURIComponent(mode)}`, {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
    }),
  ttOrders: (limit = 50, status?: string, symbol?: string, mode?: string) =>
    json<{ orders: TtOrder[] }>(`${API_BASE}/api/trading-terminal/orders${qs({ limit, status, symbol, mode })}`),
};


// ---- query-string helper ---------------------------------------------------

function qs(params: Record<string, string | number | undefined | null>): string {
  const out: string[] = [];
  for (const k of Object.keys(params)) {
    const v = params[k];
    if (v === undefined || v === null || v === '') continue;
    out.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  }
  return out.length ? `?${out.join('&')}` : '';
}


// ---------------------------------------------------------------------------
// Operator authentication helper.
// Reads NEXT_PUBLIC_OPERATOR_TOKEN (set in .env.local / deployment env).
// Returns an Authorization header object when the token is present, or an
// empty object when running in dev without the token configured.
// ---------------------------------------------------------------------------
function operatorHeaders(): Record<string, string> {
  const token =
    typeof process !== 'undefined'
      ? (process.env.NEXT_PUBLIC_OPERATOR_TOKEN ?? '')
      : '';
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function cryptoUuid(): string {
  // P15/C-H12 — Prefer globalThis.crypto.randomUUID, available in:
  //   - All modern browsers
  //   - Node 19+ (stable global Web Crypto)
  //   - Next.js SSR runtime (polyfilled since 13+)
  // Avoid require('node:crypto') because Next.js 14 webpack rejects the
  // node: scheme in browser bundles even behind try/catch (parse-time check).
  const g: { crypto?: { randomUUID?: () => string } } | undefined =
    typeof globalThis !== 'undefined'
      ? (globalThis as unknown as { crypto?: { randomUUID?: () => string } })
      : undefined;
  if (g && g.crypto && typeof g.crypto.randomUUID === 'function') {
    return g.crypto.randomUUID();
  }
  // Math.random fallback for sandboxed runtimes without WebCrypto.
  // Acceptable for Idempotency-Key because the server is the authoritative
  // dedup source — a collision is detected server-side and replays the
  // cached response (no double-execution risk).
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}


// ---------------------------------------------------------------------------
// P7 — domain types for the 11 new pages
// ---------------------------------------------------------------------------

export type PAThroughputPoint = { date: string; counts: Record<string, number> };
export type PAThroughput = {
  days: number;
  bucket: 'day' | 'week';
  series: PAThroughputPoint[];
  totals: Record<string, number>;
  generated_at: string;
};
export type PATimeInStageRow = {
  status: string;
  bucket_key: string;
  n: number;
  p50_minutes: number;
  p90_minutes: number;
  p99_minutes: number;
  histogram: { bin_label: string; count: number }[];
};
export type PATimeInStage = { days: number; per_status: PATimeInStageRow[]; generated_at: string };
export type PAGate = {
  key: string;
  label: string;
  from: string;
  pass_to: string;
  fail_to: string[];
  total: number;
  passed: number;
  failed: number;
  pass_rate: number | null;
};
export type PAGatePassRate = {
  days: number;
  window: number;
  gates: PAGate[];
  trend: { date: string; rates: Record<string, number | null> }[];
  generated_at: string;
};
export type PAOccupancyStatus = {
  status: string;
  bucket_key: string;
  count: number;
  median_age_minutes: number;
  stuck: { id: number; name: string; age_minutes: number }[];
};
export type PAOccupancy = {
  per_status: PAOccupancyStatus[];
  bottleneck_status: string | null;
  generated_at: string;
};

export type DaemonRow = {
  task_id: string;
  enabled: boolean;
  running: boolean;
  module_available?: boolean;
  interval_seconds?: number;
  last_run_at?: string | null;
  last_error?: string | null;
  seconds_since_run?: number | null;
  schedule_status?: string;
  env_flag?: string;
};
export type MissionPanelSnapshot = {
  generated_at: string;
  window_hours: number;
  incidents: {
    ingest_failures_24h: number;
    ingest_failures_recent: { id: number; source_id: number | null; fetched_at: string | null; error_msg: string | null }[];
    daemon_errors: { task_id: string; last_error: string | null; last_run_at: string | null }[];
    unhealthy_paper_runs: { strategy_id: number | null; reason: string; notes: string[] }[];
  };
  daemons: DaemonRow[];
  dispatches: {
    recent_transitions: { strategy_id: number; name: string; status: string; updated_at: string | null }[];
  };
  daily_ticker: {
    today: { new_strategies: number; ic_ingested: number; paper_pnl_pct: number | null };
    yesterday: { new_strategies: number; ic_ingested: number; paper_pnl_pct: number | null };
  };
  pulse_preview: {
    would_send_at: string;
    cooldown_blocking: boolean;
    telegram_configured: boolean;
    rendered_markdown: string;
  };
  last_telegram_reports: { id: number; report_type: string; sent_at: string | null; success: boolean; summary: string | null }[];
};
export type MissionPanelIncidents = {
  hours: number;
  items: { kind: string; at: string | null; severity: 'info' | 'warn' | 'error'; summary: string; ref?: Record<string, unknown> }[];
  total: number;
};

export type BacktestLabParam = { name: string; type: 'int' | 'float'; min: number; max: number; kind: string; unit?: string; label?: string };
export type BacktestLabParamsResp = { params: BacktestLabParam[]; max_cells: number; workers: number; enabled: boolean };
export type BacktestLabSweepReq = {
  strategy_id: number;
  param_x_name: string;
  param_x_values: number[];
  param_y_name?: string;
  param_y_values?: number[];
  seed?: number;
};
export type BacktestLabCellMetrics = {
  sharpe: number;
  max_drawdown: number;
  cum_return: number;
  profit_factor: number;
  win_rate: number;
  trades: number;
};
export type BacktestLabCell = {
  x: number;
  y: number | null;
  metrics?: BacktestLabCellMetrics;
  error?: string;
};
export type BacktestLabSweep = {
  sweep_id: number;
  strategy_id: number;
  param_x_name: string;
  param_x_values: number[];
  param_y_name: string | null;
  param_y_values: number[] | null;
  cells_total: number;
  cells_done: number;
  status: 'queued' | 'running' | 'done' | 'partial' | 'failed' | 'cancelled';
  cells: BacktestLabCell[];
  error_message: string | null;
  seed: number;
  duration_ms: number;
  created_at: string | null;
  updated_at: string | null;
};

export type AlphaFlowNode = { name: string; stage: number; incoming: number; outgoing: number; self_loops: number };
export type AlphaFlowLink = {
  source: number; target: number;
  source_name: string; target_name: string;
  value: number; kind: 'advance' | 'reject' | 'loop' | 'retreat';
  median_dwell_sec: number; median_dwell_human: string;
};
export type AlphaFlowSankey = {
  days: number;
  nodes: AlphaFlowNode[];
  links: AlphaFlowLink[];
  total_transitions: number;
  generated_at: string;
};
export type AlphaFlowTimeline = {
  strategy: AlphaStrategy | null;
  events: { id: number; from_status: string | null; to_status: string; from_stage: number | null; to_stage: number; transitioned_at: string | null; actor: string; reason: string | null; dwell_sec_in_from: number | null }[];
  current_status: string | null;
  total_age_sec?: number;
};
export type AlphaFlowDropout = {
  days: number;
  stages: { status: string; label: string; advanced: number; rejected: number; total: number; reject_rate: number | null }[];
  generated_at: string;
};

export type IrBookResponse = {
  book_ir: number | null;
  annualized_return: number | null;
  max_drawdown: number | null;
  n_strategies: number;
  n_aligned_days: number;
  benchmark: 'btc' | 'none';
  empty: boolean;
  missing: number[];
  degraded: boolean;
};
export type IrByCategory = { rows: { category: string; ir: number | null; contribution: number; n_strategies: number; annualized_return: number | null }[]; benchmark: string };
export type IrByRegime = { rows: { regime: 'bull' | 'bear' | 'range'; ir: number | null; n_days: number }[]; available: boolean; benchmark: string; reason?: string };
export type IrByAsset = { rows: { asset: string; ir: number | null; n_strategies: number; weight: number }[]; benchmark: string };
export type IrRolling = { window: number; series: { date: string; ir: number | null }[]; benchmark: string };
export type IrWaterfall = { total_ir: number; steps: { label: string; delta: number; running: number }[]; benchmark: string };

export type PoConstraints = {
  max_weight?: number;
  min_weight?: number;
  allow_short?: boolean;
  vol_target_annual?: number;
  rebalance_freq?: 'D' | 'W' | 'M';
};
export type PoCombineReq = { strategy_ids: number[]; method: string; constraints?: PoConstraints };
export type PoComponent = { id: number; weight: number; mrc_pct: number; sharpe: number; vol: number };
export type PoCombineResp = {
  method: string;
  method_label: string;
  weights: Record<string, number>;
  equity_curve: EquityPoint[];
  metrics: Record<string, number>;
  n_strategies: number;
  n_aligned_days: number;
  missing: number[];
  marginal_risk_contribution: Record<string, number>;
  diversification_ratio: number | null;
  realized_vol_annual: number | null;
  vol_target_annual: number;
  vol_target_hit: boolean;
  components: PoComponent[];
  constraints: Record<string, unknown>;
};
export type PoSaveReq = { name: string; strategy_ids: number[]; weights: Record<string, number>; method: string; constraints?: Record<string, unknown> };
export type SavedPortfolio = {
  id: number;
  name: string;
  strategy_ids: number[];
  weights: Record<string, number>;
  method: string;
  constraints: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
  stale?: boolean;
};
export type PoFrontierPoint = { vol_target: number; realized_vol: number | null; expected_return: number; sharpe: number; weights: Record<string, number> };
export type PoFrontier = { points: PoFrontierPoint[]; steps: number; missing?: number[] };

export type FsEvalReq = { formula_code: string; asset_symbol?: string; period_start?: string; period_end?: string; timeout_seconds?: number };
export type FsEvalResult = {
  ic: number | null;
  sharpe: number;
  max_drawdown: number;
  win_rate: number;
  profit_factor: number;
  cumulative_return: number;
  equity_curve: EquityPoint[];
  monthly_returns: { year: number; month: number; ret: number }[];
  n_bars: number;
  elapsed_seconds: number;
  warnings: string[];
  asset_symbol: string;
};
export type FsSaveReq = { name: string; formula_code: string; asset_symbol?: string; params?: Record<string, unknown>; eval_result?: FsEvalResult; overwrite?: boolean };
export type SavedFactor = {
  id: number;
  name: string;
  formula_code: string;
  dsl_version: string;
  author: string;
  ic_score_cached: number;
  sharpe_cached: number;
  asset_symbol: string;
  period_start: string | null;
  period_end: string | null;
  params: Record<string, unknown>;
  promoted_strategy_id: number | null;
  created_at: string | null;
  updated_at: string | null;
};

export type AgTreeNode = {
  id: number;
  slug: string;
  name: string;
  stage: number;
  status: string;
  ic_score: number;
  sharpe: number | null;
  max_drawdown: number | null;
  created_at: string | null;
  updated_at: string | null;
  parent_id: number | null;
  postmortem_node_id: number | null;
  depth: number;
  badges: string[];
  children: AgTreeNode[];
};
export type AgForest = {
  trees: AgTreeNode[];
  stats: {
    n_roots: number;
    n_strategies: number;
    max_depth: number;
    n_fertile: number;
    n_improving: number;
    n_trapped: number;
    n_barren: number;
    n_cycle: number;
  };
};
export type AgLineage = { target: AgTreeNode | null; ancestors: AgTreeNode[]; siblings: AgTreeNode[]; descendants: AgTreeNode[] };

export type LiveTradeDashboard = {
  mode: 'paper' | 'live';
  base_ccy: string;
  positions: { strategy_id: number; slug: string; name: string; side: 'long' | 'short'; qty: number; entry_ts: string | null; entry_price: number; last_price: number; unrealized_pnl: number; unrealized_pnl_pct: number; holding_hours: number | null }[];
  pnl: { today_realized: number; today_unrealized: number; today_total: number; all_time_total: number; per_strategy: { sid: number; today: number; all_time: number }[] };
  recent_fills: { ts: string | null; strategy_id: number; slug: string; side: 'buy' | 'sell'; qty_delta: number; price: number; cash_delta: number }[];
  risk: { total_exposure_ccy: number; peak_equity: number; current_equity: number; drawdown_from_peak_pct: number; margin_used_ccy: number | null; var_95: number | null };
  exchange_status: { status: string; latency_ms: number | null; venue: string | null; note: string | null };
  strategies: { sid: number; slug: string; status: string; last_tick_at: string | null; latest_sharpe: number; ic_drift: number; last_run_age_seconds: number | null; is_healthy: boolean }[];
  /**
   * P17/C-M10 — when false, periodic paper-trade tick is OFF; Sharpe is frozen.
   */
  paper_tick_enabled?: boolean;
  generated_at: string;
};
export type AuditLogRow = {
  id: number;
  created_at: string | null;
  actor: string;
  action: string;
  subject_type: string | null;
  subject_id: string | null;
  payload: unknown;
  response: unknown;
  request_ip: string | null;
  user_agent: string | null;
  idempotency_key: string | null;
  success: boolean;
  error_text: string | null;
};

export type TtSymbol = {
  symbol: string; base: string; quote: string;
  min_qty: number; qty_step: number; price_tick: number;
  last: number; change_24h_pct: number; vol_24h: number;
  price_source: 'csv' | 'mock_synthetic';
};
export type TtMarketInfo = {
  symbol: string; last: number; bid: number; ask: number;
  spread_bps: number; vol_24h: number; change_24h_pct: number;
  ts: string; price_source: string; quote_stale?: boolean;
};
export type TtPosition = { symbol: string; mode: 'paper' | 'live'; qty_signed: number; avg_entry_price: number; realized_pnl_quote: number; last_update_at: string | null };
export type TtStatus = {
  enabled: boolean; live_enabled: boolean; mode_default: 'paper' | 'live';
  supported_symbols: string[];
  fee_bps: { maker: number; taker: number };
  rate_limit_per_min: number; fat_finger_pct: number;
  default_cash_usdt: number;
};
export type TtOrderReq = {
  symbol: string; side: 'buy' | 'sell';
  order_type: 'market' | 'limit' | 'stop';
  qty: number;
  limit_price?: number; stop_price?: number;
  tif?: 'gtc' | 'ioc' | 'fok'; mode?: 'paper' | 'live';
};
export type TtOrderPreview = {
  ok: boolean; validation: string[];
  est_cost_quote: number; est_fee_quote: number; est_fee_bps: number;
  est_slippage_bps: number; slippage_warning: boolean; fat_finger_warning: boolean;
  mark_price: number; mode: string;
};
export type TtOrder = {
  order_uid: string; symbol: string; side: string; order_type: string;
  qty: number; limit_price: number | null; stop_price: number | null;
  tif: string; mode: string; status: string;
  placed_by: string; notes: string | null;
  requested_at: string | null; decided_at: string | null;
};
export type TtFill = { id: number; order_id: number; order_uid?: string; filled_at: string | null; filled_qty: number; filled_price: number; fee_quote: number; fee_bps: number; is_maker: boolean; slippage_bps: number };
export type TtOrderSubmitResp = { order: TtOrder; fills: TtFill[]; position: TtPosition | null; idempotent_replay: boolean };
export type TtCancelAllResp = { cancelled_count: number; cancelled_order_uids: string[]; skipped: number; idempotent_replay: boolean };

export { cryptoUuid };
