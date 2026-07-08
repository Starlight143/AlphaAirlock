'use client';

import ReactMarkdown from 'react-markdown';
import { markdownComponents, remarkMathPlugins, rehypeMathPlugins } from '@/lib/markdownComponents';
import { ShieldCheck, AlertTriangle, AlertOctagon, Hourglass } from 'lucide-react';
import type { AlphaStrategy } from '@/lib/api';
import { classifyVerdict, pendingReason } from '@/lib/derive';

type Props = { strategy: AlphaStrategy };

// P8-FIX/H-4: ordered soul-question keys + friendly labels matching
// backend.agents.critic.SOUL_QUESTION_KEYS.
const SOUL_QUESTION_ORDER = [
  'q1_why_works',
  'q2_what_kills',
  'q3_counterparty',
  'q4_simple_explanation',
  'q5_data_availability',
  'q6_alpha_decay',
] as const;
const SOUL_LABELS: Record<string, string> = {
  q1_why_works: 'Q1 · Why does it work?',
  q2_what_kills: 'Q2 · What would kill it?',
  q3_counterparty: 'Q3 · Who is the counterparty?',
  q4_simple_explanation: 'Q4 · Simple explanation',
  q5_data_availability: 'Q5 · Data availability',
  q6_alpha_decay: 'Q6 · Alpha decay speed',
};

const SEVERITY_STYLES: Record<
  string,
  { bg: string; border: string; text: string; label: string }
> = {
  'NO OVERLAP': {
    bg: 'bg-emerald-500/15',
    border: 'border-emerald-700/60',
    text: 'text-emerald-300',
    label: 'NO OVERLAP',
  },
  'LOW OVERLAP': {
    bg: 'bg-cyan-500/15',
    border: 'border-cyan-700/60',
    text: 'text-cyan-300',
    label: 'LOW OVERLAP',
  },
  'MODERATE OVERLAP': {
    bg: 'bg-amber-500/15',
    border: 'border-amber-700/60',
    text: 'text-amber-300',
    label: 'MODERATE OVERLAP',
  },
  'HIGH OVERLAP': {
    bg: 'bg-orange-500/15',
    border: 'border-orange-700/60',
    text: 'text-orange-300',
    label: 'HIGH OVERLAP',
  },
  'SEVERE OVERLAP': {
    bg: 'bg-rose-500/15',
    border: 'border-rose-700/60',
    text: 'text-rose-300',
    label: 'SEVERE OVERLAP',
  },
};

/**
 * Renders the Team B Risk Critic output in the same visual style as the
 * reference: a verdict banner at top, severity chip, then markdown body
 * (Q1-Q6 + Formula Quality + Production Redundancy), then flag chips.
 */
export default function CriticVerdict({ strategy }: Props) {
  const status = strategy.status;
  const critique = strategy.team_b_review || '';
  const severityRaw = (strategy.config?.critic_severity_tag as string | undefined) || '';
  const violationTags = (strategy.config?.critic_violation_tags as string[] | undefined) || [];
  const flags =
    (strategy.config?.critic_flags as { type: string; message: string }[] | undefined) || [];
  const alphaCategory = (strategy.config?.alpha_category as string | undefined) || null;
  const confidence = strategy.config?.critic_confidence as number | undefined;
  // P6-M02: production-factor overlap names (when the critic emitted them).
  // Reads pre-existing config field if backend has been upgraded; otherwise
  // the section silently no-ops.
  const overlapFactors = (strategy.config?.critic_overlap_factors as string[] | undefined) || [];
  // P6-M13: structured Q1-Q6 "soul questions" with separate answers. Same
  // pattern — read-when-present, no error when absent.
  const soulQuestions =
    (strategy.config?.critic_soul_questions as Record<string, string> | undefined) || null;

  const sev = SEVERITY_STYLES[severityRaw] ?? null;

  return (
    <div className="space-y-3 text-slate-200">
      <VerdictBanner status={status} />

      <div className="flex flex-wrap items-center gap-2">
        {sev && (
          <span
            className={`rounded border px-2 py-0.5 text-[10px] font-bold tracking-wider ${sev.bg} ${sev.border} ${sev.text}`}
          >
            {sev.label}
          </span>
        )}
        {alphaCategory && (
          <span className="rounded border border-slate-700 bg-slate-900 px-2 py-0.5 text-[10px] font-bold tracking-wider text-slate-200">
            CATEGORY: {alphaCategory.toUpperCase()}
          </span>
        )}
        {typeof confidence === 'number' && Number.isFinite(confidence) && (
          <span className="rounded border border-slate-800 bg-slate-950 px-2 py-0.5 text-[10px] font-bold tracking-wider text-slate-400">
            CONFIDENCE {(confidence * 100).toFixed(0)}%
          </span>
        )}
        {violationTags.map((t) => (
          <span
            key={t}
            className="rounded border border-rose-700/60 bg-rose-500/10 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-rose-300"
          >
            {t}
          </span>
        ))}
      </div>

      {flags.length > 0 && (
        <div className="space-y-1 rounded-md border border-rose-700/40 bg-rose-500/5 p-2">
          {flags.map((f, i) => (
            <div key={i} className="flex items-start gap-2 text-[11px] text-rose-200">
              <AlertOctagon className="mt-0.5 h-3 w-3 shrink-0 text-rose-400" />
              <span>
                <span className="font-bold">[{f.type}]</span> {f.message}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* P6-M13 + P8-FIX/H-4: structured Soul Questions block.
          Friendly labels mirror the reference video's "Q1 why works / Q2 what
          kills / Q3 counterparty / Q4 simple explanation / Q5 data avail /
          Q6 alpha decay" headings, while still degrading gracefully for
          unknown keys (so the panel never blows up on schema drift). */}
      {soulQuestions && Object.values(soulQuestions).some((v) => v && String(v).trim()) && (
        <div className="space-y-2 rounded-md border border-slate-800 bg-slate-950/40 p-3">
          <div className="font-mono text-[10px] font-bold uppercase tracking-widest text-cyan-300">
            ## Soul Questions ##
          </div>
          <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
            {SOUL_QUESTION_ORDER.map((qid) => {
              const answer = (soulQuestions as Record<string, string>)[qid];
              if (!answer || !String(answer).trim()) return null;
              return (
                <div
                  key={qid}
                  className="rounded border border-slate-800 bg-slate-900/40 p-2"
                >
                  <div className="font-mono text-[9px] font-bold uppercase tracking-widest text-emerald-300">
                    {SOUL_LABELS[qid] ?? qid.toUpperCase()}
                  </div>
                  <div className="mt-1 text-[11px] leading-relaxed text-slate-200">
                    {answer}
                  </div>
                </div>
              );
            })}
            {/* Render any extra keys the backend might emit later. */}
            {Object.entries(soulQuestions)
              .filter(([qid]) => !SOUL_LABELS[qid])
              .map(([qid, answer]) =>
                !answer || !String(answer).trim() ? null : (
                  <div key={qid} className="rounded border border-slate-800 bg-slate-900/40 p-2">
                    <div className="font-mono text-[9px] font-bold uppercase tracking-widest text-emerald-300">
                      {qid.toUpperCase()}
                    </div>
                    <div className="mt-1 text-[11px] leading-relaxed text-slate-200">
                      {answer}
                    </div>
                  </div>
                ),
              )}
          </div>
        </div>
      )}

      {/* P6-M02: production-redundancy overlap factors as chip list. */}
      {overlapFactors.length > 0 && (
        <div className="space-y-1.5 rounded-md border border-amber-700/40 bg-amber-500/5 p-2">
          <div className="text-[10px] font-bold uppercase tracking-widest text-amber-300">
            Production factor overlap
          </div>
          <div className="flex flex-wrap gap-1.5">
            {overlapFactors.map((f) => (
              <span
                key={f}
                className="rounded border border-amber-700/40 bg-amber-500/10 px-1.5 py-0.5 font-mono text-[9px] text-amber-200"
              >
                {f}
              </span>
            ))}
          </div>
        </div>
      )}

      {critique ? (
        <article className="prose prose-invert prose-sm max-w-none text-slate-200 prose-headings:text-slate-100 prose-h3:text-[12px] prose-h3:font-bold prose-h3:tracking-wider prose-h3:uppercase prose-h3:text-cyan-300 prose-code:text-emerald-300">
          {/* P31-S4: pass safe markdownComponents to filter javascript:/data: hrefs (XSS defense). */}
          <ReactMarkdown components={markdownComponents} remarkPlugins={remarkMathPlugins} rehypePlugins={rehypeMathPlugins}>{critique}</ReactMarkdown>
        </article>
      ) : (
        <div className="text-xs text-slate-500">
          Critic agent has not produced a verdict yet for this strategy.
        </div>
      )}
    </div>
  );
}

// P8-FIX/H-5 — PASS / FAIL verdict pill (the reference video says
// "verdict pass" verbatim, so the primary chip must read PASS or FAIL).
// Sub-label preserves the existing risk-sanctioned / promoted / failed text
// so we don't lose the secondary status information.
function VerdictBanner({ status }: { status: string }) {
  const upper = (status || '').toUpperCase();
  const kind = classifyVerdict(status);
  if (kind === 'PASS') {
    const sub =
      upper === 'APPROVED'
        ? 'risk sanctioned'
        : `promoted → ${upper.toLowerCase()}`;
    return (
      <div className="flex items-center gap-3 rounded-lg border border-emerald-600 bg-emerald-500/15 px-4 py-2 text-emerald-300">
        <ShieldCheck className="h-5 w-5" />
        <span className="rounded-md border border-emerald-500/80 bg-emerald-500/25 px-2 py-0.5 text-xs font-bold tracking-widest">
          VERDICT: PASS
        </span>
        <span className="text-[11px] uppercase tracking-widest text-emerald-200/80">{sub}</span>
      </div>
    );
  }
  if (kind === 'FAIL') {
    const sub =
      upper === 'REJECTED'
        ? 'critic loop blocked promotion'
        : upper === 'GRAVEYARD'
          ? 'retired by operator'
          : 'paused — awaiting operator resume';
    return (
      <div className="flex items-center gap-3 rounded-lg border border-rose-600 bg-rose-500/20 px-4 py-2 text-rose-300">
        <AlertTriangle className="h-5 w-5" />
        <span className="rounded-md border border-rose-500/80 bg-rose-500/25 px-2 py-0.5 text-xs font-bold tracking-widest">
          VERDICT: FAIL
        </span>
        <span className="text-[11px] uppercase tracking-widest text-rose-200/80">{sub}</span>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-3 rounded-lg border border-slate-700 bg-slate-800/40 px-4 py-2 text-slate-400">
      <Hourglass className="h-5 w-5 animate-pulse" />
      <span className="rounded-md border border-slate-600/80 bg-slate-700/40 px-2 py-0.5 text-xs font-bold tracking-widest">
        VERDICT: PENDING
      </span>
      {/* C-M14 — render the phase-specific reason ("awaiting backtest" for
         BACKTESTING, etc.) instead of the misleading "awaiting critic —
         {STATUS}". Critic verdict isn't meaningful until the critic
         actually runs (Stage 3 CRITIC_LOOP). */}
      <span className="text-[11px] uppercase tracking-widest text-slate-500">
        {pendingReason(status)}
      </span>
    </div>
  );
}
