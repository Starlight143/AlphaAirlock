"""Per-source-type fetchers (P3 / P6).

Each fetcher takes an `IngestSource` row and returns a `FetchOutcome`
describing what happened. The scheduler logs every attempt as an
`IngestEvent` row and writes new `KnowledgeNode` rows when fresh content
shows up.

Supported source types (11 total — matches `database.SOURCE_TYPES`):

  rss / patreon / medium / substack             → feedparser (P3)
  reddit                                         → feedparser on .rss endpoint
  youtube_video                                  → RSS + optional transcript (P6-B4)
  twitter_tag / twitter_article                  → Nitter RSS bridge (P6-B4)
  arxiv                                          → arxiv pip package (P6-B4)
  tiktok                                         → STUB (no stable public API)
  manual                                         → not auto-fetched (inbound only)

All fetchers are SYNCHRONOUS and safe to call from the scheduler thread. They
NEVER raise — every failure is captured into FetchOutcome.error.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import socket
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime  # R5/SRE-008: HTTP-date Retry-After
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import httpx

from backend._envloader import env_bool, env_int, env_str
from backend.core.database import KB_CONTENT_MAX_CHARS, IngestSource, KnowledgeNode

logger = logging.getLogger("alpha.ingest")

DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=8.0)
# Identify as a real browser for ALL outbound fetches. The previous bespoke
# branded UA ("Agentic Alpha Research System") was reliably fingerprinted as a
# bot by Cloudflare and similar CDNs, which answered with a 403 "Just a moment…"
# challenge page — whose Content-Encoding additionally tripped httpx's decoder,
# masking the 403 as the cryptic "network: Error -3 … incorrect header check".
# A realistic browser UA reaches the origin (verified against
# research.glassnode.com → HTTP 200 + 10 entries; the legacy
# insights.glassnode.com host now 403s non-browser TLS clients outright and
# only 301s real browsers to research. — Reddit likewise blocked the
# old UA — see _REDDIT_HEADERS). NOTE: do NOT pair this with
# `Accept-Encoding: identity` on feed fetches — sending identity is itself a bot
# tell that RE-triggers the Cloudflare challenge (verified: identity → 403 where
# default gzip negotiation → 200). httpx negotiates/decodes gzip/deflate/br
# transparently on the happy path.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Feed-shaped Accept header so content-negotiating origins return XML, not HTML.
_FEED_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
    "text/xml;q=0.8, */*;q=0.5"
)

# P30-S3: per-hop SSRF guard. SourceCreateRequest validator only checks
# the URL the user pasted; httpx.Client(follow_redirects=True) silently
# walks 30x chains, possibly to RFC1918 / link-local / loopback hosts
# (e.g. 169.254.169.254 cloud-instance metadata). All fetches now route
# through _safe_get which performs manual redirect handling with per-hop
# IP-class validation.
_MAX_REDIRECTS = 5

# P-SSRF2: hard response-body cap. `_safe_get` callers buffer the whole body
# (`resp.content` / `resp.text[:500_000]`); without a cap a malicious or
# oversized feed — or an operator-configurable Nitter mirror fed an attacker
# handle — can exhaust memory before the slice ever applies. Mirrors the
# streaming byte-cap in asset_cache._download. Configurable; default 10 MiB.
_MAX_RESPONSE_BYTES = env_int(
    "ALPHA_INGEST_MAX_RESPONSE_BYTES", 10 * 1024 * 1024, minimum=64 * 1024,
)


def _assert_public_host(url: str) -> Optional[str]:
    """Validate URL host; return the validated IP literal to pin the connection.

    Raises ValueError if the host resolves to a non-public IP. Returns the
    first validated public IP (string) so the caller can connect to that
    exact address and defeat DNS-rebinding/TOCTOU between validation and
    fetch. Returns None when validation is bypassed (local-dev escape hatch)
    so the caller falls back to connecting by hostname.

    Honours ALPHA_ALLOW_LOCAL_INGEST escape hatch for local-dev testing.
    """
    # P31-D2: canonical strict-whitelist env_bool.
    from backend._envloader import env_bool as _env_bool_local
    if _env_bool_local("ALPHA_ALLOW_LOCAL_INGEST", False):
        return None
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"refusing non-http(s) scheme {scheme!r} for {url!r}")
    host = (parsed.hostname or "").strip()
    if not host:
        raise ValueError(f"refusing url with empty host: {url!r}")
    try:
        ip_literal = ipaddress.ip_address(host)
        candidates = [ip_literal]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except (socket.gaierror, OSError) as exc:
            raise ValueError(f"host {host!r} could not be resolved ({exc})") from exc
        candidates = []
        for info in infos:
            sockaddr = info[4]
            if sockaddr and sockaddr[0]:
                try:
                    candidates.append(ipaddress.ip_address(sockaddr[0]))
                except ValueError:
                    continue
    for ip in candidates:
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        ):
            raise ValueError(
                f"host {host!r} resolves to non-public address {ip}; "
                "refusing for SSRF safety"
            )
    if not candidates:
        raise ValueError(f"host {host!r} produced no usable address")
    # Pin the connection to a validated address (all candidates passed the
    # checks above, so picking the first is safe). Defeats DNS-rebinding:
    # the fetch connects to THIS IP, not to a freshly re-resolved hostname.
    return str(candidates[0])


def _pin_request(
    url: str,
    headers: Dict[str, str],
    pinned_ip: Optional[str],
) -> tuple[str, Dict[str, str], Dict[str, Any]]:
    """Rewrite a request to connect to a pre-validated IP while preserving the
    original Host header + TLS SNI. Returns (connect_url, headers, extensions).

    If pinned_ip is None (validation bypassed) or the host is already an IP
    literal, returns the URL unchanged so behaviour is identical to before.
    """
    if not pinned_ip:
        return url, headers, {}
    parsed = httpx.URL(url)
    original_host = parsed.host  # decoded hostname, no brackets/port
    if not original_host:
        return url, headers, {}
    # Host already an IP literal -> nothing to rebind; leave as-is.
    try:
        ipaddress.ip_address(original_host)
        return url, headers, {}
    except ValueError:
        pass
    # copy_with(host=...) brackets IPv6 automatically and preserves port/path/query.
    connect_url = str(parsed.copy_with(host=pinned_ip))
    out_hdrs = dict(headers)
    # Build the canonical Host value (include non-default port).
    port = parsed.port
    if port is None:
        host_header = original_host
    else:
        host_header = f"{original_host}:{port}"
    # Overwrite any case-variant of an existing Host header.
    for k in list(out_hdrs.keys()):
        if k.lower() == "host":
            del out_hdrs[k]
    out_hdrs["Host"] = host_header
    return connect_url, out_hdrs, {"sni_hostname": original_host}


def _rebuffer_response(streamed: httpx.Response, body: bytes) -> httpx.Response:
    """Rebuild a non-streaming Response from already content-decoded body bytes.

    `streamed.iter_bytes()` / `streamed.content` returns bytes with the wire
    Content-Encoding (gzip / deflate / br) ALREADY removed, but `streamed.headers`
    still advertises that encoding. Rebuilding with those headers makes
    `httpx.Response.__init__` → `read()` run the decoder a SECOND time over the
    already-decoded body, which dies with "Error -3 … incorrect header check"
    for every compressed response. Strip the now-inaccurate framing headers so
    the rebuilt Response carries the decoded body verbatim. (Content-Length is
    dropped too — it described the compressed wire length, not `len(body)`.)
    """
    clean = httpx.Headers(
        [
            (k, v)
            for k, v in streamed.headers.items()
            if k.lower() not in ("content-encoding", "content-length")
        ]
    )
    return httpx.Response(
        status_code=streamed.status_code,
        headers=clean,
        content=body,
        request=streamed.request,
    )


def _safe_get(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[httpx.Timeout] = None,
) -> httpx.Response:
    """SSRF-safe GET with manual redirect handling.

    Per-hop IP-class check runs BEFORE every request. Caps at _MAX_REDIRECTS.
    """
    hdrs = dict(headers or {})
    cur = url
    with httpx.Client(timeout=timeout or DEFAULT_TIMEOUT, follow_redirects=False) as cli:
        for _ in range(_MAX_REDIRECTS + 1):
            pinned_ip = _assert_public_host(cur)
            # P-SSRF3: defeat DNS-rebinding/TOCTOU. _assert_public_host resolved
            # and validated the host; connect to THAT exact IP rather than letting
            # httpx re-resolve the hostname (a second lookup an attacker-controlled
            # DNS could answer with 127.0.0.1 / 169.254.169.254). Preserve the
            # original Host header and TLS SNI so vhost routing + cert validation
            # are unchanged. When validation is bypassed (local-dev), connect by name.
            connect_url, req_hdrs, req_ext = _pin_request(cur, hdrs, pinned_ip)
            # Stream so we can abort once the body exceeds _MAX_RESPONSE_BYTES,
            # then rebuffer into a fresh Response so callers keep using
            # `.content` / `.text` / `.json()` exactly as before (same return
            # contract as the previous `cli.get()`).
            with cli.stream("GET", connect_url, headers=req_hdrs, extensions=req_ext) as streamed:
                if streamed.status_code in (301, 302, 303, 307, 308):
                    loc = streamed.headers.get("location") or ""
                    if not loc:
                        streamed.read()
                        return _rebuffer_response(streamed, streamed.content)
                    streamed.read()  # drain body before exiting stream ctx
                    cur = str(httpx.URL(cur).join(loc))
                    continue
                chunks: list[bytes] = []
                total = 0
                try:
                    for chunk in streamed.iter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > _MAX_RESPONSE_BYTES:
                            raise ValueError(
                                f"response body exceeded {_MAX_RESPONSE_BYTES} bytes "
                                f"for {cur!r}; aborting for memory safety"
                            )
                except httpx.DecodingError:
                    # Wire bytes whose declared Content-Encoding genuinely cannot be
                    # decoded (e.g. a Cloudflare "Just a moment…" challenge that
                    # mislabels its body). Don't mask the real HTTP status as a
                    # cryptic "Error -3 … incorrect header check": surface the status
                    # with an empty body so the caller's status check reports the
                    # honest code (e.g. HTTP 403). status_code / headers are buffered
                    # before the body, so they remain valid here; an undecodable body
                    # is unusable anyway.
                    return _rebuffer_response(streamed, b"")
                return _rebuffer_response(streamed, b"".join(chunks))
        raise ValueError(f"too many redirects (>{_MAX_REDIRECTS}) following {url!r}")

# Source types whose fetchers are intentional stubs — they always return
# `FetchOutcome(skipped=1)`. Surfaced in `IngestSource.to_dict()` as
# `is_stub: True` so the frontend can render an amber STUB badge, and the
# scheduler skips them entirely (no event-log spam from sources that can
# never ingest).
#
# P6 update: twitter_tag / twitter_article are no longer stubs — they now
# route through Nitter RSS bridges. Operators who don't want that can leave
# the source disabled. Only TikTok remains a stub (no public scraping path).
# NOTE: `manual` is NOT a stub — it is the deliberate +INGEST-button target.
STUB_SOURCE_TYPES: frozenset[str] = frozenset({
    "tiktok",
})

# Default public Nitter mirror. Operators can override per deployment.
_DEFAULT_NITTER = "https://nitter.privacydev.net"


def is_stub_source_type(source_type: str) -> bool:
    return (source_type or "").strip().lower() in STUB_SOURCE_TYPES

# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------


@dataclass
class FetchedItem:
    title: str
    url: str
    body_markdown: str
    published_at: Optional[datetime] = None
    raw_html: str = ""
    # Optional override for the content-hash dedup key. When an enricher appends
    # VOLATILE content to ``body_markdown`` (e.g. Reddit comments, whose scores
    # and set drift between polls), hashing the full body would mint a brand-new
    # KnowledgeNode every poll. Set this to the STABLE part (the post body) so
    # the same thread dedups to one node. ``None`` → hash ``body_markdown`` as
    # before (correct for article/arXiv enrichment, whose full text is stable).
    dedup_body: Optional[str] = None


@dataclass
class FetchOutcome:
    items: List[FetchedItem] = field(default_factory=list)
    skipped: int = 0
    error: Optional[str] = None
    notes: str = ""
    retry_after_seconds: Optional[int] = None


def _content_hash(title: str, url: str, body: str) -> str:
    """Stable dedup key for a single feed item.

    The body is capped at ``KB_CONTENT_MAX_CHARS`` — the SAME constant used to
    cap the stored ``content`` column, so the hash and the stored body always
    slice at the same boundary (a mismatch would falsely dedup two articles that
    share a long prefix). The cap was 4096 → 32 000 → 150 000. The 4096→32 000
    widening shipped a companion ``p_recompute_content_hash_32k`` migration
    because rows longer than 4096 then had stale hashes. The 32 000→150 000
    widening needed NO migration: every pre-existing row was written under the
    32 000 cap (content ≤ 32 000), so ``content[:150000] == content[:32000]`` and
    their hashes are unchanged. Primary re-poll dedup is also now by
    ``source_url`` (see ``scheduler._persist_outcome``), so content_hash no
    longer has to match across polls. If you NARROW the cap, or widen it while
    rows already exceed the current cap, add the next keyed recompute migration.
    """
    h = hashlib.sha256()
    h.update((title or "").strip().encode("utf-8", errors="ignore"))
    h.update(b"|")
    h.update((url or "").strip().encode("utf-8", errors="ignore"))
    h.update(b"|")
    h.update((body or "").strip()[:KB_CONTENT_MAX_CHARS].encode("utf-8", errors="ignore"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# RSS-style fetchers
# ---------------------------------------------------------------------------


# CMS "style guide" / lorem-ipsum demo blocks (notably Webflow's default
# rich-text element: "Heading 1..6", a Lorem-ipsum paragraph, then "Block quote /
# Ordered list / Bold text / Emphasis / Superscript / Subscript") are sometimes
# present in a page's HTML and get scraped onto the END of an article body.
# Anchor on placeholder strings that never occur in real prose; strip trailing-only.
_BOILERPLATE_TAIL_RE = re.compile(
    r"\n\s*(?:"
    r"Heading 1\s*\n\s*Heading 2\s*\n\s*Heading 3\s*\n\s*"
    r"Heading 4\s*\n\s*Heading 5\s*\n\s*Heading 6"
    r"|Lorem ipsum dolor sit amet"
    r")\b.*\Z",
    re.IGNORECASE | re.DOTALL,
)


def _strip_boilerplate_tail(text: str) -> str:
    """Remove a trailing CMS style-guide / lorem-ipsum template block.

    Conservative by construction: matches only at the END of the text, anchors on
    placeholder strings ("Heading 1..6" run, "Lorem ipsum dolor sit amet") that
    never appear in genuine article prose, and is a no-op unless the result keeps
    the bulk of the body — so a freak over-match can never gut a real article.
    """
    if not text:
        return text
    stripped = _BOILERPLATE_TAIL_RE.sub("", text).rstrip()
    if stripped and len(stripped) >= 200 and len(stripped) >= len(text) // 3:
        return stripped
    return text


def _strip_html(html: str) -> str:
    """Cheap HTML stripper — keeps content readable without pulling lxml."""
    if not html:
        return ""
    import html as _htmllib
    import re

    # Drop scripts + styles entirely.
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.IGNORECASE | re.DOTALL)
    # Replace block tags with newlines.
    cleaned = re.sub(r"<(br|p|div|h[1-6]|li)[^>]*>", "\n", cleaned, flags=re.IGNORECASE)
    # Strip remaining tags.
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    # Decode HTML entities AFTER tag removal so feed bodies read as text, not
    # "S&amp;P 500" / "doesn&#8217;t". Done after stripping so a decoded '<' from
    # an escaped "&lt;tag&gt;" stays literal text rather than re-forming a tag.
    cleaned = _htmllib.unescape(cleaned)
    # Normalise non-breaking spaces (decoded from &nbsp;) to ordinary spaces.
    cleaned = cleaned.replace("\xa0", " ")
    # Collapse runs of whitespace.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    # Drop a trailing CMS style-guide / lorem-ipsum template block if the feed's
    # full-content HTML bundled one (e.g. Webflow blogs like Paradigm).
    return _strip_boilerplate_tail(cleaned.strip())


def _parse_feed(
    url: str,
    max_items: int = 10,
    extra_headers: Optional[Dict[str, str]] = None,
) -> FetchOutcome:
    """Generic feedparser-based fetcher shared by every RSS-like source.

    ``extra_headers`` merges on top of the default User-Agent so individual
    fetchers (e.g. Reddit) can pass additional browser-like headers without
    affecting every other source type.
    """
    try:
        import feedparser  # type: ignore
    except ImportError:
        return FetchOutcome(error="feedparser not installed (pip install feedparser)")

    # Browser-like UA + feed Accept by default so Cloudflare-fronted feeds (e.g.
    # Glassnode Insights) don't answer with a 403 challenge page. Per-source
    # overrides (e.g. _REDDIT_HEADERS) still win via the merge below.
    headers: Dict[str, str] = {"User-Agent": USER_AGENT, "Accept": _FEED_ACCEPT}
    if extra_headers:
        headers.update(extra_headers)
    try:
        # P30-S3: SSRF-safe fetch with per-hop redirect validation.
        resp = _safe_get(url, headers=headers)
        if resp.status_code == 429:
            retry_after: Optional[int] = None
            raw_ra = resp.headers.get("Retry-After", "")
            if raw_ra.strip().isdigit():
                retry_after = int(raw_ra.strip())
            elif raw_ra.strip():
                # R5/SRE-008: Retry-After may be an HTTP-date, not delta-seconds.
                try:
                    _ra_dt = parsedate_to_datetime(raw_ra.strip())
                    if _ra_dt is not None:
                        if _ra_dt.tzinfo is None:
                            _ra_dt = _ra_dt.replace(tzinfo=timezone.utc)
                        _ra_delta = (_ra_dt - datetime.now(timezone.utc)).total_seconds()
                        if _ra_delta > 0:
                            retry_after = int(_ra_delta)
                except (TypeError, ValueError):
                    retry_after = None
            return FetchOutcome(error="HTTP 429", retry_after_seconds=retry_after)
        if resp.status_code >= 400:
            return FetchOutcome(error=f"HTTP {resp.status_code}")
        payload = resp.content
    except ValueError as exc:
        # SSRF guard rejected initial URL or a redirect hop. Generic error
        # message — don't leak the internal address the chain pointed at.
        return FetchOutcome(error=f"refused: {exc}")
    except httpx.RequestError as exc:
        return FetchOutcome(error=f"network: {exc}")
    except Exception as exc:  # noqa: BLE001
        return FetchOutcome(error=f"fetch: {exc}")

    try:
        parsed = feedparser.parse(payload)
    except Exception as exc:  # noqa: BLE001
        return FetchOutcome(error=f"feed parse: {exc}")

    if parsed.bozo and not parsed.entries:
        return FetchOutcome(error=f"bozo: {parsed.bozo_exception}")

    items: List[FetchedItem] = []
    for entry in (parsed.entries or [])[:max_items]:
        title = (entry.get("title") or "").strip() or "(untitled)"
        link = (entry.get("link") or "").strip()
        # Feeds that ship full text put it in content:encoded (feedparser's
        # entry.content) while summary/description holds a short teaser.
        # Preferring summary unconditionally silently discarded the full
        # article (and its inline images) for every Ghost/WordPress feed —
        # keep whichever block is richer.
        summary_html = entry.get("summary") or ""
        content_blocks = entry.get("content") or []
        if content_blocks:
            content_html = content_blocks[0].get("value", "") or ""
            if len(content_html) > len(summary_html):
                summary_html = content_html
        body = _strip_html(summary_html)

        published_at: Optional[datetime] = None
        for ts_key in ("published_parsed", "updated_parsed"):
            ts = entry.get(ts_key)
            if ts:
                try:
                    published_at = datetime(*ts[:6], tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    published_at = None
                break

        items.append(
            FetchedItem(
                title=title,
                url=link,
                body_markdown=body,
                published_at=published_at,
                raw_html=summary_html,
            )
        )

    # ── Optional full-text enrichment ─────────────────────────────────────────
    # Controlled by ARTICLE_ENRICH_ENABLED (default 0/False).
    # Lazy import avoids circular dependency at module-load time.
    # Any exception here must NOT break the main fetch path.
    if items and env_bool("ARTICLE_ENRICH_ENABLED", False):
        try:
            from backend.core.article_enricher import enrich_items  # noqa: PLC0415
            items = enrich_items(items)
        except Exception:  # noqa: BLE001
            logger.debug(
                "article_enricher: enrichment pass raised unexpectedly", exc_info=True
            )

    return FetchOutcome(items=items)


def fetch_rss(src: IngestSource) -> FetchOutcome:
    return _parse_feed(src.url)


def fetch_substack(src: IngestSource) -> FetchOutcome:
    """Substack newsletters expose RSS at <newsletter>/feed."""
    url = src.url.rstrip("/")
    if not url.endswith("/feed"):
        url = f"{url}/feed"
    return _parse_feed(url)


def fetch_medium(src: IngestSource) -> FetchOutcome:
    """Medium publications expose RSS at https://medium.com/feed/<publication>."""
    url = src.url.rstrip("/")
    if "medium.com" in url and "/feed" not in url:
        # Normalize e.g. https://medium.com/@user → https://medium.com/feed/@user
        # Use urlparse to manipulate only the path root so that any path
        # segment coincidentally containing 'medium.com/' is not rewritten.
        from urllib.parse import urlparse  # stdlib, no new dependency
        _p = urlparse(url)
        url = _p._replace(path="/feed" + _p.path).geturl()
    return _parse_feed(url)


def fetch_patreon(src: IngestSource) -> FetchOutcome:
    """Fetch a Patreon creator's audio/podcast RSS feed.

    IMPORTANT LIMITATION: Patreon RSS feeds contain ONLY audio attachments
    (podcast episodes). Text posts, image posts, and video posts are NOT
    included — those require the Patreon web API with OAuth, which this system
    does not implement.

    URL format: https://www.patreon.com/rss/<creator>?auth=<member_token>
    The ?auth= token is unique to each paying member and is found in the
    member's Patreon settings → Membership → RSS link.  Without a valid token
    the feed returns no items (not an error — Patreon silently filters them).
    """
    return _parse_feed(src.url)


_YT_VIDEO_ID_RE = re.compile(r"(?:youtu\.be/|v=|/embed/|/shorts/)([A-Za-z0-9_-]{11})")

# Channel-id extraction patterns, MOST authoritative first. `externalId` and the
# canonical <link> reliably name the page's OWN channel; a bare `channelId":"`
# can belong to a *recommended* channel elsewhere on the page, so it is only a
# last resort. Searched over (most of) the full body — YouTube channel pages run
# ~1.8–2.4 MB and these markers routinely sit well past the first 500 KB, so the
# previous `[:500_000]` slice missed them and resolution silently failed.
_YT_CHANNEL_ID_PATTERNS = (
    re.compile(r'"externalId":"(UC[\w-]{22})"'),
    re.compile(r'rel="canonical"[^>]*?/channel/(UC[\w-]{22})'),
    re.compile(r'"channelId":"(UC[\w-]{22})"'),
    re.compile(r'/channel/(UC[\w-]{22})'),
)


def _extract_youtube_channel_id(html: str) -> Optional[str]:
    """Return the page's own ``UC…`` channel id from scraped HTML, or None.

    Prefers authoritative markers (externalId / canonical link) over a bare
    ``channelId``, which on a channel page may name a *recommended* channel
    rather than the page's own.
    """
    for pat in _YT_CHANNEL_ID_PATTERNS:
        m = pat.search(html or "")
        if m:
            return m.group(1)
    return None


def _resolve_youtube_channel(url: str) -> str:
    """L17 — accept @handle / /c/ / /user/ / channel_id / feed URLs.

    HTML-scrapes the channel page when the URL isn't already a channel-id
    feed. Falls back to the original URL on failure (callers downstream will
    log a feedparser error rather than crash).
    """
    raw = (url or "").strip()
    if not raw:
        return raw
    if "feeds/videos.xml" in raw:
        return raw
    if raw.startswith("UC") and len(raw) >= 24:
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={raw}"
    if "/channel/" in raw:
        chan = raw.rsplit("/channel/", 1)[1].split("/", 1)[0].split("?", 1)[0]
        if chan.startswith("UC"):
            return f"https://www.youtube.com/feeds/videos.xml?channel_id={chan}"
    # @handle / /c/ / /user/ — needs scrape resolve.
    # P30-S3: route via SSRF-safe httpx path. urllib.request honours system
    # proxies and resolves DNS itself, bypassing our per-hop guard.
    try:
        resp = _safe_get(raw, headers={"User-Agent": USER_AGENT})
        if resp.status_code >= 400:
            return raw
        # Search (most of) the full body — channel pages are ~2 MB and the id
        # markers often sit past 500 KB. The 4 MB cap bounds worst-case regex
        # cost (body is already capped at _MAX_RESPONSE_BYTES upstream).
        channel_id = _extract_youtube_channel_id(resp.text[:4_000_000])
        if channel_id:
            return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    except Exception as exc:  # noqa: BLE001
        logger.debug("YouTube handle resolve failed for %s: %s", raw, exc)
    return raw


def _fetch_youtube_transcript(video_id: str) -> str:
    """Returns concatenated transcript text or empty string. Never raises."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    except ImportError:
        return ""
    # R5/SRE-002: YouTubeTranscriptApi.get_transcript has no timeout and runs
    # synchronously inside asyncio.to_thread while holding the per-source poll lock;
    # a hung timedtext request would block the worker thread (and the lock) forever.
    # Run it in a 1-worker pool with a hard 20s ceiling.
    import concurrent.futures as _cf

    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
            _fut = _ex.submit(
                YouTubeTranscriptApi.get_transcript,
                video_id,
                languages=["en", "zh-Hant", "zh-Hans", "zh", "ja"],
            )
            try:
                segments = _fut.result(timeout=20)
            except _cf.TimeoutError:
                logger.debug("YouTube transcript timed out for %s", video_id)
                return ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("YouTube transcript unavailable for %s: %s", video_id, exc)
        return ""
    return " ".join(seg.get("text", "").strip() for seg in segments if seg.get("text"))


_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def fetch_url_text(url: str, *, max_chars: int = 24000) -> tuple[str, str]:
    """Fetch a single public URL and return ``(title, body_text)`` for intake.

    Used by the paste-a-link intake path (``process_text_to_node``): YouTube
    links return the transcript (via :func:`_fetch_youtube_transcript`),
    everything else is fetched through the SSRF-guarded :func:`_safe_get` and
    stripped to readable text. The connection is host-validated and IP-pinned
    exactly like every other fetcher in this module — no new network surface.

    Raises ``ValueError`` on a non-http(s) url, an HTTP error, a missing
    transcript, or an empty body so the caller can surface a clear failure
    instead of persisting a useless "here is a link" node.
    """
    raw = (url or "").strip()
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raise ValueError(f"not an http(s) url: {raw!r}")
    m = _YT_VIDEO_ID_RE.search(raw)
    if m:
        transcript = _fetch_youtube_transcript(m.group(1))
        if not transcript.strip():
            raise ValueError(
                f"no transcript for YouTube video {m.group(1)} "
                "(none published, age-restricted, or youtube_transcript_api missing)"
            )
        return (f"YouTube video {m.group(1)}", transcript[:max_chars])
    # Browser UA so Cloudflare-fronted links resolve; let httpx negotiate/decode
    # compression normally. The historical `Accept-Encoding: identity` workaround
    # for "Error -3 … incorrect header check" is no longer needed — _safe_get now
    # strips stale encoding headers before rebuilding (see _rebuffer_response) —
    # and identity itself re-triggers Cloudflare challenges, so it is removed.
    resp = _safe_get(raw, headers={"User-Agent": USER_AGENT})
    if resp.status_code >= 400:
        raise ValueError(f"fetch failed: HTTP {resp.status_code} for {raw!r}")
    ctype = (resp.headers.get("content-type") or "").lower()
    body_raw = resp.text or ""
    if "html" in ctype or body_raw.lstrip()[:1] == "<":
        tm = _HTML_TITLE_RE.search(body_raw)
        title = (tm.group(1).strip() if tm else "") or raw
        for _a, _b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'"), ("&quot;", '"')):
            title = title.replace(_a, _b)
        body = _strip_html(body_raw)
    else:
        title = raw
        body = body_raw.strip()
    body = body.strip()
    if not body:
        raise ValueError(f"no extractable text at {raw!r}")
    return (title[:512], body[:max_chars])


def fetch_youtube(src: IngestSource) -> FetchOutcome:
    """YouTube channels expose RSS at /feeds/videos.xml?channel_id=<UC...>.

    P6: resolves @handle / /c/ / /user/ URLs to channel_id, and optionally
    enriches each entry's body with the video transcript when
    ``YT_TRANSCRIPT_ENABLED`` is on.
    """
    feed_url = _resolve_youtube_channel(src.url)
    outcome = _parse_feed(feed_url)
    if outcome.error or not outcome.items:
        return outcome

    if env_bool("YT_TRANSCRIPT_ENABLED", False):
        for item in outcome.items:
            m = _YT_VIDEO_ID_RE.search(item.url or "")
            if not m:
                continue
            transcript = _fetch_youtube_transcript(m.group(1))
            if transcript:
                # Cap to 24k chars so 100k-word transcripts don't blow the
                # KnowledgeNode 32k content cap when concatenated with the feed
                # description.
                transcript = transcript[:24000]
                item.body_markdown = (
                    f"{item.body_markdown}\n\n## Transcript\n\n{transcript}"
                    if item.body_markdown
                    else f"## Transcript\n\n{transcript}"
                )
    return outcome


_REDDIT_HEADERS: Dict[str, str] = {
    # Reddit's bot-detection checks both User-Agent AND Accept headers. The
    # default USER_AGENT is already browser-like, but Reddit wants an HTML-shaped
    # Accept (not the XML-shaped feed Accept) plus Accept-Language, so this source
    # type pins its own header set. Kept explicit so Reddit tuning never disturbs
    # the shared feed defaults.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# Reddit comment enrichment (opt-in via REDDIT_FETCH_COMMENTS, default ON)
# ---------------------------------------------------------------------------
#
# A subreddit's .rss feed returns only the SUBMISSIONS (title + a selftext
# teaser), never the discussion. For r/quant and r/algotrading the actual signal
# is very often in the COMMENTS — the answers, counter-arguments, and references.
# When enabled, we fetch the comments of the most-recent posts and append them to
# the post body.
#
# Transport: Reddit's per-post JSON API (`.json`) now 403s browser-UA / non-OAuth
# clients outright (verified 2026-06 from the deployment IP — both www and
# old.reddit.com). The per-post COMMENT *.rss* feed, however, is still served
# (same RSS surface the subreddit feed already relies on), so we parse that with
# feedparser. RSS carries no score, so we rank by Reddit's own feed order
# (`sort=top`) and quality-filter by author + a minimum body length instead.
#
# Cost guards mirror the rest of the ingest layer:
#   REDDIT_FETCH_COMMENTS       master switch                      (default true)
#   REDDIT_COMMENTS_MAX_POSTS   posts whose comments we fetch/poll (default 5)
#   REDDIT_COMMENTS_PER_POST    comments kept per post             (default 6)
#   REDDIT_COMMENTS_MIN_CHARS   drop comments shorter than this    (default 40)
#   REDDIT_COMMENTS_TIMEOUT     per-request timeout, seconds       (default 8)
#   REDDIT_COMMENTS_BUDGET      total wall-clock budget, seconds   (default 25)
#   REDDIT_COMMENTS_MAX_CHARS   cap on the appended comment block  (default 8000)
#
# Why this can't simply hash the enriched body: the comment set drifts between
# polls, so hashing the full body would mint a fresh KnowledgeNode every cycle.
# We set FetchedItem.dedup_body to the pre-comment post body so a thread dedups
# to a single node even as its discussion evolves.

_REDDIT_PERMALINK_RE = re.compile(r"reddit\.com/r/[^/]+/comments/[a-z0-9]+", re.IGNORECASE)
# A COMMENT permalink has an extra base-36 id segment AFTER the post slug
# (`.../comments/<postid>/<slug>/<commentid>/`); the post's own <entry> — which
# Reddit emits as the first item of a comment .rss feed — ends at the slug and so
# does NOT match. This is how we drop the OP self-post echo without guessing by
# position (link posts and self posts lay the feed out differently).
_REDDIT_COMMENT_LINK_RE = re.compile(
    r"/comments/[a-z0-9]+/[^/]+/[a-z0-9]+/?(?:[?#]|$)", re.IGNORECASE
)
# Authors that are never substantive discussion. Compared case-insensitively
# after the `/u/` prefix is stripped.
_REDDIT_SKIP_AUTHORS = frozenset({"", "automoderator", "[deleted]"})


def _reddit_comment_rss_url(permalink: str, *, limit: int) -> Optional[str]:
    """Map a Reddit post permalink → its per-post COMMENT ``.rss`` feed, or None.

    Only genuine ``/r/<sub>/comments/<id>/...`` permalinks qualify (skips user
    feeds and any non-post link). Query/fragment and any existing ``.rss``/
    ``.json`` suffix are stripped before ``/.rss?sort=top&limit=<n>`` is appended.
    """
    if not permalink or not _REDDIT_PERMALINK_RE.search(permalink):
        return None
    base = permalink.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if base.endswith(".rss") or base.endswith(".json"):
        base = base.rsplit(".", 1)[0].rstrip("/")
    return f"{base}/.rss?sort=top&limit={int(limit)}"


def _reddit_author(raw: str) -> str:
    """Normalise a feed author (``/u/name``, ``u/name``, ``/user/name``) → ``name``."""
    a = (raw or "").strip()
    for pfx in ("/u/", "u/", "/user/", "user/"):
        if a.lower().startswith(pfx):
            a = a[len(pfx):]
            break
    return a.strip().strip("/")


def _reddit_entry_body(entry) -> str:
    """Extract a comment entry's body text, preferring content:encoded over the
    summary (same richer-block rule as ``_parse_feed``), then strip HTML."""
    summary_html = entry.get("summary") or ""
    blocks = entry.get("content") or []
    if blocks:
        first = blocks[0]
        cval = first.get("value", "") if isinstance(first, dict) else ""
        if cval and len(cval) > len(summary_html):
            summary_html = cval
    return _strip_html(summary_html)


def _format_reddit_comments(
    entries: list, *, per_post: int, min_chars: int, max_chars: int
) -> str:
    """Render comment-feed entries → a ``## Top comments`` markdown block.

    ``entries`` are feedparser entries from a post's comment ``.rss`` feed. Keeps
    only genuine comments (link carries a comment id — drops the OP post echo),
    skips bot / deleted / too-short bodies, preserves feed order (``sort=top``),
    keeps the first ``per_post``, and caps total length. ``""`` when none qualify.
    """
    parts: List[str] = []
    for entry in entries or []:
        if len(parts) >= per_post:
            break
        link = entry.get("link") or "" if hasattr(entry, "get") else ""
        if not _REDDIT_COMMENT_LINK_RE.search(link):
            continue  # the post's own entry, or a malformed/non-comment link
        author = _reddit_author(entry.get("author") or "")
        if author.lower() in _REDDIT_SKIP_AUTHORS:
            continue
        body = _reddit_entry_body(entry)
        if not body or body in ("[deleted]", "[removed]") or len(body) < min_chars:
            continue
        label = f"**u/{author}**" if author else "**comment**"
        parts.append(f"{label}\n\n{body}")
    if not parts:
        return ""
    return ("## Top comments\n\n" + "\n\n".join(parts))[:max_chars].rstrip()


def _fetch_post_comments(
    permalink: str, *, timeout_s: float, per_post: int, min_chars: int, max_chars: int
) -> Optional[str]:
    """Fetch + format the top comments for ONE Reddit post via its comment ``.rss``.

    None on any failure. Mirrors ``fetch_reddit``'s www→old.reddit.com 403
    fallback. Best-effort: every failure mode returns None so the post is still
    ingested without comments. Routes through ``_safe_get`` for the same SSRF +
    byte-cap + redirect protections as every other outbound fetch.
    """
    # Pull several more than we keep so the author/length filters still leave
    # ``per_post`` comments to show.
    url = _reddit_comment_rss_url(permalink, limit=max(25, min(per_post * 5, 100)))
    if not url:
        return None
    try:
        import feedparser  # type: ignore
    except ImportError:
        return None
    timeout = httpx.Timeout(timeout_s, connect=min(timeout_s, 5.0))

    def _entries(u: str) -> Optional[list]:
        try:
            resp = _safe_get(u, headers=_REDDIT_HEADERS, timeout=timeout)
        except (ValueError, httpx.RequestError):
            return None
        except Exception:  # noqa: BLE001
            return None
        if resp.status_code != 200:
            return None
        try:
            parsed = feedparser.parse(resp.content)
        except Exception:  # noqa: BLE001
            return None
        return list(parsed.entries or [])

    entries = _entries(url)
    if not entries and "www.reddit.com" in url:
        entries = _entries(url.replace("www.reddit.com", "old.reddit.com", 1))
    if not entries:
        return None
    block = _format_reddit_comments(
        entries, per_post=per_post, min_chars=min_chars, max_chars=max_chars
    )
    return block or None


def _enrich_reddit_comments(items: List[FetchedItem]) -> None:
    """Append top comments to the most-recent Reddit posts, mutating in place.

    Bounded by both a post-count cap and a wall-clock budget. Sets
    ``dedup_body`` to the pre-comment body so a thread dedups to one node even as
    its comments evolve. Never raises — a per-post failure leaves that post
    un-enriched but still ingested.
    """
    if not items:
        return
    max_posts = env_int("REDDIT_COMMENTS_MAX_POSTS", 5, minimum=0, maximum=25)
    if max_posts <= 0:
        return
    per_post = env_int("REDDIT_COMMENTS_PER_POST", 6, minimum=1, maximum=30)
    min_chars = env_int("REDDIT_COMMENTS_MIN_CHARS", 40, minimum=1, maximum=5000)
    timeout_s = float(env_int("REDDIT_COMMENTS_TIMEOUT", 8, minimum=2, maximum=30))
    budget_s = float(env_int("REDDIT_COMMENTS_BUDGET", 25, minimum=5, maximum=180))
    max_chars = env_int("REDDIT_COMMENTS_MAX_CHARS", 8000, minimum=500, maximum=30000)
    # Spacing between consecutive comment fetches so one poll doesn't burst
    # Reddit's per-IP rate limiter — the dominant cause of HTTP 429 on the .rss
    # surface. Set REDDIT_COMMENTS_DELAY_MS=0 to restore the old back-to-back burst.
    delay_s = env_int("REDDIT_COMMENTS_DELAY_MS", 1000, minimum=0, maximum=10000) / 1000.0

    deadline = time.monotonic() + budget_s
    attempted = 0
    for item in items:
        if attempted >= max_posts:
            break
        if not item.url or not _REDDIT_PERMALINK_RE.search(item.url):
            continue  # non-post link (user feed etc.) — doesn't count toward cap
        if time.monotonic() >= deadline:
            logger.debug("reddit comments: wall-clock budget exhausted")
            break
        # First fetch fires immediately; only the subsequent ones wait, so a
        # single post still enriches with zero added latency. Clamp the wait to
        # the remaining budget so spacing can never push past the deadline above.
        if attempted > 0 and delay_s > 0.0:
            time.sleep(min(delay_s, max(0.0, deadline - time.monotonic())))
        attempted += 1  # counts the network attempt (success or not) toward cap
        try:
            block = _fetch_post_comments(
                item.url,
                timeout_s=timeout_s,
                per_post=per_post,
                min_chars=min_chars,
                max_chars=max_chars,
            )
        except Exception:  # noqa: BLE001
            logger.debug("reddit comments: fetch raised for %s", item.url, exc_info=True)
            block = None
        if not block:
            continue
        # Pin the dedup hash to the STABLE post body before appending comments.
        if item.dedup_body is None:
            item.dedup_body = item.body_markdown
        sep = "\n\n" if item.body_markdown.strip() else ""
        item.body_markdown = f"{item.body_markdown}{sep}{block}"


def fetch_reddit(src: IngestSource) -> FetchOutcome:
    """Reddit subreddits/users expose .rss endpoints; use whichever the user pasted.

    Bot-detection mitigation:
    * Uses a browser-like User-Agent + Accept headers (Reddit blocks custom UAs).
    * Tries www.reddit.com first; if that returns 403, falls back to
      old.reddit.com which still has lighter bot-filtering as of 2026.

    Comment enrichment (P-REDDIT-COMMENTS): the .rss feed carries only the post
    bodies; when ``REDDIT_FETCH_COMMENTS`` is enabled (default) we additionally
    pull the top comments of the most-recent posts so the KB captures the
    discussion, not just the prompt. See ``_enrich_reddit_comments``.
    """
    url = src.url.rstrip("/")
    if not url.endswith(".rss") and not url.endswith(".json"):
        url = url + ".rss"

    outcome = _parse_feed(url, extra_headers=_REDDIT_HEADERS)

    # Fallback: if www.reddit.com blocked us, retry with old.reddit.com.
    # Only rewrite the host — preserve the full path and query string.
    if outcome.error and "403" in (outcome.error or "") and "www.reddit.com" in url:
        fallback_url = url.replace("www.reddit.com", "old.reddit.com", 1)
        logger.debug(
            "fetch_reddit: www returned 403, retrying via old.reddit.com (%s)",
            fallback_url,
        )
        outcome = _parse_feed(fallback_url, extra_headers=_REDDIT_HEADERS)

    # Opt-in: append top comments to each post. Best-effort — never let a
    # comment-fetch failure turn a healthy feed fetch into an error.
    if outcome.items and not outcome.error and env_bool("REDDIT_FETCH_COMMENTS", True):
        try:
            _enrich_reddit_comments(outcome.items)
        except Exception:  # noqa: BLE001
            logger.debug("reddit comment enrichment raised unexpectedly", exc_info=True)

    return outcome


# ---------------------------------------------------------------------------
# P6-B4 — Nitter-bridged Twitter fetchers
# ---------------------------------------------------------------------------


def _nitter_base() -> str:
    """Configurable Nitter mirror — public instances rotate frequently."""
    return env_str("NITTER_INSTANCE_URL", _DEFAULT_NITTER).rstrip("/")


def _twitter_handle(url: str) -> Optional[str]:
    """Extract the @handle from any plausible URL/handle the user might paste."""
    raw = (url or "").strip().lstrip("@")
    if not raw:
        return None
    # If they pasted a full URL: https://twitter.com/elonmusk → "elonmusk"
    for prefix in ("https://twitter.com/", "https://x.com/",
                   "http://twitter.com/", "http://x.com/"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):]
            break
    raw = raw.split("/", 1)[0].split("?", 1)[0]
    # Twitter handles: 1-15 alnum/underscore.
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", raw):
        return None
    return raw


def fetch_twitter_tag(src: IngestSource) -> FetchOutcome:
    """Twitter handle/tag fetcher via Nitter RSS bridge.

    Source URL can be a Twitter handle (``elonmusk``), full URL
    (``https://twitter.com/elonmusk``), or a hashtag URL — the latter is
    nitter's ``/search/rss?q=%23foo&f=tweets``.
    """
    raw = (src.url or "").strip()
    base = _nitter_base()
    if raw.startswith("#") or "%23" in raw or "/search" in raw:
        # If the user pasted a full Twitter/Nitter search URL, extract the
        # bare query term instead of percent-encoding the entire URL.
        if raw.startswith(("http://", "https://")):
            _parsed = urllib.parse.urlparse(raw)
            _q = urllib.parse.parse_qs(_parsed.query).get("q", [""])[0]
            # Strip any leading # or %23 that the browser may have included.
            query = urllib.parse.unquote(_q).lstrip("#")
        else:
            query = urllib.parse.unquote(raw).lstrip("#")
        url = f"{base}/search/rss?q=%23{urllib.parse.quote(query, safe='')}&f=tweets"
    else:
        handle = _twitter_handle(raw)
        if not handle:
            return FetchOutcome(error=f"Invalid Twitter handle/URL: {raw[:80]}")
        url = f"{base}/{handle}/rss"
    outcome = _parse_feed(url)
    if outcome.error:
        # Nitter mirrors rotate; surface a clear hint.
        outcome = FetchOutcome(
            error=f"{outcome.error} (Nitter via {base}; try NITTER_INSTANCE_URL override)",
        )
    return outcome


def fetch_twitter_article(src: IngestSource) -> FetchOutcome:
    """Twitter "article" view — same Nitter RSS path as the tag fetcher."""
    return fetch_twitter_tag(src)


def fetch_tiktok(src: IngestSource) -> FetchOutcome:  # noqa: ARG001
    return FetchOutcome(
        skipped=1,
        notes="TikTok fetcher deferred — no stable public API.",
    )


def fetch_manual(src: IngestSource) -> FetchOutcome:  # noqa: ARG001
    return FetchOutcome(
        skipped=1,
        notes="Manual source — content arrives via the +INGEST dialog, not the scheduler.",
    )


# ---------------------------------------------------------------------------
# P6-B4 — arXiv fetcher
# ---------------------------------------------------------------------------


_IMG_TAG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=["\'])([^"\']+)(["\'])', re.IGNORECASE)


def _absolutize_img_srcs(html: str, base_url: str) -> str:
    """Rewrite relative ``<img src>`` values to absolute URLs via ``base_url``.

    The asset cache (asset_cache.cache_images_for_html) only downloads
    http(s):// URLs and silently skips relative ones, so any fetched article
    HTML must be absolutized before it is stored in ``FetchedItem.raw_html``.
    Absolute and ``data:`` sources pass through untouched; protocol-relative
    ``//host/...`` sources inherit the base scheme. Never raises.
    """
    if not html or not base_url:
        return html or ""

    def _fix(m: "re.Match[str]") -> str:
        src = m.group(2).strip()
        if src.startswith(("http://", "https://", "data:")):
            return m.group(0)
        try:
            absolute = urljoin(base_url, src)
        except ValueError:
            return m.group(0)
        return f"{m.group(1)}{absolute}{m.group(3)}"

    return _IMG_TAG_SRC_RE.sub(_fix, html)


def _arxiv_html_url(entry_id: str) -> Optional[str]:
    """Map an arXiv abstract URL to its official full-text HTML rendering.

    ``http://arxiv.org/abs/2506.01234v1`` → ``https://arxiv.org/html/2506.01234v1``.
    arXiv publishes HTML for most papers submitted since 2023-12; older or
    failed-conversion papers 404 there, in which case the caller keeps the
    abstract. Returns None when the URL is not an arXiv abstract link.
    """
    m = re.search(r"arxiv\.org/abs/(?P<pid>[^?#]+)", entry_id or "", re.IGNORECASE)
    if not m:
        return None
    return f"https://arxiv.org/html/{m.group('pid')}"


def _enrich_arxiv_fulltext(items: List["FetchedItem"]) -> List["FetchedItem"]:
    """Replace abstract-only bodies with the paper's full text where available.

    The arXiv API only returns the abstract, so without this pass every paper
    node holds ~1 paragraph of "surface" content. Fetches the official
    ``arxiv.org/html/<id>`` rendering and extracts the body via the same
    trafilatura pipeline the RSS enricher uses. Honours the shared
    ARTICLE_ENRICH_MAX_ITEMS / ARTICLE_ENRICH_TIMEOUT budgets; any failure
    (no HTML version, network error, thin extraction) keeps the abstract.
    Never raises.
    """
    # Lazy import mirrors _parse_feed's enrichment hook (avoids module-load
    # circular dependency between ingest_fetchers and article_enricher).
    from backend.core.article_enricher import _extract_text, _max_items, _timeout_s  # noqa: PLC0415

    budget = _max_items()
    timeout = httpx.Timeout(float(_timeout_s()), connect=8.0)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    out: List[FetchedItem] = []
    enriched = 0
    for item in items:
        html_url = _arxiv_html_url(item.url) if enriched < budget else None
        if not html_url:
            out.append(item)
            continue
        text = ""
        page_html = ""
        try:
            resp = _safe_get(html_url, headers=headers, timeout=timeout)
            if resp.status_code < 400 and resp.content:
                text = _extract_text(resp.content)
                if text:
                    page_html = resp.text
        except Exception:  # noqa: BLE001
            logger.debug("arxiv fulltext fetch failed for %s", html_url, exc_info=True)
        if len(text) > len(item.body_markdown or ""):
            out.append(
                FetchedItem(
                    title=item.title,
                    url=item.url,
                    # Same cap as article_enricher._replace_body — the
                    # KnowledgeNode.content column limit.
                    body_markdown=text[:KB_CONTENT_MAX_CHARS],
                    published_at=item.published_at,
                    # Carry the rendering so the asset cache can pick up the
                    # paper's figures. arXiv HTML references them relative to
                    # the page directory, hence the trailing-slash base. Site
                    # chrome (the arxiv.org/static/ logo etc.) is dropped so it
                    # doesn't burn an image slot on every single paper.
                    raw_html=re.sub(
                        r'<img\b[^>]*?\bsrc=["\']https://arxiv\.org/static/[^"\']*["\'][^>]*>',
                        "",
                        _absolutize_img_srcs(page_html, html_url + "/"),
                    ),
                )
            )
            enriched += 1
        else:
            out.append(item)
    return out


def fetch_arxiv(src: IngestSource) -> FetchOutcome:
    """Fetch recent arXiv papers matching the source URL as a search query.

    URL semantics:
      - ``cat:cs.LG``         → category
      - ``au:Cochrane``       → author
      - ``ti:funding+rate``   → title contains
      - any string            → free-text query
      - or a literal HTTP atom URL → parsed verbatim

    Honours ``ARXIV_MAX_RESULTS`` (default 10). Never raises.
    """
    try:
        import arxiv  # type: ignore
    except ImportError:
        return FetchOutcome(error="arxiv pip package not installed")

    query = (src.url or "").strip()
    if not query:
        return FetchOutcome(error="arxiv source has empty query")
    if query.startswith("http://") or query.startswith("https://"):
        # User pasted a full arXiv atom URL — let feedparser handle it.
        return _parse_feed(query)

    max_results = env_int("ARXIV_MAX_RESULTS", 10, minimum=1, maximum=50)
    try:
        search = arxiv.Search(
            query=query,
            max_results=max_results,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        client = arxiv.Client(page_size=max_results, delay_seconds=1.0, num_retries=2)
        # R5/SRE-006: the arxiv lib uses urllib internally and accepts no timeout;
        # bound the blocking results() call with a process-wide socket timeout so a
        # stalled arXiv connection cannot hang the scheduler worker thread forever.
        _old_timeout = socket.getdefaulttimeout()
        try:
            socket.setdefaulttimeout(30)
            results = list(client.results(search))
        finally:
            socket.setdefaulttimeout(_old_timeout)
    except Exception as exc:  # noqa: BLE001
        return FetchOutcome(error=f"arxiv: {type(exc).__name__}: {exc}")

    items: List[FetchedItem] = []
    for r in results:
        title = (r.title or "").strip().replace("\n", " ")
        summary = (r.summary or "").strip().replace("\n", " ")
        url = str(r.entry_id) or ""
        published_at = r.published if isinstance(r.published, datetime) else None
        items.append(
            FetchedItem(
                title=title[:512] or "(untitled paper)",
                url=url,
                body_markdown=summary,
                published_at=published_at,
                raw_html="",
            )
        )

    # ── Optional full-text enrichment (same switch as the RSS path) ─────────
    if items and env_bool("ARTICLE_ENRICH_ENABLED", False):
        try:
            items = _enrich_arxiv_fulltext(items)
        except Exception:  # noqa: BLE001
            logger.debug("arxiv fulltext enrichment raised unexpectedly", exc_info=True)

    return FetchOutcome(items=items)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def fetch_glassnode(src: IngestSource) -> FetchOutcome:
    """P12-B-H1 — Glassnode Insights RSS/feed fetcher.

    Glassnode publishes its research notes via a standard RSS feed
    (research.glassnode.com/rss — formerly insights.glassnode.com, whose
    Cloudflare config now 403s non-browser clients). Routing it through
    `_parse_feed` keeps
    the same dedup + cadence machinery as other RSS-shaped sources while
    giving Glassnode its own first-class registry entry so the UI / KPI
    tiles / type-filter dropdown surface it correctly.
    """
    return _parse_feed(src.url)


FETCHERS: Dict[str, Callable[[IngestSource], FetchOutcome]] = {
    "rss": fetch_rss,
    "patreon": fetch_patreon,
    "medium": fetch_medium,
    "substack": fetch_substack,
    "reddit": fetch_reddit,
    "twitter_tag": fetch_twitter_tag,
    "twitter_article": fetch_twitter_article,
    "youtube_video": fetch_youtube,
    "tiktok": fetch_tiktok,
    "arxiv": fetch_arxiv,
    "glassnode": fetch_glassnode,
    "manual": fetch_manual,
}


def fetch_for(src: IngestSource) -> FetchOutcome:
    """Dispatch by source_type with a sane unknown-type fallback."""
    fn = FETCHERS.get(src.source_type)
    if fn is None:
        return FetchOutcome(error=f"unknown source_type '{src.source_type}'")
    try:
        return fn(src)
    except Exception as exc:  # noqa: BLE001
        logger.exception("fetcher crashed for source %s", src.id)
        return FetchOutcome(error=f"unhandled: {type(exc).__name__}: {exc}")


# KB-space taxonomy keys (must match KNOWLEDGE_CATEGORIES in backend/app/main.py;
# string literals kept here to avoid a circular import). Auto-ingested nodes
# used to land in "unclassified" wholesale — map each source's domain to its
# natural shelf instead. Matched against the SOURCE feed URL (stable), never
# the per-item link, so aggregators that link out (e.g. Quantocracy) still
# classify by the feed they came through.
_KB_CATEGORY_PATTERNS: tuple = (
    # Academic / working papers
    (re.compile(r"arxiv\.org|\bbis\.org", re.I), "papers"),
    # Strategy-idea blogs
    (
        re.compile(
            r"quantpedia\.|alphaarchitect\.|robotwealth\.|quantocracy\.|"
            r"quantgalore\.|thinknewfound\.|allocatesmartly\.",
            re.I,
        ),
        "alpha-ideas",
    ),
    # Methods / statistics / ML technique blogs
    (
        re.compile(
            r"hudsonthames\.|quantstart\.|eranraviv\.|artursepp\.|"
            r"thegradient\.|bair\.berkeley\.|towardsdatascience\.",
            re.I,
        ),
        "analysis-methods",
    ),
    # Portfolio construction / allocation
    (re.compile(r"portfoliooptimizer\.|elmwealth\.", re.I), "portfolio-management"),
    # Trading psychology / decision frameworks
    (re.compile(r"moontower", re.I), "mental-models"),
    # On-chain & market data vendors (metric-centric research)
    (
        re.compile(
            r"glassnode\.|coinmetrics\.|cryptoquant|santiment|ecoinometrics\.|"
            r"checkonchain\.|intotheblock|amberdata\.",
            re.I,
        ),
        "factor-data",
    ),
    # Derivatives / microstructure desks
    (re.compile(r"deribit\.|blockscholes|paradigm\.co", re.I), "market-structure"),
    # Macro commentary, thematic research, community discussion
    (
        re.compile(
            r"multicoin\.|ark-invest\.|cryptohayes\.|coinshares\.|balaena-quant|"
            r"libertystreeteconomics\.|fredblog\.|reddit\.com",
            re.I,
        ),
        "contextual",
    ),
)


def _guess_kb_category(source_url: str, source_type: str) -> Optional[str]:
    """Best-effort KB-space category for an ingested item.

    Returns None (→ "unclassified" in the UI) when nothing matches; never
    raises. source_type wins over URL patterns where it is unambiguous.
    """
    if source_type == "arxiv":
        return "papers"
    if source_type == "reddit":
        return "contextual"
    for pat, cat in _KB_CATEGORY_PATTERNS:
        if pat.search(source_url or ""):
            return cat
    return None


def _content_hash_for_item(item: FetchedItem) -> str:
    """Dedup hash for a fetched item — the single source of truth shared by
    ``items_to_knowledge_nodes`` (persistence) and the scheduler's pre-download
    dup check. Uses the stable ``dedup_body`` when an enricher set one (Reddit
    comments), otherwise the body, so volatile appended content does not mint a
    fresh node every poll. See ``FetchedItem.dedup_body``.
    """
    body = (item.body_markdown or "").strip()
    hash_src = item.dedup_body.strip() if item.dedup_body is not None else body
    return _content_hash(item.title, item.url[:1024] if item.url else "", hash_src)


def items_to_knowledge_nodes(
    items: List[FetchedItem],
    source: IngestSource,
) -> List[Dict[str, Any]]:
    """Convert FetchedItems → KnowledgeNode-ready dicts (kind=concept by default).

    Caller is responsible for de-duplication via content_hash + persistence.
    """
    out: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for item in items:
        body = item.body_markdown.strip()
        h = _content_hash_for_item(item)
        out.append(
            {
                "title": item.title[:512],
                "content": body[:KB_CONTENT_MAX_CHARS],
                "tags": source.source_type,
                "kind": "concept",
                "source_url": item.url[:1024] if item.url else None,
                "source_type": source.source_type,
                "category": _guess_kb_category(source.url or "", source.source_type),
                "content_hash": h,
                "ingested_at": now,
                # P6 — kept for the asset cache so the scheduler can extract
                # inline <img> URLs and rewrite the node body to point at the
                # local /api/assets/{hash} endpoint. Never serialized to DB.
                "raw_html": item.raw_html or "",
            }
        )
    return out


__all__ = [
    "FetchedItem",
    "FetchOutcome",
    "FETCHERS",
    "STUB_SOURCE_TYPES",
    "fetch_for",
    "fetch_url_text",
    "is_stub_source_type",
    "items_to_knowledge_nodes",
]
