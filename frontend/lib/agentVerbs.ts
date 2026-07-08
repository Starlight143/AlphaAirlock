// P16 A-M7 — shared verb extraction for daemon-log lines so the
// AgentTeam rail and the HeaderBarV2 cycler never diverge in wording.
//
// Both call sites previously implemented near-identical keyword
// matching but returned different strings ("last: research" vs
// "researching"). Centralising the keyword table eliminates the drift
// risk and gives one place to extend the verb vocabulary.

import type { DaemonLogEvent } from './api';

type Verb =
  | 'research'
  | 'backtest'
  | 'review'
  | 'code'
  | 'ingest'
  | 'work';

const KEYWORDS: { match: RegExp; verb: Verb }[] = [
  { match: /research/i, verb: 'research' },
  { match: /backtest/i, verb: 'backtest' },
  { match: /critic|review/i, verb: 'review' },
  { match: /code|compile/i, verb: 'code' },
  { match: /ingest|collect/i, verb: 'ingest' },
];

const PRESENT: Record<Verb, string> = {
  research: 'researching',
  backtest: 'backtesting',
  review: 'reviewing',
  code: 'coding',
  ingest: 'ingesting',
  work: 'working',
};

const LAST: Record<Verb, string> = {
  research: 'last: research',
  backtest: 'last: backtest',
  review: 'last: review',
  code: 'last: code',
  ingest: 'last: ingest',
  work: 'last: work',
};

function classify(line: string): Verb {
  const l = line || '';
  for (const { match, verb } of KEYWORDS) {
    if (match.test(l)) return verb;
  }
  return 'work';
}

/**
 * Present-progressive verb for the *currently active* daemon line (e.g.
 * "researching"). Pass the most-recent line for an agent. Returns
 * "working" when no keyword matches.
 */
export function verbOfActive(line: string): string {
  return PRESENT[classify(line)];
}

/**
 * "last: <verb>" label for an *idle* agent's most-recent daemon
 * activity. Used by the AgentTeam card's right-edge chip when the
 * agent has no recent events in the polling window.
 */
export function verbOfLast(line: string): string {
  return LAST[classify(line)];
}

/**
 * Convenience: scan the given event list (most-recent first) for the
 * first hit on `agentKey` and return its `verbOfLast(...)` label.
 * Returns `'idle'` when the agent never appears in the window.
 */
export function lastVerbForAgent(
  events: DaemonLogEvent[],
  agentKey: string,
): string {
  for (const ev of events) {
    if ((ev.agent || '').trim() !== agentKey) continue;
    return verbOfLast(ev.line || '');
  }
  return 'idle';
}
