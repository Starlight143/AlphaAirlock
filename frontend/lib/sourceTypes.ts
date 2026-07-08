// Frontend-side label registry for /sources (P5-FE-15, extended in P8-FIX/M-1).
// Keeps the section headings / dropdown labels / category-tab labels in one
// place so the page never accidentally shows raw lowercase keys.
//
// `SOURCE_CATEGORIES` mirrors `backend.core.database.SOURCE_CATEGORIES` —
// if you add a category here, add it there too.

import type { SourceType } from './api';

export const SOURCE_TYPE_LABELS: Record<SourceType, string> = {
  rss: 'RSS Feeds',
  patreon: 'Patreon Subscriptions',
  medium: 'Medium Publications',
  substack: 'Substack Newsletters',
  reddit: 'Reddit Subscriptions',
  twitter_tag: 'Twitter Tag Feeds',
  twitter_article: 'Twitter Article Feeds',
  youtube_video: 'YouTube Subscriptions',
  tiktok: 'TikTok Subscriptions',
  arxiv: 'arXiv Paper Feeds',
  glassnode: 'Glassnode Insights',
  manual: 'Manual Input Feeds',
};

export const SOURCE_TYPE_PLACEHOLDERS: Record<SourceType, string> = {
  rss: 'https://example.com/feed.xml',
  patreon: 'https://www.patreon.com/rss/<creator>?auth=<token>',
  medium: 'https://medium.com/@author or https://medium.com/feed/publication',
  substack: 'https://<newsletter>.substack.com',
  reddit: 'https://reddit.com/r/<sub>/.rss',
  twitter_tag: '@handle or https://twitter.com/<handle> (Nitter bridge)',
  twitter_article: '@handle or https://twitter.com/<handle>/status/<id>',
  youtube_video: 'UC... / @handle / channel URL — handle auto-resolves',
  tiktok: '(stub — no stable public API)',
  arxiv: 'cat:cs.LG · au:Cochrane · ti:funding+rate · or full query string',
  glassnode: 'https://research.glassnode.com/rss/ or any glassnode export URL',
  manual: 'noop — use the +INGEST button to add content',
};

export const SOURCE_CATEGORIES = [
  'apps',
  'youtube',
  'dps',
  'invoice',
  'trading_tool',
  'quant_fund',
  'algo_trading',
  'course',
  'tradeview',
  'research',
  'cloud',
  'grafana_data',
  'ai_image',
  'ai',
] as const;

export type SourceCategory = (typeof SOURCE_CATEGORIES)[number];

// P11-F4-14 — Display labels for the SourcesTopTabs / category badges. The
// underlying KEYS ("tradeview", "research") must stay stable because the
// backend (SOURCE_CATEGORIES in backend.core.database) and persisted source
// rows still reference those exact strings — renaming the key would orphan
// every row in production. Only the user-visible label changes here:
//   - "TradeView"  → "TheToolmaker"
//   - "Research"   → "Research (Stock)"
export const CATEGORY_LABELS: Record<SourceCategory, string> = {
  apps: 'Apps',
  youtube: 'YouTube',
  dps: 'DPS',
  invoice: 'Invoice',
  trading_tool: 'Trading Tool',
  quant_fund: 'Quant Fund',
  algo_trading: 'Algo Trading',
  course: 'Course',
  tradeview: 'TheToolmaker',
  research: 'Research (Stock)',
  cloud: 'Cloud',
  grafana_data: 'Grafana Data',
  ai_image: 'AI generate image',
  ai: 'AI',
};

export function sourceTypeLabel(t: string | null | undefined): string {
  if (!t) return 'Other';
  return SOURCE_TYPE_LABELS[t as SourceType] ?? 'Other';
}

export function categoryLabel(c: string | null | undefined): string {
  if (!c) return 'Uncategorised';
  return CATEGORY_LABELS[c as SourceCategory] ?? c;
}
