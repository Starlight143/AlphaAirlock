'use client';

import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  CheckCircle2, XCircle, AlertTriangle, Minus,
  Pencil, Save, X, RotateCcw, Loader2,
} from 'lucide-react';
import { api, type GateCriteriaRule } from '@/lib/api';
import { queryKeys } from '@/lib/query';

/**
 * BG PIPELINE GATE CRITERIA — right-rail panel listing the rules that gate
 * promotion past Stage 4. Each rule shows live pass/total counts pulled from
 * /api/gate-criteria.
 *
 * P-EDIT — operators can retune the numeric thresholds inline (Edit → number
 * inputs → Save). Overrides persist server-side (storage/gate_criteria_
 * overrides.json) and only affect THIS checklist's evaluation — the Critic's
 * hard-rejection floor lives in backend thresholds and is unaffected.
 */
export default function GateCriteria() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  // Draft of operator-facing DISPLAY values, keyed by rule.key. Populated when
  // entering edit mode so live refetches never clobber in-progress edits.
  const [draft, setDraft] = useState<Record<string, number>>({});
  const [error, setError] = useState<string | null>(null);

  const q = useQuery({
    queryKey: queryKeys.gateCriteria,
    queryFn: api.gateCriteria,
    // Pause polling while editing so the inputs don't fight a background refetch.
    refetchInterval: editing ? false : 10_000,
  });

  const rules = useMemo<GateCriteriaRule[]>(() => q.data?.rules ?? [], [q.data]);

  const save = useMutation({
    mutationFn: () => {
      // Send only the rules whose display value actually changed.
      const changed = rules
        .filter((r) => r.editable && draft[r.key] !== undefined && draft[r.key] !== r.editable.value)
        .map((r) => ({ key: r.key, value: draft[r.key] }));
      if (changed.length === 0) return Promise.resolve(null);
      return api.gateCriteriaUpdate(changed);
    },
    onSuccess: () => {
      setEditing(false);
      setError(null);
      qc.invalidateQueries({ queryKey: queryKeys.gateCriteria });
    },
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  });

  const reset = useMutation({
    mutationFn: () => api.gateCriteriaReset(),
    onSuccess: () => {
      setEditing(false);
      setDraft({});
      setError(null);
      qc.invalidateQueries({ queryKey: queryKeys.gateCriteria });
    },
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  });

  const beginEdit = () => {
    // Seed the draft from the current persisted display values.
    const seed: Record<string, number> = {};
    for (const r of rules) if (r.editable) seed[r.key] = r.editable.value;
    setDraft(seed);
    setError(null);
    setEditing(true);
  };

  const cancelEdit = () => {
    setEditing(false);
    setDraft({});
    setError(null);
  };

  const setVal = (key: string, v: number) =>
    setDraft((prev) => ({ ...prev, [key]: v }));

  const busy = save.isPending || reset.isPending;

  return (
    <div className="flex flex-col rounded-xl border border-slate-800 bg-slate-900/40">
      <header className="flex items-center justify-between gap-2 border-b border-slate-800 px-4 py-2">
        <h2 className="text-[10px] font-bold uppercase tracking-[0.2em] text-slate-300">
          BG Pipeline Gate Criteria
        </h2>
        <div className="flex items-center gap-2">
          {!editing ? (
            <>
              <span className="text-[9px] text-slate-500">{rules.length} rules</span>
              <button
                type="button"
                onClick={beginEdit}
                disabled={rules.length === 0}
                title="Edit threshold values"
                className="inline-flex items-center gap-1 rounded border border-slate-700 bg-slate-900 px-2 py-0.5 text-[9px] font-bold uppercase tracking-wider text-slate-300 hover:border-cyan-700 hover:text-cyan-200 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <Pencil className="h-3 w-3" /> Edit
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={() => reset.mutate()}
                disabled={busy}
                title="Reset all thresholds to backend defaults"
                className="inline-flex items-center gap-1 rounded border border-slate-700 bg-slate-900 px-2 py-0.5 text-[9px] font-bold uppercase tracking-wider text-slate-400 hover:border-amber-700 hover:text-amber-200 disabled:opacity-40"
              >
                {reset.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <RotateCcw className="h-3 w-3" />}
                Reset
              </button>
              <button
                type="button"
                onClick={cancelEdit}
                disabled={busy}
                className="inline-flex items-center gap-1 rounded border border-slate-700 bg-slate-900 px-2 py-0.5 text-[9px] font-bold uppercase tracking-wider text-slate-400 hover:text-slate-200 disabled:opacity-40"
              >
                <X className="h-3 w-3" /> Cancel
              </button>
              <button
                type="button"
                onClick={() => save.mutate()}
                disabled={busy}
                className="inline-flex items-center gap-1 rounded border border-cyan-700 bg-cyan-500/15 px-2 py-0.5 text-[9px] font-bold uppercase tracking-wider text-cyan-200 hover:bg-cyan-500/25 disabled:opacity-40"
              >
                {save.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />}
                Save
              </button>
            </>
          )}
        </div>
      </header>

      {error && (
        <div className="border-b border-rose-800/60 bg-rose-500/10 px-4 py-1.5 text-[10px] text-rose-300">
          {error}
        </div>
      )}
      {editing && (
        <div className="border-b border-slate-800 bg-slate-950/40 px-4 py-1.5 text-[9px] text-slate-500">
          Tunes this checklist&apos;s evaluation only — the Critic&apos;s hard-rejection floor is configured separately in the backend.
        </div>
      )}

      <ul className="p-2">
        {rules.map((r) => (
          <Row
            key={r.key}
            rule={r}
            editing={editing}
            draftValue={draft[r.key]}
            onChange={(v) => setVal(r.key, v)}
          />
        ))}
        {rules.length === 0 && (
          <li className="px-3 py-2 text-xs text-slate-500">
            {q.isLoading
              ? 'Loading criteria…'
              : q.isError
              ? <span className="text-rose-400">Failed to load gate criteria.</span>
              : 'No rules configured.'}
          </li>
        )}
      </ul>
    </div>
  );
}

function Row({
  rule,
  editing,
  draftValue,
  onChange,
}: {
  rule: GateCriteriaRule;
  editing: boolean;
  draftValue: number | undefined;
  onChange: (v: number) => void;
}) {
  const allPass = rule.total > 0 && rule.passed === rule.total;
  const noData = rule.total === 0;
  const Icon = allPass ? CheckCircle2 : rule.severity === 'blocker' ? XCircle : AlertTriangle;
  const color = noData
    ? 'text-slate-500'
    : allPass
    ? 'text-emerald-300'
    : rule.severity === 'blocker'
    ? 'text-rose-300'
    : 'text-amber-300';

  const ed = rule.editable;
  const value = draftValue ?? ed?.value ?? rule.threshold;

  return (
    <li className="flex items-start gap-2 rounded-md px-2 py-1.5 hover:bg-slate-900/60">
      <Icon className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${color}`} />
      <div className="flex-1 leading-tight">
        {editing && ed ? (
          <div className="flex items-center gap-1.5">
            {/* Stable name prefix derived from the label (label = "Name op value[unit]"). */}
            <span className="text-[11px] font-bold text-slate-200">
              {rule.label.replace(/\s*[<>]=?\s*-?[\d.]+%?\s*$/, '')}
            </span>
            <span className="font-mono text-[10px] text-slate-500">{rule.operator}</span>
            <input
              type="number"
              value={Number.isFinite(value) ? value : 0}
              step={ed.step}
              min={ed.min ?? undefined}
              max={ed.max ?? undefined}
              onChange={(e) => {
                const n = parseFloat(e.target.value);
                if (!Number.isNaN(n)) onChange(n);
              }}
              className="w-20 rounded border border-slate-600 bg-slate-950 px-1.5 py-0.5 text-right font-mono text-[11px] text-cyan-200 focus:border-cyan-500 focus:outline-none"
            />
            {ed.unit && <span className="font-mono text-[10px] text-slate-400">{ed.unit}</span>}
          </div>
        ) : (
          <div className="text-[11px] font-bold text-slate-200">{rule.label}</div>
        )}
        <div className="font-mono text-[9px] text-slate-500">
          {noData
            ? 'N/A'
            : `${rule.passed}/${rule.total} passing (${Number.isFinite(rule.ratio) ? (rule.ratio * 100).toFixed(0) : '—'}%)`}
        </div>
      </div>
      <span
        className={
          rule.severity === 'blocker'
            ? 'rounded border border-rose-700/60 bg-rose-500/10 px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider text-rose-300'
            : 'rounded border border-amber-700/60 bg-amber-500/10 px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider text-amber-300'
        }
      >
        {rule.severity}
      </span>
    </li>
  );
}
