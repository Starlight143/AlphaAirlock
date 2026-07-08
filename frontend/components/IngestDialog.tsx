'use client';

import { useEffect, useRef, useState } from 'react';
import { X, PlayCircle } from 'lucide-react';
import { api, cryptoUuid } from '@/lib/api';

type Props = {
  open: boolean;
  onClose: () => void;
  onLaunched: (strategyId: number) => void;
};

// P13 A-M4 — empty default with a language-neutral placeholder. The earlier
// BTC funding-rate seed was English-only and biased every cold-start session
// toward a specific hypothesis the operator hadn't asked for. Empty + RUN
// button stays disabled (text.trim().length < 10) until the operator types.
const DEFAULT_SEED = '';

export default function IngestDialog({ open, onClose, onLaunched }: Props) {
  const [text, setText] = useState(DEFAULT_SEED);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // P108 A-M4 — persistent idempotency key across close/reopen cycles.
  // Minted lazily on first submit; cleared only after the server confirms
  // success (onLaunched). If the dialog closes mid-flight and the user
  // reopens+resubmits the same text, the same key is sent so the server
  // returns an idempotent_replay response instead of spawning a second run.
  const idempotencyKeyRef = useRef<string | null>(null);

  // P14 A-H2 — reset transient dialog state whenever modal closes.
  useEffect(() => {
    if (!open) {
      setText(DEFAULT_SEED);
      setError(null);
      setBusy(false);
      // F7-3 fix: clear the idempotency key when the operator abandons the
      // dialog (closes without submitting, or closes after an error). The key
      // is intentionally kept alive only for retry within the same text-session.
      // Once text is reset to DEFAULT_SEED the old key must not be reused for
      // a genuinely different next submission — doing so risks the backend
      // returning idempotent_replay=true with the OLD text's pipeline result.
      // Successful submit already clears the key at line 84; this covers the
      // abandon/error-close path.
      idempotencyKeyRef.current = null;
    }
  }, [open]);

  // P16 A-H2 — Escape key handler. The backdrop is closed via onClick
  // (added below) but Escape needs a window listener since the
  // backdrop div is not focusable.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose, busy]);

  if (!open) return null;

  async function submit() {
    // P108 A-M4 — mint a new idempotency key only if we don't already hold
    // one for this submission attempt (i.e. first click, or after a prior
    // successful launch cleared it). Reusing the same key across close/reopen
    // cycles lets the server return idempotent_replay=true instead of
    // spawning a second pipeline run.
    if (!idempotencyKeyRef.current) {
      idempotencyKeyRef.current = cryptoUuid();
    }
    const idempotencyKey = idempotencyKeyRef.current;
    setBusy(true);
    setError(null);
    try {
      // Re-check LLM config right before sending so the user sees the actual
      // reason (rather than a generic 500 from the backend).
      const h = await api.health();
      if (!h.llm?.configured) {
        const envName = h.llm?.key_env_var ?? 'ANTHROPIC_API_KEY';
        const provider = h.llm?.resolved ?? '?';
        throw new Error(
          `LLM is not configured (provider=${provider}). Set ${envName} in .env at the project root, then restart the backend.`,
        );
      }
      const r = await api.pipelineRun(text.trim(), undefined, { idempotencyKey });
      // P16 A-L4 — single closure path. `onLaunched` is responsible
      // for closing the dialog (the parent maps it to the same
      // close+navigate handler). Calling onClose() here too caused a
      // double state update that briefly re-opened the dialog
      // before the navigation landed.
      // Clear the key only after confirmed server-side success so that
      // a subsequent invocation for a genuinely new text mints a fresh UUID.
      idempotencyKeyRef.current = null;
      onLaunched(r.strategy_id);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
      // Key is intentionally retained on failure so a retry (same dialog
      // session or after close/reopen) sends the same Idempotency-Key and
      // gets the server's cached error or triggers a safe retry path.
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 backdrop-blur-sm"
      onClick={() => { if (!busy) onClose(); }}
    >
      <div
        className="w-[640px] max-w-[92vw] rounded-xl border border-slate-700 bg-slate-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="ingest-dialog-title"
      >
        <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
          <h2 id="ingest-dialog-title" className="text-sm font-bold tracking-widest text-slate-100">INGEST RAW SOURCE</h2>
          <button
            onClick={onClose}
            className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-slate-100"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="space-y-3 p-5">
          <p className="text-xs text-slate-400">
            Paste any unstructured market commentary, research note, or anomaly observation.
            The Intake agent will extract a knowledge node and the full pipeline (Researcher
            → Coder → Backtester → Critic) will run in the background.
          </p>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            disabled={busy}
            placeholder="https://example.com/post  |  paste any market commentary / research note / URL (min 10 chars)"
            className="h-44 w-full resize-none rounded-lg border border-slate-700 bg-slate-950 p-3 text-xs text-slate-100 outline-none focus:border-emerald-600"
          />
          {text.trim().length < 10 && (
            <div className="text-right text-[10px] text-slate-500">
              {text.trim().length} / 10 characters
            </div>
          )}
          {error && (
            <div className="rounded border border-rose-700 bg-rose-500/10 p-2 text-xs text-rose-300">
              {error}
            </div>
          )}
          <div className="flex justify-end gap-2">
            <button
              onClick={onClose}
              disabled={busy}
              className="rounded-md border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
            >
              Cancel
            </button>
            <button
              onClick={submit}
              disabled={busy || text.trim().length < 10}
              className="flex items-center gap-1.5 rounded-md border border-emerald-700 bg-emerald-500/15 px-3 py-1.5 text-xs font-bold tracking-wide text-emerald-300 hover:bg-emerald-500/25 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <PlayCircle className="h-4 w-4" />
              {busy ? 'LAUNCHING…' : 'RUN PIPELINE'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
