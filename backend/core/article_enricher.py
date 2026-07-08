"""Full-text article enrichment for RSS feed items (P6-D05).

For many RSS sources the feed body is a teaser / truncated preview.
This module optionally fetches the real article page and extracts clean
body text using trafilatura, replacing the RSS preview in FetchedItem.body_markdown.

Disabled by default (ARTICLE_ENRICH_ENABLED=0 / False).

Bypass strategy cascade (keep-the-longest result wins):
  1. Direct fetch with domain-appropriate headers:
       - Googlebot UA  for sites that serve full text to search crawlers
         (wsj.com, ft.com, barrons.com, thetimes.co.uk, foreignpolicy.com)
       - Google Referer for metered paywalls that grant free access to
         search-referred visitors (bloomberg.com, nytimes.com, etc.)
       - Plain browser UA for everything else (including Medium, which is
         already cookie-free on our server-side fetcher)
  2. Full-text PDF (paper repositories): if the fetched page links a PDF whose
     stem matches the page (BIS work1353.htm → work1353.pdf) or an anchor says
     "full text"/"download", fetch + extract it with pypdf. Paper pages (e.g.
     bis.org) carry only a summary in HTML; the real paper is the PDF.
  3. Domain-specific proxy if still thin:
       - Medium / Medium custom domains → freedium.cfd
       - All other sites              → 12ft.io/proxy
  4. Return original item unchanged if nothing beats the feed body.

Technique provenance (analysis of BPC sites.js):
  Medium:   allow_cookies + remove_cookies — metered, cookie-based.
            Server-side we never send cookies, so the meter is already
            reset per request.  Hard member-only wall is NOT bypassable
            without credentials; freedium.cfd serves a cached mirror.
  WSJ/FT:   useragent=googlebot — these sites respect Google for SEO.
  Bloomberg: no Googlebot rule; Google Referer clears the metered counter.
  Seeking Alpha / Economist / Forbes / Fortune: block_regex (JS blocking) —
            browser-only technique; NOT implementable server-side.

All HTTP calls route through backend.core.ingest_fetchers._safe_get to
preserve SSRF protection.  Lazy import avoids circular dependency at
module-load time.

Usage note
----------
Bypassing publisher paywalls may violate individual sites' Terms of Service.
Enable this feature only for content you are entitled to access.

Environment variables
---------------------
ARTICLE_ENRICH_ENABLED       bool   default 0      master switch
ARTICLE_ENRICH_MIN_BODY      int    default 500     floor: a fetched body must
                                                    reach N chars to replace the
                                                    feed body (failed-extraction
                                                    guard)
ARTICLE_ENRICH_SKIP_ABOVE    int    default 3000    skip the fetch only when the
                                                    feed body is already >= N
                                                    chars AND not teaser-shaped
ARTICLE_ENRICH_MAX_ITEMS     int    default 5       max items enriched per poll
ARTICLE_ENRICH_TIMEOUT       int    default 15      per-request timeout (s)
ARTICLE_ENRICH_PDF           bool   default 1       follow a paper page's full-
                                                    text PDF link + extract it
ARTICLE_ENRICH_PDF_MAX_PAGES int    default 80      max PDF pages to extract
ARTICLE_ENRICH_FREEDIUM_URL  str    default https://freedium.cfd
ARTICLE_ENRICH_12FT_URL      str    default https://12ft.io

Why not "skip if body >= MIN_BODY": a feed teaser can be ANY length — QuantStart
ships ~506-char excerpts ending in '[...]', newsletters ship 1400-char blurbs
ending in 'Read more'. Gating on a small length silently leaves ~70% of items as
teasers (and with no article images, since enrichment is also what captures the
full page's <img> tags). We instead attempt whenever the body is short OR its
tail looks truncated/gated, and only ever REPLACE with a strictly longer body.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse, quote_plus

from backend._envloader import env_bool, env_int, env_str
from backend.core.database import KB_CONTENT_MAX_CHARS

logger = logging.getLogger("alpha.article_enricher")

# ── Googlebot UA (verbatim from BPC background.js) ──────────────────────────
_GOOGLEBOT_UA = (
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
)
# ── Generic modern browser UA (used for proxied requests) ───────────────────
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_ACCEPT_HTML = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
)

# ── Per-domain bypass rules ─────────────────────────────────────────────────
# Keys are the last-two-label root domain (e.g. "medium.com").
# "ua":      "googlebot" | "browser"  (default "browser")
# "referer": "google" | "facebook"    (default None — no Referer header)
# "proxy":   "freedium" | "12ft"      (default None — use generic "12ft" fallback)
#
# Googlebot UA is only set where BPC sites.js explicitly has useragent=googlebot.
# Google Referer is set where metered paywalls are known to grant search traffic.
_DOMAIN_RULES: Dict[str, Dict[str, str]] = {
    # ── Medium and its first-party custom publication domains ────────────────
    # BPC: allow_cookies + remove_cookies (cookie-based meter).
    # For hard member-only articles the RSS preview is the only free content;
    # freedium.cfd provides a community mirror of Medium content.
    "medium.com":             {"proxy": "freedium"},
    "betterprogramming.pub":  {"proxy": "freedium"},
    "towardsdatascience.com": {"proxy": "freedium"},
    "levelup.gitconnected.com": {"proxy": "freedium"},
    "uxdesign.cc":            {"proxy": "freedium"},
    "bootcamp.uxdesign.cc":   {"proxy": "freedium"},
    "itnext.io":              {"proxy": "freedium"},
    "plainenglish.io":        {"proxy": "freedium"},
    # ── Sites that serve full content to Googlebot (BPC: useragent=googlebot) ─
    "wsj.com":           {"ua": "googlebot"},
    "ft.com":            {"ua": "googlebot"},
    "barrons.com":       {"ua": "googlebot"},
    "thetimes.co.uk":    {"ua": "googlebot"},
    "thetimes.com":      {"ua": "googlebot"},
    "foreignpolicy.com": {"ua": "googlebot"},
    # ── Metered paywalls that lift restrictions for Google-referred traffic ───
    "bloomberg.com":        {"referer": "google"},
    "washingtonpost.com":   {"referer": "google"},
    "nytimes.com":          {"referer": "google"},
    "theatlantic.com":      {"referer": "google"},
    "wired.com":            {"referer": "google"},
    "technologyreview.com": {"referer": "google"},
    "hbr.org":              {"referer": "google"},
    "businessinsider.com":  {"referer": "google"},
    "foreignaffairs.com":   {"referer": "google"},
    "nationalgeographic.com": {"referer": "google"},
    "scientificamerican.com": {"referer": "google"},
}


# ── Env helpers ──────────────────────────────────────────────────────────────

def is_enrich_enabled() -> bool:
    return env_bool("ARTICLE_ENRICH_ENABLED", False)


def _min_body() -> int:
    # Floor a fetched body must clear to REPLACE the feed body — guards against
    # swapping a teaser for an even-shorter failed extraction (nav/footer scraps).
    return env_int("ARTICLE_ENRICH_MIN_BODY", 500, minimum=50, maximum=10_000)


def _skip_above() -> int:
    # Skip the HTTP fetch only when the feed body is already this long AND does
    # not look teaser-shaped — i.e. the feed almost certainly shipped full
    # content:encoded text (Ghost/WordPress). Below this, attempt enrichment.
    return env_int("ARTICLE_ENRICH_SKIP_ABOVE", 3000, minimum=500, maximum=100_000)


# Tail markers that betray a truncated / paywall-gated feed body. Checked
# case-insensitively against the LAST stretch of the body (where these CTAs and
# truncation markers live), so an article that merely mentions "subscribe"
# mid-paragraph is not flagged.
_TEASER_MARKERS: tuple = (
    "[...]", "[…]", "(more…)", "(more...)",
    "read more", "continue reading", "read the full", "read in app",
    "appeared first on", "to access", "to read the rest", "read the rest",
    "subscribe", "subscriber", "upgrade to", "unlock", "premium",
    "learn more about", "click here",
)


def _looks_like_teaser(body: str) -> bool:
    """Heuristic: does this feed body look truncated/paywall-gated, not full?

    Length alone is a poor signal — a 1400-char "Read more" newsletter blurb is
    still a teaser. We scan only the TAIL so a normal article that mentions
    "subscribe" mid-body is not flagged. Used purely to DECIDE whether to attempt
    a fetch; the keep-the-longer rule in ``_enrich_one`` guarantees a false
    positive can never shorten the stored body.
    """
    if not body:
        return False
    tail = body[-180:].lower()
    return any(m in tail for m in _TEASER_MARKERS)


def _max_items() -> int:
    return env_int("ARTICLE_ENRICH_MAX_ITEMS", 5, minimum=1, maximum=20)


def _timeout_s() -> int:
    return env_int("ARTICLE_ENRICH_TIMEOUT", 15, minimum=5, maximum=60)


def _freedium_base() -> str:
    return env_str("ARTICLE_ENRICH_FREEDIUM_URL", "https://freedium.cfd").rstrip("/")


def _12ft_base() -> str:
    return env_str("ARTICLE_ENRICH_12FT_URL", "https://12ft.io").rstrip("/")


def _pdf_enabled() -> bool:
    # Follow a paper page's "full text PDF" link and extract its text. Default ON
    # (within the ARTICLE_ENRICH_ENABLED master switch); needs pypdf installed.
    return env_bool("ARTICLE_ENRICH_PDF", True)


def _pdf_max_pages() -> int:
    # Bound extraction work on very long PDFs (the result is still capped at
    # KB_CONTENT_MAX_CHARS). A typical working paper is ~30 pages.
    return env_int("ARTICLE_ENRICH_PDF_MAX_PAGES", 80, minimum=1, maximum=500)


# Paper repositories whose article page is only a SUMMARY/abstract and links the
# real paper as a separate PDF. These are always attempted (regardless of the
# summary's length) so the PDF-following step below can reach the full text.
# arXiv is intentionally absent — it has a dedicated HTML full-text path in
# ingest_fetchers (arxiv.org/html), no PDF parsing needed.
_PAPER_DOMAINS: tuple = ("bis.org",)


def _is_paper_url(url: str) -> bool:
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return any(host == d or host.endswith("." + d) for d in _PAPER_DOMAINS)


# ── Domain helpers ───────────────────────────────────────────────────────────

# Second-level labels used under country-code TLDs (e.g. .co.uk, .org.au).
# When the second-to-last label is one of these AND the TLD is a two-letter
# country code, take three labels so that 'www.thetimes.co.uk' → 'thetimes.co.uk'.
_CCTLD_SLDS: frozenset = frozenset(
    {"co", "org", "gov", "net", "ac", "com", "edu", "mil", "ltd", "plc"}
)


def _root_domain(url: str) -> str:
    """Return the registrable root domain from a URL.

    Handles plain two-label domains ('medium.com') and ccTLD second-level
    domains ('thetimes.co.uk') by checking whether the second-to-last label
    is a known SLD abbreviation under a two-letter country-code TLD.
    """
    try:
        host = (urlparse(url).hostname or "").lower().strip()
        parts = [p for p in host.split(".") if p]
        if len(parts) >= 3 and parts[-2] in _CCTLD_SLDS and len(parts[-1]) == 2:
            # e.g. ['www', 'thetimes', 'co', 'uk'] → 'thetimes.co.uk'
            return ".".join(parts[-3:])
        return ".".join(parts[-2:]) if len(parts) >= 2 else host
    except Exception:  # noqa: BLE001
        return ""


def _rule_for(url: str) -> Dict[str, str]:
    return _DOMAIN_RULES.get(_root_domain(url), {})


def _direct_headers(rule: Dict[str, str]) -> Dict[str, str]:
    """Build request headers for the direct-fetch attempt."""
    ua = _GOOGLEBOT_UA if rule.get("ua") == "googlebot" else _BROWSER_UA
    hdrs: Dict[str, str] = {
        "User-Agent": ua,
        "Accept": _ACCEPT_HTML,
        "Accept-Language": "en-US,en;q=0.9",
    }
    referer = rule.get("referer")
    if referer == "google":
        hdrs["Referer"] = "https://www.google.com/"
    elif referer == "facebook":
        hdrs["Referer"] = "https://www.facebook.com/"
    return hdrs


def _proxy_url(original_url: str, proxy_type: str) -> Optional[str]:
    """Build the full proxy URL for the given proxy type."""
    if proxy_type == "freedium":
        # Format: https://freedium.cfd/<original_url>   (URL not re-encoded)
        return f"{_freedium_base()}/{original_url}"
    if proxy_type == "12ft":
        # Format: https://12ft.io/proxy?q=<url-encoded original>
        return f"{_12ft_base()}/proxy?q={quote_plus(original_url)}"
    return None


# ── Text extraction ──────────────────────────────────────────────────────────

def _extract_text(html_bytes: bytes) -> str:
    """Extract clean article body text from HTML bytes via trafilatura.

    Returns empty string if trafilatura is missing or extraction fails.
    trafilatura is lightweight (~15 MB, no torch) but is a separate install:
        pip install trafilatura>=1.9.0
    """
    try:
        import trafilatura  # type: ignore
    except ImportError:
        logger.warning(
            "article_enricher: trafilatura not installed — "
            "install with: pip install trafilatura>=1.9.0"
        )
        return ""
    try:
        text: Optional[str] = trafilatura.extract(
            html_bytes,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
            favor_recall=True,
            output_format="txt",
        )
        from backend.core.ingest_fetchers import _strip_boilerplate_tail  # noqa: PLC0415

        return _strip_boilerplate_tail((text or "").strip())
    except Exception:  # noqa: BLE001
        logger.debug("article_enricher: trafilatura extraction failed", exc_info=True)
        return ""


# ── PDF full-text extraction (paper repositories) ─────────────────────────────

import re as _re  # noqa: E402 — module-level, kept local to this section
import unicodedata as _ud  # noqa: E402 — used by _normalize_pdf_math below

# Anchor tags whose href ends in .pdf, with the link text captured so we can
# prefer "full text"/"download" links over incidental PDF references.
_PDF_HREF_RE = _re.compile(
    r'<a\b[^>]*?href=["\']([^"\']+?\.pdf(?:[?#][^"\']*)?)["\'][^>]*>(.*?)</a>',
    _re.IGNORECASE | _re.DOTALL,
)
# STRONG anchor hints only — a bare "PDF"/"download" link is too loose (it
# matches footer "Download our brochure (PDF)" links). Must clearly denote the
# article's own full text.
_PDF_ANCHOR_HINTS = (
    "full text", "full-text", "fulltext", "full paper", "full pdf",
    "download paper", "download the paper", "download full", "read the full paper",
)
# Boilerplate PDFs that are never the article body — excluded by url or anchor.
_PDF_EXCLUDE_HINTS = (
    "privacy", "gdpr", "terms", "cookie", "consent", "policy", "policies",
    "disclaimer", "disclosure", "brochure", "factsheet", "fact-sheet",
    "prospectus", "newsletter-signup", "subscribe",
)


# ── PDF math-glyph repair ─────────────────────────────────────────────────────
#
# When a PDF embeds its formulas in a math font (the usual LaTeX/Type1 case),
# pypdf maps each glyph onto a *styled* Unicode codepoint instead of plain
# ASCII: the variable ``K`` arrives as ``𝐾`` (U+1D43E MATHEMATICAL ITALIC
# CAPITAL K), ``α`` as ``𝛼``, the italic ``h`` as ``ℎ`` (U+210E PLANCK
# CONSTANT). The reader sees mojibake like ``𝐾𝐾 clusters`` and ``𝛾𝐹𝐹𝑅𝑖``.
# Two artefacts compound this:
#   1. Styling — every math letter is a Mathematical-Alphanumeric / Letterlike
#      codepoint. NFKC folds these straight back to ASCII / Greek.
#   2. Doubling — some PDFs draw each formula glyph twice (a poor-man's bold),
#      so the run arrives as ``𝐾𝐾``. This is collapsed, but ONLY when a maximal
#      styled run splits cleanly into identical consecutive pairs, so genuine
#      sequences such as ``𝐹𝐹𝑅`` ("FFR") or ``𝑒𝑟𝑟𝑜𝑟`` ("error") — which are
#      not all-pairs — are left untouched.
# Glyphs the PDF font never mapped (e.g. ``∑`` with no ToUnicode entry) arrive
# as U+FFFD; they are left in place rather than guessed. Only the styled runs
# are rewritten — dollar amounts, superscripts and ordinary prose are untouched.
_MATH_RUN_RE = _re.compile(
    "[\U0001d400-\U0001d7ff"   # Mathematical Alphanumeric Symbols (𝐀–𝟿)
    "ℎ"                    # ℎ PLANCK CONSTANT (italic h)
    "ℂℊ-ℓ"       # ℂ ℊℋℌℍℎℏℐℑℒℓ
    "ℕℙ-ℝ"       # ℕ ℙℚℛℜℝ
    "ℤℨℬ-ℴ" # ℤ ℨ ℬℭ℮ℯℰℱℲℳℴ
    "]+"
)


def _is_doubled_run(run: str) -> bool:
    """True when every glyph in a styled run is immediately repeated (𝐾𝐾)."""
    if len(run) < 2 or len(run) % 2:
        return False
    return all(run[i] == run[i + 1] for i in range(0, len(run), 2))


def _normalize_pdf_math(text: str) -> str:
    """Fold pypdf's styled math glyphs back to ASCII/Greek and undo doubling.

    Surgical: only maximal runs of Mathematical-Alphanumeric / Letterlike math
    codepoints are rewritten (de-doubled when all-pairs, then NFKC-folded).
    Every other character — ``$`` amounts, ``²`` superscripts, U+FFFD, plain
    prose — is left exactly as-is. Idempotent (folded text contains no styled
    runs, so a second pass is a no-op), which makes it safe to re-apply to
    already-stored content during a backfill.
    """
    if not text:
        return text

    def _fix(m: "_re.Match[str]") -> str:
        run = m.group(0)
        if _is_doubled_run(run):
            run = run[::2]
        return _ud.normalize("NFKC", run)

    return _MATH_RUN_RE.sub(_fix, text)


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from a (text-based) PDF via pypdf. "" on any failure.

    pypdf is pure-Python and lazy-imported so the backend boots without it when
    ARTICLE_ENRICH_PDF is off. Scanned/image-only PDFs yield no text (no OCR) →
    "" → the caller keeps the HTML summary. Whitespace is normalised; the result
    is bounded by KB_CONTENT_MAX_CHARS at the call site (``_replace_body``).
    """
    if not pdf_bytes or pdf_bytes[:5] != b"%PDF-":
        return ""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        logger.warning(
            "article_enricher: pypdf not installed — pip install 'pypdf>=4.0,<6'"
        )
        return ""
    import io

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")  # owner-locked PDFs often open with empty pw
            except Exception:  # noqa: BLE001
                return ""
        parts: List[str] = []
        total = 0
        limit = KB_CONTENT_MAX_CHARS + 20_000  # extract a little past the cap
        for page in reader.pages[: _pdf_max_pages()]:
            try:
                t = page.extract_text() or ""
            except Exception:  # noqa: BLE001
                t = ""
            if t:
                parts.append(t)
                total += len(t)
                if total > limit:
                    break
        text = "\n\n".join(parts)
        text = _normalize_pdf_math(text)  # styled math glyphs → ASCII/Greek
        text = _re.sub(r"[ \t]+", " ", text)
        text = _re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    except Exception:  # noqa: BLE001
        logger.debug("article_enricher: PDF extraction failed", exc_info=True)
        return ""


def _find_fulltext_pdf_url(page_url: str, html: str) -> Optional[str]:
    """Return the absolute URL of the page's full-text PDF, or None.

    Strategy (conservative — a wrong PDF would REPLACE the article body): return,
    in order, (1) a PDF whose path stem matches the page's (``work1353.htm`` →
    ``work1353.pdf`` — the BIS pattern), or (2) a link whose anchor text strongly
    denotes the article's own full text ("full text"/"full paper"/…). Footer/nav
    PDFs (privacy, GDPR, terms, brochure, …) are excluded, and there is NO
    "first PDF on the page" fallback — that grabbed unrelated PrivacyPolicy.pdf
    links. Returns None when nothing clearly qualifies.
    """
    if not page_url or not html:
        return None
    base_noext = page_url.split("?", 1)[0].split("#", 1)[0]
    low = base_noext.lower()
    stem_pdf = None
    for ext in (".htm", ".html"):
        if low.endswith(ext):
            stem_pdf = base_noext[: -len(ext)] + ".pdf"
            break

    candidates: List[tuple] = []  # (abs_url, anchor_text_lower)
    for href, anchor in _PDF_HREF_RE.findall(html):
        try:
            abs_url = urljoin(page_url, href.strip())
        except Exception:  # noqa: BLE001
            continue
        if not abs_url.lower().startswith(("http://", "https://")):
            continue
        atext = _re.sub(r"<[^>]+>", " ", anchor or "").strip().lower()
        candidates.append((abs_url, atext))
    if not candidates:
        return None

    def _excluded(abs_url: str, atext: str) -> bool:
        blob = (abs_url.lower() + " " + atext)
        return any(x in blob for x in _PDF_EXCLUDE_HINTS)

    # 1. Stem match — strongest signal (the page's own paper).
    if stem_pdf:
        for abs_url, atext in candidates:
            if abs_url.split("?", 1)[0].lower() == stem_pdf.lower() and not _excluded(abs_url, atext):
                return abs_url
    # 2. Strong "full text" anchor.
    for abs_url, atext in candidates:
        if not _excluded(abs_url, atext) and any(h in atext for h in _PDF_ANCHOR_HINTS):
            return abs_url
    # No loose fallback: an unrecognised PDF is NOT assumed to be the full text.
    return None


# ── Per-item enrichment ──────────────────────────────────────────────────────

def _enrich_one(item) -> object:
    """Enrich a single FetchedItem to the FULLEST available body.

    Fetches the article page (direct, then a paywall proxy if direct didn't beat
    the feed body), extracts clean text, and returns a copy whose body_markdown
    is whichever of {feed body, direct, proxy} is LONGEST — but only when the
    winner is fetched full text that both clears the ``_min_body`` floor AND
    exceeds the feed body. This "keep the longer" rule means enrichment can only
    ADD content (and the article page's images, via raw_html), never truncate a
    body that was already richer than the page. Returns the original on failure.
    """
    url = (item.url or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return item

    # Lazy import to break circular dependency with ingest_fetchers.
    try:
        import httpx
        from backend.core.ingest_fetchers import (  # noqa: PLC0415
            _absolutize_img_srcs,
            _assert_public_host,
            _safe_get,
        )
    except ImportError as exc:
        logger.debug("article_enricher: import failed: %s", exc)
        return item

    timeout = httpx.Timeout(float(_timeout_s()), connect=8.0)
    rule = _rule_for(url)
    original_body = item.body_markdown or ""
    floor = _min_body()

    # Best fetched candidate so far (text + the page HTML its images come from).
    best_text = ""
    best_html = ""
    direct_html_raw = ""   # raw direct page, for full-text-PDF link detection
    direct_html_abs = ""   # absolutized direct page, reused if a PDF wins

    # ── Step 1: direct fetch ─────────────────────────────────────────────────
    try:
        resp = _safe_get(url, headers=_direct_headers(rule), timeout=timeout)
        if resp.status_code < 400 and resp.content:
            direct_html_raw = resp.text
            # Keep the article page itself (img srcs absolutized) so the
            # scheduler's asset-cache pass sees the article's images, not just
            # whatever thumbnails the feed summary happened to embed.
            direct_html_abs = _absolutize_img_srcs(resp.text, url)
            direct_text = _extract_text(resp.content)
            if len(direct_text) > len(best_text):
                best_text = direct_text
                best_html = direct_html_abs
    except Exception:  # noqa: BLE001
        logger.debug(
            "article_enricher: direct fetch failed for %s", url, exc_info=True
        )

    # ── Step 2: full-text PDF — paper pages are a summary + a separate PDF ────
    # Tried BEFORE the proxy: for papers the PDF *is* the full text, so this both
    # completes them and skips a pointless paywall-proxy round-trip. Self-scoping:
    # _find_fulltext_pdf_url returns None unless the page actually links a PDF, so
    # ordinary article pages never trigger a speculative download.
    if _pdf_enabled() and direct_html_raw:
        pdf_url = _find_fulltext_pdf_url(url, direct_html_raw)
        if pdf_url:
            try:
                _assert_public_host(pdf_url)  # SSRF re-check before the fetch
                presp = _safe_get(
                    pdf_url,
                    headers={"User-Agent": _BROWSER_UA, "Accept": "application/pdf,*/*"},
                    timeout=timeout,
                )
                if presp.status_code < 400 and presp.content:
                    pdf_text = _extract_pdf_text(presp.content)
                    if len(pdf_text) > len(best_text):
                        best_text = pdf_text
                        # The PDF has no inline <img> for the asset cache; reuse
                        # the article page's HTML so any figures shown there are
                        # still captured.
                        best_html = direct_html_abs or best_html
            except ValueError:
                logger.info("article_enricher: skipping non-public PDF url %s", pdf_url)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "article_enricher: PDF fetch/extract failed for %s",
                    pdf_url, exc_info=True,
                )

    # ── Step 3: proxy fallback — only when nothing yet beats the feed body ────
    if len(best_text) < max(floor, len(original_body) + 1):
        # Guard: re-validate original URL before forwarding it to a third-party
        # proxy. The direct-fetch path already ran _assert_public_host via
        # _safe_get; repeating it here prevents indirect SSRF where a malicious
        # RSS <link> pointing at an internal address passes the outer proxy check
        # but leaks the internal URL to 12ft.io / freedium.cfd.
        try:
            _assert_public_host(url)
        except ValueError:
            logger.info(
                "article_enricher: skipping proxy step for non-public URL %s", url
            )
        else:
            # Domain-specific proxy if configured, otherwise 12ft.io.
            proxy_type = rule.get("proxy") or "12ft"
            proxy_url_str = _proxy_url(url, proxy_type)
            if proxy_url_str:
                try:
                    proxy_hdrs = {
                        "User-Agent": _BROWSER_UA,
                        "Accept": _ACCEPT_HTML,
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": "https://www.google.com/",
                    }
                    resp = _safe_get(proxy_url_str, headers=proxy_hdrs, timeout=timeout)
                    if resp.status_code < 400 and resp.content:
                        proxy_text = _extract_text(resp.content)
                        if len(proxy_text) > len(best_text):
                            best_text = proxy_text
                            best_html = _absolutize_img_srcs(resp.text, proxy_url_str)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "article_enricher: %s proxy failed for %s",
                        proxy_type, url, exc_info=True,
                    )

    # ── Decide: replace only with a real body that is strictly longer ────────
    if len(best_text) >= floor and len(best_text) > len(original_body):
        logger.info(
            "article_enricher: enriched %s (%d → %d chars)",
            url, len(original_body), len(best_text),
        )
        return _replace_body(item, best_text, raw_html=best_html)

    logger.debug(
        "article_enricher: kept original for %s (feed=%d, best fetched=%d)",
        url, len(original_body), len(best_text),
    )
    return item


def _replace_body(item, text: str, raw_html: Optional[str] = None):
    """Return a new FetchedItem with body_markdown replaced by full text.

    ``raw_html``, when non-empty, replaces the item's raw_html too — it should
    be the fetched article page (img srcs absolutized) so the downstream
    asset-cache pass extracts the article's images rather than only the feed
    summary's. Falls back to the original raw_html otherwise.
    """
    # Import here to avoid circular dependency at module load time.
    from backend.core.ingest_fetchers import FetchedItem  # noqa: PLC0415

    return FetchedItem(
        title=item.title,
        url=item.url,
        # Cap to the shared KnowledgeNode.content limit.
        body_markdown=text[:KB_CONTENT_MAX_CHARS],
        published_at=item.published_at,
        raw_html=raw_html if raw_html else item.raw_html,
    )


# ── Public batch API ─────────────────────────────────────────────────────────

def enrich_items(items: List) -> List:
    """Enrich up to ARTICLE_ENRICH_MAX_ITEMS items that look truncated/gated.

    An item is ATTEMPTED when its feed body is shorter than
    ``ARTICLE_ENRICH_SKIP_ABOVE`` *or* its tail still looks like a teaser
    (``_looks_like_teaser`` — '[...]', 'Read more', 'subscriber', …). An item
    that already carries a long, un-gated body is passed through with no HTTP
    request. The per-poll attempt count is capped by ``ARTICLE_ENRICH_MAX_ITEMS``
    so one poll can't make dozens of extra calls; items beyond the cap pass
    through unchanged.

    Returns the full list (length unchanged) — enriched items have updated
    body_markdown (and raw_html, for the asset-cache image pass); the rest are
    the original objects.

    Never raises.
    """
    if not items:
        return items

    skip_above = _skip_above()
    max_items = _max_items()
    enriched_count = 0
    result = []

    for item in items:
        body = item.body_markdown or ""
        # Attempt when short, teaser-shaped, OR a known paper repo (whose page is
        # just a summary linking the full PDF — its summary may exceed skip_above
        # yet still be incomplete).
        needs_enrich = (
            len(body) < skip_above
            or _looks_like_teaser(body)
            or _is_paper_url(item.url or "")
        )
        if enriched_count < max_items and needs_enrich:
            try:
                item = _enrich_one(item)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "article_enricher: _enrich_one crashed for url=%s", item.url
                )
            enriched_count += 1
        result.append(item)

    if enriched_count:
        logger.info(
            "article_enricher: attempted enrichment for %d/%d items",
            enriched_count, len(items),
        )
    return result


__all__ = [
    "enrich_items",
    "is_enrich_enabled",
]
