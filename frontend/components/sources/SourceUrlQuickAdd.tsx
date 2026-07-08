'use client';

import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Loader2, Sparkles } from 'lucide-react';
import { api, type SourceType } from '@/lib/api';
import { queryKeys } from '@/lib/query';

/**
 * Inline URL quick-add (P6-A2).
 *
 * Matches the reference screenshot where the /sources header has a single
 * full-width input that accepts any URL and the system auto-detects the
 * source_type. Keeps the +ADD SOURCE modal as the fallback for cases where
 * the heuristic is wrong or the user wants to tweak cadence / category.
 *
 * Heuristic uses URL-host substrings — explicit and predictable. The user
 * can always edit the source after creation.
 */
type Props = {
  onAdded?: (sourceId: number) => void;
};

const DETECTION_PATTERNS: { match: RegExp; type: SourceType }[] = [
  { match: /youtube\.com\/feeds\/videos\.xml|youtube\.com\/(@|channel\/|c\/|user\/)|youtu\.be/i, type: 'youtube_video' },
  { match: /(^|\/)UC[\w-]{22}(\/|$)/, type: 'youtube_video' },
  { match: /reddit\.com\/r\//i, type: 'reddit' },
  { match: /(^|\/\/)([\w-]+\.)?substack\.com/i, type: 'substack' },
  { match: /medium\.com|@[\w-]+\.medium\.com/i, type: 'medium' },
  { match: /patreon\.com/i, type: 'patreon' },
  { match: /tiktok\.com/i, type: 'tiktok' },
  { match: /twitter\.com|x\.com/i, type: 'twitter_tag' },
  { match: /arxiv\.org|^(cat|au|ti|abs):/i, type: 'arxiv' },
  // P12-B-L5 — recognise Glassnode Insights so the URL-quick-add picks the
  // dedicated 'glassnode' fetcher rather than falling through to generic RSS.
  // Must precede the RSS catch-all since insights.glassnode.com/rss matches both.
  { match: /glassnode\.com|insights\.glassnode\.com/i, type: 'glassnode' },
  { match: /\/feed(\b|\.xml|\.rss|$)|\.rss$|\.xml$/i, type: 'rss' },
];

function detectSourceType(url: string): SourceType {
  const trimmed = url.trim();
  for (const { match, type } of DETECTION_PATTERNS) {
    if (match.test(trimmed)) return type;
  }
  // Default to RSS for raw http/https; treat anything else as a manual entry.
  return /^https?:\/\//i.test(trimmed) ? 'rss' : 'manual';
}


function deriveName(url: string): string {
  try {
    const u = new URL(/^https?:\/\//.test(url) ? url : `https://${url}`);
    const host = u.hostname.replace(/^www\./, '');
    // For Substack / YouTube handle / Reddit subreddit, pick the first non-feed path segment.
    const part = u.pathname.split('/').filter(Boolean).find((p) => !/^(feed|rss|videos\.xml)$/i.test(p));
    return part ? `${host}/${part}` : host;
  } catch {
    return url.slice(0, 80);
  }
}


export default function SourceUrlQuickAdd({ onAdded }: Props) {
  const [url, setUrl] = useState('');
  const [error, setError] = useState<string | null>(null);
  const qc = useQueryClient();

  const create = useMutation({
    mutationFn: async () => {
      const trimmed = url.trim();
      if (!trimmed) throw new Error('Empty URL');
      const sourceType = detectSourceType(trimmed);
      const payload = {
        name: deriveName(trimmed),
        source_type: sourceType,
        url: trimmed,
        cadence_minutes: 60,
        enabled: true,
      };
      return api.sourceCreate(payload);
    },
    onSuccess: (src) => {
      setUrl('');
      setError(null);
      qc.invalidateQueries({ queryKey: queryKeys.sources });
      onAdded?.(src.id);
    },
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  });

  return (
    <div className="flex min-w-[260px] flex-1 flex-col gap-1">
      <div className="flex items-center gap-1.5 rounded-md border border-slate-700 bg-slate-950 px-2.5 py-1">
        <Sparkles className="h-3.5 w-3.5 text-cyan-400" />
        <input
          type="text"
          value={url}
          onChange={(e) => { setUrl(e.target.value); setError(null); }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              if (url.trim() && !create.isPending) create.mutate();
            }
          }}
          placeholder="Paste source URL or profile URL to add new source..."
          className="min-w-0 flex-1 bg-transparent text-[11px] text-slate-200 placeholder:text-slate-600 focus:outline-none"
        />
        <span className="hidden text-[9px] uppercase tracking-widest text-slate-500 sm:inline">
          {url.trim() ? detectSourceType(url) : 'auto'}
        </span>
        <button
          onClick={() => { if (url.trim() && !create.isPending) create.mutate(); }}
          disabled={create.isPending || !url.trim()}
          className="inline-flex items-center gap-1 rounded border border-cyan-700 bg-cyan-500/15 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-cyan-200 hover:bg-cyan-500/25 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {create.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : 'Add'}
        </button>
      </div>
      {error && <div className="text-[10px] text-rose-300">{error}</div>}
    </div>
  );
}
