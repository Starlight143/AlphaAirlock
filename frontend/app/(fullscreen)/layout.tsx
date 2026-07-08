import type { ReactNode } from 'react';
import { IngestDialogProvider } from '@/components/layout/IngestDialogProvider';

/**
 * Fullscreen route group (P6-A1).
 *
 * Used for pages whose reference design owns the entire viewport — currently
 * /cointegration's star-field graph, but reserved for future immersive views
 * (full-bleed factor-network, presentation mode, etc.).
 *
 * Differences vs (shell)/layout.tsx:
 *   - No Sidebar / HeaderBarV2 — the page chrome would clip the visualization.
 *   - IngestDialogProvider is preserved so any chrome-less page can still
 *     pop the +INGEST dialog programmatically; without it the provider context
 *     would be unavailable and any nested hook that imported it would crash
 *     on mount.
 *   - Background colour matches the rest of the app so the transition between
 *     /(shell) and /(fullscreen) pages is invisible if the user wanders.
 */
export default function FullscreenLayout({ children }: { children: ReactNode }) {
  return (
    <IngestDialogProvider>
      <div className="relative h-screen w-screen overflow-hidden bg-slate-950 text-slate-100">
        {children}
      </div>
    </IngestDialogProvider>
  );
}
