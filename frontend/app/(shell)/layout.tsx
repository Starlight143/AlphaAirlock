import { Suspense, type ReactNode } from 'react';
import Sidebar from '@/components/layout/Sidebar';
import HeaderBarV2 from '@/components/layout/HeaderBarV2';
import { IngestDialogProvider } from '@/components/layout/IngestDialogProvider';

/**
 * Shell layout for all dashboard pages.
 *
 * Composition:
 *   ┌────────────────────────────────────────────┐
 *   │ Sidebar │ HeaderBarV2                       │
 *   │ (240px) ├───────────────────────────────────┤
 *   │         │ children (per-route page content) │
 *   └─────────┴───────────────────────────────────┘
 *
 * The sidebar + header are server-rendered shells with embedded client
 * subcomponents (Sidebar uses usePathname; HeaderBarV2 uses TanStack Query).
 * IngestDialogProvider hoists the global "+ INGEST" dialog so any page can
 * trigger it via useIngestDialog().
 *
 * Next.js 14 requires every useSearchParams() call to be inside a Suspense
 * boundary. HeaderBarV2 calls useSearchParams() for the ?debug=1 flag, so we
 * wrap it here. The fallback preserves the header height (h-16 + border-b +
 * the CyclerRow h-9) so the layout does not shift during the brief suspension
 * on first render.
 */
export default function ShellLayout({ children }: { children: ReactNode }) {
  return (
    <IngestDialogProvider>
      {/* P29-F14: keyboard accessibility skip link. */}
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only fixed top-2 left-2 z-50 rounded bg-cyan-500 px-3 py-1 text-xs font-semibold text-white shadow-lg"
      >
        Skip to main content
      </a>
      <div className="flex h-screen w-screen overflow-hidden bg-slate-950 text-slate-100">
        <Sidebar />
        <div className="relative flex h-full flex-1 flex-col overflow-hidden">
          {/* Suspense boundary required by Next.js 14 because HeaderBarV2
              calls useSearchParams() internally (for ?debug=1). Without this,
              Next.js throws a build-time warning/error in strict mode and may
              suspend the entire shell layout tree. The fallback skeleton
              matches the combined height of the header (h-16) + CyclerRow
              (h-9) so there is no layout shift on hydration. */}
          <Suspense
            fallback={
              <div className="flex flex-col">
                <div className="h-16 border-b border-slate-800 bg-slate-900/60" />
                <div className="h-9 border-b border-slate-800 bg-slate-950/40" />
              </div>
            }
          >
            <HeaderBarV2 />
          </Suspense>
          {/* min-h-0 is REQUIRED: as a flex child, <main> defaults to
              min-height:auto which lets it grow to its content's height instead
              of being bounded by the flex row. That silently breaks every page
              whose root uses `h-full overflow-y-auto` to scroll (e.g.
              /backtest-panel) — the page grows past the viewport and its
              bottom (the BG Pipeline Gate Criteria panel) is clipped with no
              way to scroll to it. min-h-0 bounds <main> so inner overflow works. */}
          <main id="main-content" tabIndex={-1} className="relative flex-1 min-h-0 overflow-hidden">
            {children}
          </main>
        </div>
      </div>
    </IngestDialogProvider>
  );
}
