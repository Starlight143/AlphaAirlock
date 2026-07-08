'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { X, FileText, ExternalLink, Loader2 } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import { api, type IngestSource, type KnowledgeNode } from '@/lib/api';
import { markdownComponents, remarkMathPlugins, rehypeMathPlugins } from '@/lib/markdownComponents';
import { safeExternalUrl } from '@/lib/safeUrl';
import { queryKeys } from '@/lib/query';

type Props = {
  source: IngestSource;
  onClose: () => void;
};

/**
 * Full-bleed drawer that opens when a SourceCard's "Open" button is clicked
 * (P6-A2). Mirrors the reference screenshot:
 *
 *   ┌─────────────────────────────────────────────────────────────┐
 *   │  <Source name> — N files                          [×]        │
 *   ├──────────────────────────┬──────────────────────────────────┤
 *   │  date-slug filename 1     │                                  │
 *   │  date-slug filename 2     │  Selected file's markdown body   │
 *   │  date-slug filename 3     │                                  │
 *   │  …                        │                                  │
 *   └──────────────────────────┴──────────────────────────────────┘
 *
 * Lives at fixed z-40 over the page; Escape and the X button both close.
 */
export default function SourceFilesDrawer({ source, onClose }: Props) {
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const filesQ = useQuery({
    queryKey: queryKeys.sourceFiles(source.id, 500),
    queryFn: () => api.sourceFiles(source.id, 500),
    staleTime: 30_000,
  });

  const nodes: KnowledgeNode[] = filesQ.data?.nodes ?? [];
  const selected = useMemo(
    () => (selectedId ? nodes.find((n) => n.id === selectedId) : null) ?? null,
    [nodes, selectedId],
  );

  // P16 A-H1 — Escape key handler. The previous `onKeyDown` on the
  // outer div never fired because the div was not focusable (no
  // `tabIndex`). Attach to `window` while the drawer is mounted and
  // clean up on unmount. Pattern matches StageDrillDownModal.tsx.
  //
  // Use a ref to snapshot `onClose` so the effect only runs on
  // mount/unmount (dep array `[]`). This prevents the listener from
  // being torn down and re-added on every render caused by the
  // `sourcesQ` polling interval — the latest `onClose` is always
  // called via the ref without re-registering the DOM listener.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCloseRef.current();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  return (
    <div
      className="fixed inset-0 z-40 flex bg-slate-950/85 backdrop-blur"
      role="dialog"
      aria-modal="true"
      aria-labelledby="source-files-drawer-title"
    >
      {/* Click-outside catcher (left/right gutters) */}
      <div className="hidden flex-1 lg:block" onClick={onClose} />
      <div className="flex h-full w-full max-w-[1280px] flex-col border-l border-slate-800 bg-slate-950 shadow-2xl lg:mx-auto lg:border-x lg:rounded-l-none">
        {/* Header */}
        <header className="flex items-center justify-between border-b border-slate-800 bg-slate-900/60 px-4 py-3">
          <div className="flex min-w-0 items-center gap-3">
            <FileText className="h-4 w-4 text-cyan-300" />
            <div className="min-w-0">
              <div id="source-files-drawer-title" className="truncate text-sm font-bold text-slate-100">
                {source.name}
              </div>
              <div className="truncate font-mono text-[10px] text-slate-500">
                {source.url}
              </div>
            </div>
            <span className="shrink-0 rounded border border-cyan-700/40 bg-cyan-500/10 px-1.5 py-0.5 font-mono text-[10px] font-bold uppercase tracking-wider text-cyan-300">
              {filesQ.data?.total ?? '—'} files
            </span>
          </div>
          <button
            onClick={onClose}
            aria-label="Close drawer"
            className="inline-flex items-center gap-1 rounded border border-slate-700 px-2 py-1 text-[10px] uppercase tracking-wider text-slate-400 hover:bg-slate-800 hover:text-slate-100"
          >
            <X className="h-3.5 w-3.5" />
            Close
          </button>
        </header>

        {/* Body: 2-pane list + preview */}
        <div className="flex min-h-0 flex-1">
          <aside className="flex w-[320px] shrink-0 flex-col border-r border-slate-800">
            {filesQ.isLoading ? (
              <div className="flex items-center justify-center gap-2 py-12 text-[11px] text-slate-500">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                Loading…
              </div>
            ) : nodes.length === 0 ? (
              <div className="px-4 py-12 text-center text-[11px] text-slate-500">
                No files ingested from this source yet.
              </div>
            ) : (
              <ul className="flex-1 divide-y divide-slate-800 overflow-y-auto">
                {nodes.map((n) => (
                  <li key={n.id}>
                    <button
                      onClick={() => setSelectedId(n.id)}
                      className={`block w-full px-3 py-2 text-left text-[11px] hover:bg-slate-900 ${
                        selectedId === n.id ? 'bg-cyan-500/10 text-cyan-200' : 'text-slate-300'
                      }`}
                    >
                      <div className="truncate font-mono text-[10px] text-slate-500">
                        {filenameFor(n)}
                      </div>
                      <div className="line-clamp-2 text-[11px]">{n.title}</div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </aside>

          {/* Preview pane */}
          <section className="flex min-w-0 flex-1 flex-col overflow-hidden">
            {selected ? (
              <article className="prose prose-invert prose-sm h-full max-w-none overflow-y-auto px-6 py-5 prose-headings:font-mono prose-h2:text-rose-300 prose-code:text-emerald-200">
                <header className="not-prose mb-3 flex items-start justify-between gap-3 border-b border-slate-800 pb-3">
                  <div>
                    <h1 className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
                      K#{selected.id}
                    </h1>
                    <h2 className="mt-1 text-base font-bold text-slate-100">
                      {selected.title}
                    </h2>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {/* P11-F4-06 — Open in KB shortcut so operators can jump from
                        the drawer preview to the full KB Explorer page for the
                        same node (cross-links, edit, lineage etc.). */}
                    <Link
                      href={`/kb-explorer?node=${selected.id}`}
                      className="inline-flex items-center gap-1 rounded border border-cyan-700/40 bg-cyan-500/10 px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-cyan-200 hover:bg-cyan-500/20"
                      title="Open in Knowledge Base Explorer"
                    >
                      <FileText className="h-3 w-3" />
                      Open in KB
                    </Link>
                    {selected.source_url && (() => {
                      // P29-F11: only emit anchor for safe schemes.
                      const safe = safeExternalUrl(selected.source_url);
                      if (!safe) {
                        return (
                          <span
                            className="inline-flex items-center gap-1 rounded border border-slate-700 px-2 py-1 text-[10px] uppercase tracking-wider text-slate-500"
                            title={`Blocked URL: ${selected.source_url}`}
                          >
                            <ExternalLink className="h-3 w-3" />
                            blocked
                          </span>
                        );
                      }
                      return (
                        <a
                          href={safe}
                          target="_blank"
                          rel="noreferrer noopener"
                          className="inline-flex items-center gap-1 rounded border border-slate-700 px-2 py-1 text-[10px] uppercase tracking-wider text-slate-300 hover:bg-slate-800"
                        >
                          <ExternalLink className="h-3 w-3" />
                          source
                        </a>
                      );
                    })()}
                  </div>
                </header>
                <ReactMarkdown components={markdownComponents} remarkPlugins={remarkMathPlugins} rehypePlugins={rehypeMathPlugins}>
                  {selected.content || '*(no content)*'}
                </ReactMarkdown>
              </article>
            ) : (
              <div className="flex h-full items-center justify-center text-xs text-slate-500">
                Click a file to preview its content
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}


function filenameFor(n: KnowledgeNode): string {
  const datePart = (n.ingested_at || n.created_at || '').slice(0, 10);
  const slugBase = (n.title || `node-${n.id}`).toLowerCase();
  // P12-B-L6 — keep CJK (U+4E00..U+9FFF) characters intact in the slug so
  // Chinese / Japanese / Korean titles aren't collapsed to empty strings.
  const slug = slugBase
    .normalize('NFKD')
    .replace(/[^a-z0-9一-鿿]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64) || `k-${n.id}`;
  return datePart ? `${datePart}-${slug}` : slug;
}
