// Derived per-strategy quality / origin tags for the Stage drill-down modal
// (P5-FE-16). Mirrors the reference UI's "[PROVEN SR-x.x] / manual /
// ic-stable" chips. Pure client-side — no new backend payload needed.

import type { AlphaStrategy } from '@/lib/api';
import { qualityLabel, qualityLabelClasses } from '@/lib/derive';

export type StageBadge = {
  text: string;
  classes: string;
};

export function stageBadges(s: AlphaStrategy): StageBadge[] {
  const out: StageBadge[] = [];

  // P11-F2-05 — transient/running state badge appears FIRST so the operator
  // can see in-flight strategies at a glance from the stage drill-down.
  const TRANSIENT = new Set([
    'INTAKE',
    'STORY_GEN',
    'CODE_GEN',
    'BACKTESTING',
    'CRITIC_LOOP',
  ]);
  if (TRANSIENT.has((s.status || '').toUpperCase())) {
    out.push({
      text: 'running',
      classes:
        'border-cyan-500/70 bg-cyan-500/15 text-cyan-200 animate-pulse',
    });
  }

  // Quality tier from Sharpe.
  const sharpe = Number(s.metrics?.annualized_sharpe);
  const ql = qualityLabel(Number.isFinite(sharpe) ? sharpe : null);
  if (ql.tier !== 'UNKNOWN') {
    out.push({
      text: `[${ql.text}]`,
      classes: qualityLabelClasses(ql.tone),
    });
  }

  // Origin: manual (no source_node_ids) vs ingest-derived.
  const sources = (s.config?.source_node_ids as number[] | undefined) ?? [];
  const fromChat = !!s.config?.extracted_from_chat;
  if (fromChat) {
    out.push({
      text: 'alpha-lab',
      classes: 'border-purple-700/60 bg-purple-500/10 text-purple-300',
    });
  } else if (sources.length === 0) {
    out.push({
      text: 'manual',
      classes: 'border-slate-700 bg-slate-900 text-slate-300',
    });
  }

  // ic-stable: tight drawdown + decent Sharpe.
  const dd = Number(s.metrics?.max_drawdown);
  if (
    Number.isFinite(sharpe) &&
    Number.isFinite(dd) &&
    sharpe >= 0.7 &&
    Math.abs(dd) <= 0.2
  ) {
    out.push({
      text: 'ic-stable',
      classes: 'border-cyan-700/60 bg-cyan-500/10 text-cyan-300',
    });
  }

  // Force-promoted bypass tag.
  if (s.config?.gate_force) {
    out.push({
      text: 'forced',
      classes: 'border-amber-700/60 bg-amber-500/10 text-amber-300',
    });
  }
  return out;
}
