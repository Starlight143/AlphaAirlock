'use client';

import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import {
  Skull,
  Sparkles,
  Target,
  Activity,
  FlaskConical,
  Code2,
  Brain,
  Lightbulb,
  HelpCircle,
  type LucideIcon,
} from 'lucide-react';
import { api, type PipelineBucket } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import StageDrillDownModal from './StageDrillDownModal';

/**
 * F3 — V1 8-bucket strategy pipeline row (Mission Control middle row).
 *
 * Aligns with backend /api/pipeline/buckets:
 *   0 Alpha Ideas → 1 Research → 2 Factor Dev → 3 Full Backtest →
 *   4 Paper Trade → 5 Small Capital → 6 Live → 7 Graveyard
 *
 * Extras vs the bare V1 row:
 *   (a) Stage 0 — overlays a three-layer queue counter sourced from
 *       /api/auto-pipeline/status (`pending / total (queue)`).
 *   (b) Every bucket has a thin progress bar at the bottom whose width is
 *       `bucket.count / total * 100%`, providing an at-a-glance distribution.
 *   (c) Inter-bucket separators are filled cyan SVG arrows instead of the
 *       previous thin chevron — matches the reference screenshots.
 */
const ICONS: Record<string, LucideIcon> = {
  alpha_ideas: Lightbulb,
  research: Brain,
  factor_dev: Code2,
  full_backtest: FlaskConical,
  paper_trade: Activity,
  small_capital: Target,
  live: Sparkles,
  graveyard: Skull,
  // V2-compat fallbacks (in case a stale V2 payload lands in cache):
  live_trade: Sparkles,
};

const ACCENTS: Record<string, string> = {
  alpha_ideas: '#22D3EE',
  research: '#22C55E',
  factor_dev: '#A855F7',
  full_backtest: '#3B82F6',
  paper_trade: '#F59E0B',
  small_capital: '#F97316',
  live: '#10B981',
  graveyard: '#6B7280',
  // V2-compat fallbacks:
  live_trade: '#10B981',
};

export default function PipelinePills() {
  const [drillBucket, setDrillBucket] = useState<PipelineBucket | null>(null);

  const bucketsQ = useQuery({
    queryKey: queryKeys.pipelineBuckets,
    queryFn: api.pipelineBuckets,
    refetchInterval: 4_000,
  });

  const autoQ = useQuery({
    queryKey: queryKeys.autoPipelineStatus,
    queryFn: api.autoPipelineStatus,
    refetchInterval: 6_000,
  });

  // P13 A-M5 — dedup `live` vs `live_trade`. The backend currently emits
  // one or the other (V1 = `live`, V2 = `live_trade`), but if both ever
  // appear in the same payload the second occurrence is dropped so we
  // never render duplicate "Live" cards in the row. `live` wins because
  // V1 is the canonical schema; if only `live_trade` exists it survives.
  const rawBuckets = bucketsQ.data?.buckets ?? [];
  const buckets = useMemo(() => {
    const out: typeof rawBuckets = [];
    const seen = new Set<string>();
    for (const b of rawBuckets) {
      const canon = b.key === 'live_trade' ? 'live' : b.key;
      if (seen.has(canon)) continue;
      seen.add(canon);
      out.push(b);
    }
    return out;
  }, [rawBuckets]);
  const total = bucketsQ.data?.total ?? 0;

  // P12 A-M2 — Stage 0 queue overlay shows `advanced/ingested (queue)` where
  //   advanced  = dispatched_today           (strategies that left Stage 0)
  //   ingested  = recent_triggers.length     (raw items the backend saw)
  //   queue     = advanced + ingested        (coarse total in-flight)
  // All three default to 0 so partial backend responses degrade gracefully.
  const stage0Queue = useMemo(() => {
    const auto = autoQ.data;
    const triggers = auto?.recent_triggers ?? [];
    const dispatched = Number(auto?.dispatched_today ?? 0);
    const ingested = triggers.length;
    return {
      advanced: dispatched,
      ingested,
      queue: dispatched + ingested,
    };
  }, [autoQ.data]);

  return (
    <section className="flex h-full flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/50">
      <header className="flex items-center justify-between border-b border-slate-800 px-4 py-2">
        <h2 className="text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
          Strategy Pipeline
        </h2>
        <span className="rounded border border-slate-800 px-2 py-0.5 text-[9px] font-bold tracking-wider text-cyan-300">
          [{total} STRATEG{total === 1 ? 'Y' : 'IES'}]
        </span>
      </header>

      <div className="flex flex-1 items-center gap-1 overflow-x-auto p-3">
        {buckets.length === 0 && bucketsQ.isLoading ? (
          <div className="flex w-full items-center justify-center text-xs text-slate-500">
            Loading pipeline buckets…
          </div>
        ) : (
          buckets.map((b, idx) => (
            <div key={b.key} className="flex items-center">
              <Bucket
                bucket={b}
                total={total}
                accent={ACCENTS[b.key] ?? '#64748B'}
                onClick={() => setDrillBucket(b)}
                stage0Queue={b.index === 0 ? stage0Queue : null}
              />
              {idx < buckets.length - 1 && <ArrowSep />}
            </div>
          ))
        )}
      </div>

      <StageDrillDownModal
        bucket={drillBucket}
        onClose={() => setDrillBucket(null)}
      />
    </section>
  );
}

function Bucket({
  bucket,
  total,
  accent,
  onClick,
  stage0Queue,
}: {
  bucket: PipelineBucket;
  total: number;
  accent: string;
  onClick: () => void;
  stage0Queue: { advanced: number; ingested: number; queue: number } | null;
}) {
  const Icon = ICONS[bucket.key] ?? Brain;
  const hot = bucket.count > 0;
  // Progress-bar share — identical rule for EVERY stage (Stage 0 included) so
  // the bar always matches the primary count shown below it. (Previously Stage
  // 0 scaled the bar by the auto-pipeline queue, which disagreed with the
  // number rendered on the pill.)
  const pct = total > 0 ? Math.min(100, (bucket.count / total) * 100) : 0;

  return (
    <button
      onClick={onClick}
      aria-label={`Stage ${bucket.index} — ${bucket.label} — ${bucket.count} strateg${bucket.count === 1 ? 'y' : 'ies'}`}
      title={`Stage ${bucket.index} — ${bucket.label} — ${bucket.count} strateg${bucket.count === 1 ? 'y' : 'ies'} — click to drill down`}
      className={clsx(
        'flex w-[112px] shrink-0 cursor-pointer flex-col gap-1 rounded-lg border bg-slate-950/60 p-2 text-left transition hover:bg-slate-900',
        hot ? 'border-slate-700' : 'border-slate-900',
      )}
      style={hot ? { boxShadow: `inset 0 0 0 1px ${accent}33` } : undefined}
    >
      <div className="flex items-center gap-1.5">
        <Icon
          className="h-3 w-3 shrink-0"
          style={{ color: hot ? accent : '#475569' }}
        />
        <span className="text-[9px] font-bold uppercase tracking-wider text-slate-500">
          Stage {bucket.index}
        </span>
      </div>

      {/* Primary number = live count of strategies currently in this stage
          (matches the drill-down modal + every other pill). Stage 0 keeps the
          auto-pipeline throughput triple `advanced/ingested (queue)` as a small
          SECONDARY line so the inbox-flow signal survives — but it no longer
          REPLACES the count, which previously made a real backlog of INTAKE
          strategies render as a misleading "0/0 (0)". */}
      <div
        className="font-mono text-xl leading-none"
        style={{ color: hot ? accent : '#475569' }}
      >
        {bucket.count}
      </div>
      {stage0Queue ? (
        <div
          className="flex items-center gap-1 font-mono text-[9px] leading-none text-slate-500"
          title={`Auto-pipeline throughput — advanced/ingested (queue): advanced=${stage0Queue.advanced} (left Stage 0 today), ingested=${stage0Queue.ingested} (raw triggers seen), queue=${stage0Queue.queue} (total in-flight)`}
        >
          <span>{`${stage0Queue.advanced}/${stage0Queue.ingested} (${stage0Queue.queue})`}</span>
          <HelpCircle
            className="h-2.5 w-2.5 shrink-0 text-slate-600"
            aria-hidden="true"
          />
        </div>
      ) : null}

      <div className="text-[9px] uppercase tracking-wider text-slate-400">
        {bucket.label}
      </div>

      {/* (b) Thin progress bar — width proportional to bucket share. */}
      <div className="mt-1 h-1 w-full overflow-hidden rounded-full bg-slate-800/80">
        <div
          className="h-full rounded-full transition-all"
          style={{
            width: `${pct}%`,
            background: hot ? accent : '#334155',
            opacity: hot ? 0.85 : 0.4,
          }}
        />
      </div>
    </button>
  );
}

/**
 * (c) P12 A-L1 — open-stroke cyan arrow separator. Pure SVG so it renders
 * crisp at any zoom level and never depends on the user's fallback font for
 * the unicode glyph. Stroke-only (fill="none") matches the reference UI.
 */
function ArrowSep() {
  return (
    <svg
      className="mx-1 h-3.5 w-3.5 shrink-0 text-cyan-400"
      viewBox="0 0 14 14"
      fill="none"
      aria-hidden="true"
    >
      <path
        d="M2 7 H10 M7 3 L11 7 L7 11 Z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
    </svg>
  );
}
