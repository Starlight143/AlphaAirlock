# Ingest Sources — URL / Identifier Format Guide

> **English** · [繁體中文](./ingest-sources.zh.md)

This page documents what to type into the **URL / Identifier** field for every
source type on the **`/sources`** page, and exactly how the backend turns that
value into a feed it can poll.

The single most important rule:

> **The generic `rss` type does NOT auto-discover a feed from a homepage.**
> Whatever you type is passed straight to the feed parser. For `rss` you must
> enter the **feed URL itself** (`…/feed`, `…/rss`, `…/feed.xml`). For the
> platform-specific types (Substack / Medium / Reddit / YouTube / Twitter) you
> *can* paste the author's home/profile URL — the fetcher derives the feed
> endpoint for you.

Authoritative sources for everything below:
[`backend/core/database.py`](../backend/core/database.py) (`SOURCE_TYPES`),
[`backend/core/ingest_fetchers.py`](../backend/core/ingest_fetchers.py)
(per-type fetchers), and
[`frontend/lib/sourceTypes.ts`](../frontend/lib/sourceTypes.ts)
(UI labels / placeholders).

---

## Quick reference

| `source_type` | UI label | What to enter in **URL / Identifier** | Auto-derives the feed? |
|---|---|---|---|
| `rss` | RSS Feeds | The **feed URL itself** — `https://site.com/feed.xml` (or `…/feed`, `…/rss`) | ❌ No — must already be a feed endpoint |
| `substack` | Substack Newsletters | `https://<newsletter>.substack.com` (homepage is fine) | ✅ appends `/feed` |
| `medium` | Medium Publications | `https://medium.com/@author` or `https://medium.com/feed/<publication>` | ✅ inserts `/feed` (medium.com-hosted only) |
| `reddit` | Reddit Subscriptions | `https://reddit.com/r/<sub>` (or a user page) | ✅ appends `.rss`; falls back to `old.reddit.com` on 403 |
| `youtube_video` | YouTube Subscriptions | Channel URL, `@handle`, or a `UC…` channel id | ✅ resolves to `feeds/videos.xml` |
| `twitter_tag` | Twitter Tag Feeds | `@handle`, a profile URL, or a `#hashtag` | ✅ via a Nitter RSS bridge |
| `twitter_article` | Twitter Article Feeds | Same as `twitter_tag` | ✅ via the same Nitter bridge |
| `patreon` | Patreon Subscriptions | `https://www.patreon.com/rss/<creator>?auth=<token>` | ❌ paste the exact member-RSS URL (audio only) |
| `arxiv` | arXiv Paper Feeds | A query — `cat:cs.LG`, `au:Name`, `ti:funding+rate`, free text, or a full atom URL | ✅ via the arXiv API |
| `glassnode` | Glassnode Insights | `https://research.glassnode.com/rss/` | ❌ paste the feed URL |
| `tiktok` | TikTok Subscriptions | — | — **stub**, never fetched (no stable public API) |
| `manual` | Manual Input Feeds | — | — never polled; content arrives via the **+ INGEST** button |

Legend: ✅ = you may paste the author's home/profile URL and the fetcher builds
the real feed URL. ❌ = you must paste a working feed URL yourself.

---

## Per-type detail

### `rss` — generic RSS / Atom
- **Enter:** the feed URL, e.g. `https://example.com/feed.xml`, `https://example.com/feed`, `https://example.com/index.xml`.
- **Behaviour:** the URL is handed verbatim to the feed parser. A blog *homepage*
  (HTML, not a feed) yields no entries and the source shows an error.
- **Finding a site's feed URL:** try the homepage + `/feed`, `/rss`, `/feed.xml`,
  or `/atom.xml`; or open the page source and search for
  `application/rss+xml` / `application/atom+xml` — the `href` there is the feed.

### `substack` — Substack newsletters
- **Enter:** `https://<newsletter>.substack.com` (homepage) **or** the full `…/feed`.
- **Behaviour:** if the URL does not already end in `/feed`, the fetcher appends it.

### `medium` — Medium publications & authors
- **Enter:** `https://medium.com/@author`, `https://medium.com/<publication>`, or the full `https://medium.com/feed/<publication>`.
- **Behaviour:** when the host is `medium.com` and the path has no `/feed`, the
  fetcher inserts `/feed` at the path root. **Custom-domain** Medium blogs
  (e.g. `blog.example.com`) are *not* rewritten — for those, enter the feed URL
  directly (usually `…/feed`).

### `reddit` — subreddits & user feeds
- **Enter:** `https://reddit.com/r/<sub>` or a user page; trailing `.rss`/`.json` optional.
- **Behaviour:** appends `.rss` when missing, sends a browser-like User-Agent,
  and retries via `old.reddit.com` if `www.reddit.com` returns HTTP 403.

### `youtube_video` — channels (with optional transcripts)
- **Enter:** a channel URL, an `@handle`, a `UC…` channel id, or a ready-made `feeds/videos.xml?channel_id=UC…` URL.
- **Behaviour:** `@handle` / `/c/` / `/user/` URLs are HTML-scraped to resolve the
  `UC…` channel id, then converted to the channel RSS feed. With
  `YT_TRANSCRIPT_ENABLED=1` each entry's body is enriched with the video transcript.

### `twitter_tag` / `twitter_article` — X/Twitter via Nitter
- **Enter:** `@handle`, a profile URL (`https://twitter.com/<handle>` or `x.com`), or a `#hashtag` / search URL.
- **Behaviour:** routed through a Nitter RSS bridge — handles become
  `{nitter}/<handle>/rss`, hashtags become `{nitter}/search/rss?q=%23…&f=tweets`.
  The mirror defaults to `https://nitter.privacydev.net` and is overridable with
  `NITTER_INSTANCE_URL` (public mirrors rotate often, so expect to set this).
  `twitter_article` uses the exact same path as `twitter_tag`.

### `patreon` — creator podcast RSS
- **Enter:** `https://www.patreon.com/rss/<creator>?auth=<member_token>`.
- **Behaviour:** the URL is fetched verbatim. **Two hard limitations:**
  (1) the `?auth=<token>` is *your* per-member token, found in Patreon →
  Membership → RSS link — without it Patreon silently returns no items;
  (2) Patreon RSS contains **audio/podcast episodes only** — text, image, and
  video posts are not available (they need the OAuth web API, which is not implemented).

### `arxiv` — paper feeds
- **Enter:** a query string — `cat:cs.LG` (category), `au:Cochrane` (author),
  `ti:funding+rate` (title contains), any free-text query, **or** a full arXiv atom URL.
- **Behaviour:** non-URL queries go through the arXiv API sorted by submission
  date (newest first); a full `http(s)` atom URL is parsed as a normal feed.
  `ARXIV_MAX_RESULTS` (default `10`, clamped `1…50`) bounds the result count.

### `glassnode` — Glassnode Insights
- **Enter:** `https://research.glassnode.com/rss/` (or any Glassnode export feed URL).
  The old `insights.glassnode.com` host now answers non-browser clients with a
  Cloudflare 403 — use the `research.` host directly.
- **Behaviour:** fetched as a normal RSS feed; it has its own type so the UI and
  KPI tiles surface it distinctly from generic `rss`.

### `tiktok` — stub
- Always skipped — there is no stable public API. Adding one is harmless but it never ingests; the UI marks it with a **STUB** badge.

### `manual` — operator-entered content
- Never polled by the scheduler. Leave the URL blank and push content in via the
  **+ INGEST** dialog on the dashboard. The cadence field is disabled for this type.

---

## Two ways to add a source

1. **Quick-add bar** (top of `/sources`) — paste any URL and the type is
   auto-detected from the host (Substack / Medium / Reddit / YouTube / Twitter /
   arXiv / Glassnode). Anything that looks like a plain `http(s)` URL with no
   recognised host **defaults to `rss`** (so pasting a homepage here creates an
   `rss` source that will fail unless the URL is actually a feed); anything that
   isn't an `http(s)` URL becomes `manual`. Cadence defaults to 60 minutes.
2. **+ ADD SOURCE modal** — full control over **Name**, **Source Type**,
   **Category**, **URL / Identifier**, **Poll cadence**, and **Enabled**. Use
   this when the auto-detector guesses wrong or you want a non-default cadence.

### Cadence, category, enabled
- **Poll cadence** — minutes between polls. Default **60**; the modal accepts a
  whole number from **5** to **10080** (7 days).
- **Category** — an optional content tab (`apps`, `quant_fund`, `research`,
  `ai`, …). Leave it blank to auto-detect from the URL.
- **Enabled** — when on, the source is polled on the next scheduler tick.

---

## Gotchas, limits & required switches

- **Scheduler master switch.** Sources are only auto-polled when
  `ALPHA_INGEST_ENABLED=1` (default `0`); `ALPHA_INGEST_TICK_SECONDS` sets the
  tick. You can add sources with the scheduler off — they just won't fetch until
  it's on.
- **Public hosts only (SSRF guard).** `POST /api/sources` and every fetch reject
  URLs whose host resolves to a private / loopback / link-local / reserved
  address. Only public internet URLs work unless you set
  `ALPHA_ALLOW_LOCAL_INGEST=1` (local-dev only — never in production).
- **Response cap.** Feed bodies are capped (default 10 MiB,
  `ALPHA_INGEST_MAX_RESPONSE_BYTES`); oversized responses are refused.
- **Optional enrichment.** `ARTICLE_ENRICH_ENABLED=1` fetches full article text
  when an RSS summary is too short (may breach a site's ToS); `ASSET_CACHE_ENABLED=1`
  caches inline feed images to `storage/assets/`.
- **De-duplication.** Items are de-duped by a content hash of
  `title | url | body`, so re-polling the same feed will not create duplicates.
