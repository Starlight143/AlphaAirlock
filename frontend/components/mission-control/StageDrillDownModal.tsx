'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import clsx from 'clsx';
import { X, ExternalLink, Play, Loader2, ChevronDown, ChevronUp } from 'lucide-react';
import {
  api,
  type AlphaStrategy,
  type PipelineBucket,
  type PipelineBucketV2,
} from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { stageBadges } from './stageBadges';

/**
 * F3 — accepts both V1 and V2 buckets. V2 brings `sub_pills` which we
 * intentionally ignore here (the bucket itself already filters by all
 * underlying statuses), but the structural superset means the parent can
 * pass either shape without explicit narrowing.
 */
type AnyBucket = PipelineBucket | PipelineBucketV2;

type Props = {
  bucket: AnyBucket | null;
  onClose: () => void;
};

type SortKey = 'date' | 'sharpe' | 'name';

function readActor(s: AlphaStrategy): string {
  // strategy.config is `Record<string, unknown>`; tolerate any shape.
  const raw = (s.config as Record<string, unknown> | undefined)?.actor;
  if (typeof raw === 'string' && raw.trim()) return raw.trim();
  return 'system';
}

/**
 * Modal opened by clicking a Strategy Pipeline bucket on Mission Control
 * (P5-FE-16 / F3 refresh).
 *
 * F3 deltas:
 *   - Title format changed to `Stage {idx} — {label}` (en-dash separator,
 *     reference UI parity).
 *   - New "Actor" filter chip dropdown built from unique `s.config.actor`
 *     values (falls back to "system" when absent) inside the bucket subset.
 */
export default function StageDrillDownModal({ bucket, onClose }: Props) {
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState<SortKey>('date');
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [actorFilter, setActorFilter] = useState<string>('all');
  // P11-F2-04 — track per-row expansion state for the inline drilldown.
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());
  const toggleRow = (id: number) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // F14-1 fix: track backdrop-initiated press via a React ref instead of a
  // mutable DOM property. A ref survives re-renders without risk of the
  // custom property being orphaned on a stale DOM node or left dirty when
  // stopPropagation on the inner div intercepts the mouseup before the
  // backdrop handler fires.
  const backdropMouseDownRef = useRef(false);

  const qc = useQueryClient();
  const sq = useQuery({
    queryKey: queryKeys.strategies,
    queryFn: api.strategies,
    enabled: !!bucket,
    refetchInterval: bucket ? 10_000 : false,
  });

  // P16 A-M2 — track *which* row is currently re-cloning so the
  // spinner + disabled state apply only to that button. The earlier
  // `rerun.isPending` was shared across every row in the list,
  // making the whole table appear frozen during one click.
  const [pendingId, setPendingId] = useState<number | null>(null);
  const [rerunErrors, setRerunErrors] = useState<Record<number, string>>({});
  const rerun = useMutation({
    mutationFn: (s: AlphaStrategy) =>
      api.pipelineRun(s.formula_code || s.name || '', undefined),
    onMutate: (s: AlphaStrategy) => {
      setPendingId(s.id);
      setRerunErrors((prev) => {
        if (!(s.id in prev)) return prev;
        const { [s.id]: _drop, ...rest } = prev;
        return rest;
      });
    },
    onSettled: () => {
      setPendingId(null);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.strategies });
      qc.invalidateQueries({ queryKey: queryKeys.pipelineBuckets });
      qc.invalidateQueries({ queryKey: queryKeys.pipelineBucketsV2 });
    },
    onError: (e, s) => {
      setRerunErrors((prev) => ({
        ...prev,
        [s.id]: e instanceof Error ? e.message : String(e),
      }));
    },
  });

  // F14-5 — ref for the dialog panel used for focus-on-open and Tab trap.
  const panelRef = useRef<HTMLDivElement>(null);

  // Close on Escape.
  useEffect(() => {
    if (!bucket) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [bucket, onClose]);

  // F14-5 — move keyboard focus into the modal when it opens so that
  // aria-modal="true" is not contradicted by focus remaining on the
  // pill button that triggered the open.
  useEffect(() => {
    if (bucket) {
      panelRef.current?.focus();
    }
  }, [bucket]);

  // Pre-filter the strategies in this bucket so the actor dropdown only
  // surfaces values that are actually selectable.
  const inBucket = useMemo(() => {
    if (!bucket || !sq.data) return [] as AlphaStrategy[];
    const allowed = new Set(bucket.statuses.map((x) => x.toUpperCase()));
    return (sq.data.strategies ?? []).filter((s) =>
      allowed.has((s.status || '').toUpperCase()),
    );
  }, [bucket, sq.data]);

  const actorOptions = useMemo(() => {
    const set = new Set<string>();
    for (const s of inBucket) set.add(readActor(s));
    return ['all', ...Array.from(set).sort((a, b) => a.localeCompare(b))];
  }, [inBucket]);

  // Reset filters when the bucket changes so stale selections don't hide rows.
  useEffect(() => {
    if (bucket) {
      setStatusFilter('all');
      setActorFilter('all');
      setSearch('');
      setSort('date');
      setExpandedIds(new Set());
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional: only re-reset filters when the bucket key changes, not on every setter identity change
  }, [bucket?.key]);

  const visible = useMemo(() => {
    if (!bucket) return [] as AlphaStrategy[];
    let rows = inBucket;
    if (search.trim()) {
      const needle = search.trim().toLowerCase();
      rows = rows.filter(
        (s) =>
          (s.name || '').toLowerCase().includes(needle) ||
          (s.slug || '').toLowerCase().includes(needle) ||
          `s#${s.id}`.includes(needle),
      );
    }
    if (statusFilter !== 'all') {
      rows = rows.filter((s) => (s.status || '').toUpperCase() === statusFilter);
    }
    if (actorFilter !== 'all') {
      rows = rows.filter((s) => readActor(s) === actorFilter);
    }
    rows = rows.slice().sort((a, b) => {
      if (sort === 'sharpe') {
        const A = Number(a.metrics?.annualized_sharpe) || 0;
        const B = Number(b.metrics?.annualized_sharpe) || 0;
        return B - A;
      }
      if (sort === 'name') {
        return (a.name || '').localeCompare(b.name || '');
      }
      const A = a.updated_at || '';
      const B = b.updated_at || '';
      return B.localeCompare(A);
    });
    return rows;
  }, [bucket, inBucket, search, statusFilter, actorFilter, sort]);

  if (!bucket) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 backdrop-blur-sm"
      onMouseDown={(e) => {
          // Record that the press started on the backdrop itself (not on the
          // inner panel). e.target === e.currentTarget ensures drags that
          // originate inside the modal panel never set the flag even if they
          // drift outside before mouseup.
          backdropMouseDownRef.current = e.target === e.currentTarget;
        }}
      onMouseUp={(e) => {
          if (e.target === e.currentTarget && backdropMouseDownRef.current) {
            backdropMouseDownRef.current = false;
            onClose();
          }
        }}
    >
      <div
        ref={panelRef}
        // F14-5 — tabIndex={-1} makes the container programmatically focusable
        // without inserting it into the natural Tab order. Combined with the
        // focus-on-open useEffect above and the Tab trap below, this satisfies
        // the contract implied by aria-modal="true".
        tabIndex={-1}
        className="flex max-h-[85vh] w-[760px] max-w-[95vw] flex-col overflow-hidden rounded-xl border border-slate-700 bg-slate-900 shadow-2xl outline-none"
        onMouseDown={(e) => { backdropMouseDownRef.current = false; e.stopPropagation(); }}
        onMouseUp={(e) => { backdropMouseDownRef.current = false; e.stopPropagation(); }}
        // F14-5 — Tab-trap: cycle focus among the panel's focusable children so
        // keyboard operators cannot tab out while the modal is open.
        onKeyDown={(e) => {
          if (e.key !== 'Tab') return;
          const panel = panelRef.current;
          if (!panel) return;
          const focusable = Array.from(
            panel.querySelectorAll<HTMLElement>(
              'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])',
            ),
          ).filter((el) => !el.closest('[hidden]'));
          if (focusable.length === 0) { e.preventDefault(); return; }
          const first = focusable[0];
          const last = focusable[focusable.length - 1];
          if (e.shiftKey) {
            if (document.activeElement === first) { e.preventDefault(); last.focus(); }
          } else {
            if (document.activeElement === last) { e.preventDefault(); first.focus(); }
          }
        }}
        role="dialog"
        aria-modal="true"
        aria-labelledby="stage-drilldown-title"
      >
        <header className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
          <div>
            <h2 id="stage-drilldown-title" className="text-sm font-bold tracking-widest text-slate-100">
              Stage {bucket.index}{' '}
              <span className="mx-1 text-slate-500">—</span>{' '}
              {bucket.label}
            </h2>
            <div className="mt-0.5 text-[10px] text-slate-500">
              {bucket.count} strategies · statuses: {bucket.statuses.join(', ')}
            </div>
          </div>
          <button
            onClick={onClose}
            className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-slate-100"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        {/* Filter chip toolbar */}
        <div className="flex flex-wrap items-center gap-2 border-b border-slate-800 bg-slate-950/30 px-4 py-2">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by name / slug / S#id…"
            className="w-56 rounded-md border border-slate-700 bg-slate-950 px-2 py-1 text-[11px] text-slate-100 outline-none focus:border-cyan-600"
          />
          <Select
            label="Status"
            value={statusFilter}
            onChange={setStatusFilter}
            options={['all', ...bucket.statuses.map((x) => x.toUpperCase())]}
          />
          <Select
            label="Actor"
            value={actorFilter}
            onChange={setActorFilter}
            options={actorOptions}
          />
          <Select
            label="Sort"
            value={sort}
            onChange={(v) => setSort(v as SortKey)}
            options={['date', 'sharpe', 'name']}
          />
          <span className="ml-auto flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-slate-500">
            {visible.length} matching
            {/* F14-7 — stale-data indicator: when no filters are active, the
                client-side inBucket count should equal bucket.count (backend).
                A divergence means the strategies list (10 s refetch) is out of
                step with the buckets list (4 s refetch). Surface a subtle hint
                so the operator knows the discrepancy is transient, not a bug. */}
            {!sq.isLoading &&
              !sq.isFetching &&
              search.trim() === '' &&
              statusFilter === 'all' &&
              actorFilter === 'all' &&
              inBucket.length !== bucket.count && (
                <span
                  className="rounded border border-amber-700/40 bg-amber-500/10 px-1 py-0.5 text-[8px] font-bold uppercase tracking-wider text-amber-400"
                  title={`Local list shows ${inBucket.length} strategies but the pipeline header reports ${bucket.count}. The two data sources refresh at different intervals (strategies: 10 s, buckets: 4 s) — the discrepancy will resolve on the next strategy refetch.`}
                >
                  may be stale
                </span>
              )}
          </span>
        </div>

        <div className="flex-1 overflow-y-auto">
          {sq.isLoading ? (
            <div className="flex h-32 items-center justify-center text-xs text-slate-500">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              Loading strategies…
            </div>
          ) : visible.length === 0 ? (
            <div className="p-8 text-center text-xs text-slate-500">
              No strategies in this stage.
            </div>
          ) : (
            <ul className="divide-y divide-slate-800">
              {visible.map((s) => {
                const actor = readActor(s);
                const sharpe = Number(s.metrics?.annualized_sharpe);
                const isExpanded = expandedIds.has(s.id);
                return (
                  <li key={s.id}>
                    <div className="grid grid-cols-12 items-center gap-3 px-4 py-2 hover:bg-slate-950/40">
                      <div className="col-span-9">
                        <div className="flex items-center gap-2 font-mono text-xs">
                          {/* P11-F2-04 — expand/collapse chevron */}
                          <button
                            onClick={() => toggleRow(s.id)}
                            className="rounded p-0.5 text-slate-500 hover:bg-slate-800 hover:text-slate-200"
                            aria-label={isExpanded ? 'Collapse row' : 'Expand row'}
                            title={isExpanded ? 'Collapse details' : 'Expand details'}
                          >
                            {isExpanded ? (
                              <ChevronUp className="h-3.5 w-3.5" />
                            ) : (
                              <ChevronDown className="h-3.5 w-3.5" />
                            )}
                          </button>
                          <span className="text-cyan-300">S#{s.id}</span>
                          {/* P11-F2-06 — SR badge next to ID */}
                          {Number.isFinite(sharpe) && <SrBadge sharpe={sharpe} />}
                          <span className="truncate text-slate-200">
                            {s.slug ?? s.name}
                          </span>
                        </div>
                        <div className="mt-1 flex flex-wrap items-center gap-1">
                          {stageBadges(s).map((b, i) => (
                            <span
                              key={i}
                              className={clsx(
                                'rounded border px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider',
                                b.classes,
                              )}
                            >
                              {b.text}
                            </span>
                          ))}
                          <span
                            className="rounded border border-slate-700 bg-slate-900 px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider text-slate-300"
                            title="Author / actor that initiated this strategy."
                          >
                            {actor}
                          </span>
                          <span className="text-[10px] text-slate-500">
                            {s.status} ·{' '}
                            {(s.updated_at || '').slice(0, 16).replace('T', ' ')}
                          </span>
                        </div>
                      </div>
                      <div className="col-span-3 flex items-center justify-end gap-2">
                        <Link
                          href={`/strategies/${s.id}`}
                          onClick={onClose}
                          className="inline-flex items-center gap-1 rounded border border-cyan-700/40 bg-cyan-500/10 px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-cyan-200 hover:bg-cyan-500/20"
                        >
                          <ExternalLink className="h-3 w-3" /> Open
                        </Link>
                        {(() => {
                          // P16 A-M12 — disable when there is no formula_code
                          // and no name to seed Intake with. Submitting an
                          // empty/literal-"rerun" raw_text caused Intake to
                          // hallucinate a story from the placeholder string.
                          const reclonable = !!(s.formula_code || s.name);
                          const isPending = pendingId === s.id;
                          return (
                            <button
                              onClick={() => {
                                if (!reclonable) return;
                                rerun.mutate(s);
                              }}
                              disabled={!reclonable || isPending}
                              title={
                                reclonable
                                  ? "Re-clone — feeds this strategy's formula code (or name) back into Intake as a new raw_text seed. Note: backend has no dedicated re-run endpoint, so the Intake agent will re-extract a story from the formula source. Use 'Open' to inspect the original instead."
                                  : 'Re-clone unavailable — this strategy has no formula or name to seed Intake with. Use Open to inspect the original instead.'
                              }
                              className="inline-flex items-center gap-1 rounded border border-amber-700/40 bg-amber-500/10 px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-amber-200 hover:bg-amber-500/20 disabled:cursor-not-allowed disabled:opacity-40"
                            >
                              {isPending ? (
                                <Loader2 className="h-3 w-3 animate-spin" />
                              ) : (
                                <Play className="h-3 w-3" />
                              )}
                              Re-clone
                            </button>
                          );
                        })()}
                      </div>
                    </div>
                    {isExpanded && <ExpandedRow id={s.id} />}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

function Select({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: string[];
}) {
  return (
    <label className="flex items-center gap-1 text-[10px] uppercase tracking-widest text-slate-400">
      {label}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-md border border-slate-700 bg-slate-950 px-2 py-1 text-[11px] text-slate-200 outline-none focus:border-cyan-600"
      >
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

/**
 * P11-F2-06 — Sharpe-ratio chip with colour banding.
 *   >= 1.5  emerald  (institutional grade)
 *   >= 0.8  amber    (acceptable)
 *   else    slate    (sub-threshold)
 */
function SrBadge({ sharpe }: { sharpe: number }) {
  let tone = 'border-slate-700 bg-slate-900 text-slate-400';
  if (sharpe >= 1.5) {
    tone = 'border-emerald-700/60 bg-emerald-500/10 text-emerald-300';
  } else if (sharpe >= 0.8) {
    tone = 'border-amber-700/60 bg-amber-500/10 text-amber-300';
  }
  return (
    <span
      className={clsx(
        'shrink-0 rounded border px-1 py-0.5 font-mono text-[9px] font-bold uppercase tracking-wider',
        tone,
      )}
      title={`Annualised Sharpe ratio = ${sharpe.toFixed(3)}`}
    >
      SR={sharpe.toFixed(2)}
    </span>
  );
}

/**
 * P11-F2-04 — inline expansion panel showing formula, alpha story, and critic
 * verdict for the selected strategy without leaving the modal. Lazy-fetches
 * the strategy detail on first expand and reuses the cache afterwards.
 */
function ExpandedRow({ id }: { id: number }) {
  const q = useQuery({
    queryKey: queryKeys.strategy(id),
    queryFn: () => api.strategy(id),
    staleTime: 30_000,
  });

  if (q.isLoading) {
    return (
      <div className="flex items-center gap-2 border-t border-slate-800 bg-slate-950/60 px-4 py-2 font-mono text-[10px] text-slate-500">
        <Loader2 className="h-3 w-3 animate-spin" /> Loading details…
      </div>
    );
  }
  if (q.isError || !q.data) {
    return (
      <div className="border-t border-slate-800 bg-slate-950/60 px-4 py-2 font-mono text-[10px] text-rose-400">
        Failed to load details.
      </div>
    );
  }

  const strat = q.data;
  const cfg = (strat.config as Record<string, unknown> | undefined) ?? {};

  const formulaRaw =
    (strat.formula_code as string | undefined) ||
    (cfg.formula as string | undefined) ||
    '';
  const formulaLine =
    formulaRaw
      .split('\n')
      .map((l) => l.trim())
      .find((l) => l.length > 0) || '';

  const storyRaw = (cfg.alpha_story as string | undefined) || '';
  const storyLines = storyRaw
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l.length > 0);

  const criticRaw =
    (cfg.critic_verdict as string | undefined) ||
    (cfg.critic_review as string | undefined) ||
    '';
  const criticLines = criticRaw
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l.length > 0);

  // P12 A-H4 — orchestrator persists 6 Critic soul-question responses under
  // config.critic_soul_questions (see orchestrator.py:679,921). Surface them
  // here so the inline drilldown matches the reference UI's "why does this
  // alpha work" decomposition.
  const soulRaw =
    (cfg.critic_soul_questions as Record<string, unknown> | undefined) ?? {};
  const soulRows = SOUL_KEYS.map((k) => {
    const v = soulRaw[k];
    const txt = typeof v === 'string' ? v.trim() : '';
    return { key: k, label: SOUL_LABELS[k], text: txt };
  }).filter((r) => r.text.length > 0);

  const hasAny =
    formulaLine.length > 0 ||
    storyLines.length > 0 ||
    criticLines.length > 0 ||
    soulRows.length > 0;

  return (
    <div className="space-y-2 border-t border-slate-800 bg-slate-950/60 px-4 py-2 font-mono text-[10px] leading-relaxed text-slate-300">
      {formulaLine && (
        <div>
          <div className="text-[9px] font-bold uppercase tracking-widest text-cyan-400">
            Formula
          </div>
          <div className="whitespace-pre-wrap break-all text-emerald-300">
            {formulaLine}
          </div>
        </div>
      )}
      {storyLines.length > 0 && (
        <div>
          <div className="text-[9px] font-bold uppercase tracking-widest text-cyan-400">
            Alpha Story
          </div>
          <ul className="max-h-32 space-y-0.5 overflow-y-auto text-slate-200">
            {storyLines.map((l, i) => (
              <li key={i} className="whitespace-pre-wrap break-all">
                {l}
              </li>
            ))}
          </ul>
        </div>
      )}
      {criticLines.length > 0 && (
        <div>
          <div className="text-[9px] font-bold uppercase tracking-widest text-cyan-400">
            Critic Verdict
          </div>
          <ul className="max-h-32 space-y-0.5 overflow-y-auto text-amber-200">
            {criticLines.map((l, i) => (
              <li key={i} className="whitespace-pre-wrap break-all">
                {l}
              </li>
            ))}
          </ul>
        </div>
      )}
      {soulRows.length > 0 && (
        <div>
          <div className="text-[9px] font-bold uppercase tracking-widest text-cyan-400">
            Soul Questions
          </div>
          <ul className="max-h-48 space-y-1 overflow-y-auto text-slate-200">
            {soulRows.map((r) => (
              <li key={r.key} className="whitespace-pre-wrap break-all">
                <span className="font-bold text-cyan-300">{r.label}:</span>{' '}
                {r.text}
              </li>
            ))}
          </ul>
        </div>
      )}
      {!hasAny && (
        <div className="text-slate-500">No expanded detail available.</div>
      )}
    </div>
  );
}

// P12 A-H4 — labels for the 6 Critic soul-question fields persisted by the
// orchestrator under config.critic_soul_questions.
// Keys and labels aligned with CriticVerdict.tsx SOUL_QUESTION_ORDER so both
// components surface the same questions for the same backend data.
const SOUL_KEYS = [
  'q1_why_works',
  'q2_what_kills',
  'q3_counterparty',
  'q4_simple_explanation',
  'q5_data_availability',
  'q6_alpha_decay',
] as const;
type SoulKey = (typeof SOUL_KEYS)[number];
const SOUL_LABELS: Record<SoulKey, string> = {
  q1_why_works: 'Q1 · Why does it work?',
  q2_what_kills: 'Q2 · What would kill it?',
  q3_counterparty: 'Q3 · Who is the counterparty?',
  q4_simple_explanation: 'Q4 · Simple explanation',
  q5_data_availability: 'Q5 · Data availability',
  q6_alpha_decay: 'Q6 · Alpha decay speed',
};
