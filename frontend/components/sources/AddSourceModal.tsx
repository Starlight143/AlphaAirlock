'use client';

import { useEffect, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { X } from 'lucide-react';
import { api, cryptoUuid, type SourceType } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import {
  CATEGORY_LABELS,
  SOURCE_CATEGORIES,
  SOURCE_TYPE_LABELS,
  SOURCE_TYPE_PLACEHOLDERS,
  type SourceCategory,
} from '@/lib/sourceTypes';

type Props = {
  open: boolean;
  onClose: () => void;
  /** Called after the source is successfully created and the sources cache is invalidated. */
  onSuccess?: () => void;
};

// P13 A-H5 — alphabetical order so the dropdown is scannable. (The backend
// FETCHERS registry order was operator-meaningless; alphabetical matches
// every other dropdown in the app.) `arxiv` and `glassnode` are first-class
// fetchers and slot in naturally between `manual` and `medium`.
const SOURCE_TYPE_ORDER: SourceType[] = [
  'arxiv',
  'glassnode',
  'manual',
  'medium',
  'patreon',
  'reddit',
  'rss',
  'substack',
  'tiktok',
  'twitter_article',
  'twitter_tag',
  'youtube_video',
];

export default function AddSourceModal({ open, onClose, onSuccess }: Props) {
  const qc = useQueryClient();
  const [name, setName] = useState('');
  const [sourceType, setSourceType] = useState<SourceType>('rss');
  const [url, setUrl] = useState('');
  const [cadence, setCadence] = useState(60);
  // F8-3 fix: track the raw string the user types so the field never silently
  // rewrites their input mid-keystroke. Validation is deferred to submit time.
  const [cadenceRaw, setCadenceRaw] = useState('60');
  const [enabled, setEnabled] = useState(true);
  const [category, setCategory] = useState<SourceCategory | ''>('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setName('');
      setUrl('');
      setSourceType('rss');
      setCadence(60);
      setCadenceRaw('60');
      setEnabled(true);
      setCategory('');
      setError(null);
    }
  }, [open]);

  // P16 A-H3 — Escape key handler. The backdrop is closed via onClick
  // (added below) but Escape needs a window listener since the
  // backdrop div is not focusable.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  // F8-3 fix: mutationFn now accepts an explicit cadence_minutes value so that
  // the submit-time validated cadence is used instead of the potentially-stale
  // `cadence` state variable (React batches setState; calling mutate() in the
  // same tick as setCadence() would read the pre-update value).
  const create = useMutation({
    mutationFn: ({ cadenceMinutes }: { cadenceMinutes: number }) =>
      api.sourceCreate({
        name: name.trim(),
        source_type: sourceType,
        url: url.trim(),
        cadence_minutes: cadenceMinutes,
        enabled,
        category: category || null,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.sources });
      onSuccess?.();
      onClose();
    },
    onError: (e: unknown) => {
      setError(e instanceof Error ? e.message : String(e));
    },
  });

  if (!open) return null;
  const placeholder = SOURCE_TYPE_PLACEHOLDERS[sourceType] ?? '';
  // P12-B-L4 — manual sources are never polled by the scheduler, so the
  // cadence input is moot. Grey it out and surface a 1-liner explainer so
  // operators don't waste seconds tweaking a value that has no effect.
  const isManual = sourceType === 'manual';

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 backdrop-blur-sm"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-labelledby="add-source-title"
    >
      <div
        className="w-[640px] max-w-[92vw] rounded-xl border border-slate-700 bg-slate-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
          <h2 id="add-source-title" className="text-sm font-bold tracking-widest text-slate-100">
            ADD INGEST SOURCE
          </h2>
          <button
            onClick={onClose}
            className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-slate-100"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="space-y-4 p-5">
          <Field label="Name">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Glassnode Insights weekly"
              className="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs text-slate-100 outline-none focus:border-cyan-600"
            />
          </Field>
          <Field label="Source Type">
            <select
              value={sourceType}
              onChange={(e) => setSourceType(e.target.value as SourceType)}
              className="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs text-slate-100 outline-none focus:border-cyan-600"
            >
              {SOURCE_TYPE_ORDER.map((t) => (
                <option key={t} value={t}>
                  {SOURCE_TYPE_LABELS[t]}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Category (top tab — leave blank to auto-detect from URL)">
            <select
              value={category}
              onChange={(e) => setCategory(e.target.value as SourceCategory | '')}
              className="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs text-slate-100 outline-none focus:border-cyan-600"
            >
              <option value="">— auto —</option>
              {SOURCE_CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {CATEGORY_LABELS[c]}
                </option>
              ))}
            </select>
          </Field>
          <Field label="URL / Identifier">
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder={placeholder}
              className="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-xs text-slate-100 outline-none focus:border-cyan-600"
            />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Poll cadence (minutes)">
              <input
                type="number"
                min={5}
                max={60 * 24 * 7}
                value={cadenceRaw}
                onChange={(e) => {
                  // F8-3 fix: accept raw input as-is; no silent clamping or
                  // falsy-0 replacement. Validation runs on submit.
                  setCadenceRaw(e.target.value);
                }}
                disabled={isManual}
                className={`w-full rounded-md border px-3 py-1.5 font-mono text-xs outline-none focus:border-cyan-600 ${
                  isManual
                    ? 'cursor-not-allowed border-slate-800 bg-slate-900 text-slate-500'
                    : 'border-slate-700 bg-slate-950 text-slate-100'
                }`}
              />
              {isManual && (
                <div className="mt-1 text-[10px] text-slate-500">
                  Manual sources do not poll.
                </div>
              )}
            </Field>
            <Field label="Enabled">
              <div className="flex items-center gap-2 rounded-md border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs text-slate-200">
                <input
                  id="add-source-enabled"
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => setEnabled(e.target.checked)}
                  className="h-3 w-3 accent-cyan-500"
                />
                <label htmlFor="add-source-enabled" className="cursor-pointer select-none">
                  {enabled ? 'Enabled — will poll on next tick' : 'Disabled'}
                </label>
              </div>
            </Field>
          </div>

          {error && (
            <div className="rounded border border-rose-700 bg-rose-500/10 p-2 text-xs text-rose-300">
              {error}
            </div>
          )}

          <div className="flex justify-end gap-2 border-t border-slate-800 pt-3">
            <button
              onClick={onClose}
              disabled={create.isPending}
              className="rounded-md border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
            >
              Cancel
            </button>
            <button
              onClick={() => {
                setError(null);
                if (!name.trim() || (!url.trim() && !isManual)) {
                  setError('Name and URL are required.');
                  return;
                }
                // F8-3 fix: validate cadence at submit time with a clear
                // message instead of silently clamping during onChange.
                let resolvedCadence = cadence; // default for manual sources
                if (!isManual) {
                  const parsedCadence = Number(cadenceRaw);
                  if (
                    !Number.isFinite(parsedCadence) ||
                    !Number.isInteger(parsedCadence) ||
                    parsedCadence < 5 ||
                    parsedCadence > 60 * 24 * 7
                  ) {
                    setError('Poll cadence must be a whole number between 5 and 10,080 minutes.');
                    return;
                  }
                  resolvedCadence = parsedCadence;
                }
                create.mutate({ cadenceMinutes: resolvedCadence });
              }}
              disabled={create.isPending || !name.trim() || (!url.trim() && !isManual)}
              // P12-B-M4 — re-skin to purple so the primary action matches
              // the ADD SOURCE button on /sources (header CTA) instead of the
              // generic cyan accent used for read-only chips.
              className="rounded-md border border-purple-600 bg-purple-500/15 px-3 py-1.5 text-xs font-bold tracking-wide text-purple-200 hover:bg-purple-500/25 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {create.isPending ? 'Adding…' : 'ADD SOURCE'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="mb-1 text-[10px] font-bold uppercase tracking-wider text-slate-500">
        {label}
      </div>
      {children}
    </label>
  );
}
