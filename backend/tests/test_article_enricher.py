"""Regression tests for article_enricher's enrich gate + keep-longer rule.

The original gate only attempted enrichment when the feed body was SHORTER than
ARTICLE_ENRICH_MIN_BODY (500). A teaser can be any length — QuantStart ships
~506-char excerpts ending in '[...]', newsletters ship ~1400-char 'Read more'
blurbs — so ~70% of items were stored as teasers with no article images. The fix
attempts whenever the body is short OR teaser-shaped, and only ever REPLACES the
body with a strictly-longer fetched one. Fully network-free (HTTP is monkeypatched).
"""

from __future__ import annotations

import httpx

import backend.core.article_enricher as ae
import backend.core.ingest_fetchers as inf
from backend.core.ingest_fetchers import FetchedItem


# --------------------------------------------------------------------------- #
# _looks_like_teaser — markers live in the TAIL, not mid-body                  #
# --------------------------------------------------------------------------- #


def test_looks_like_teaser_detects_tail_markers():
    assert ae._looks_like_teaser("intro paragraph then truncated [...]")
    assert ae._looks_like_teaser("blah blah blah Read more")
    assert ae._looks_like_teaser("preview text … upgrade to premium today")
    assert ae._looks_like_teaser("The post Foo appeared first on Bar")
    assert ae._looks_like_teaser("Subscriber-only. Subscribe to access today's update")


def test_looks_like_teaser_false_for_full_or_empty():
    assert not ae._looks_like_teaser("")
    assert not ae._looks_like_teaser(
        "A complete article body that ends on a normal declarative sentence."
    )
    # 'subscribe' only mid-body (pushed out of the 180-char tail) is NOT a teaser.
    assert not ae._looks_like_teaser("subscribe now " + "z" * 400)


# --------------------------------------------------------------------------- #
# enrich_items gate — attempt short OR teaser-shaped; skip long clean bodies   #
# --------------------------------------------------------------------------- #


def test_enrich_items_attempts_short_and_teaser_skips_long_clean(monkeypatch):
    monkeypatch.setattr(ae, "_skip_above", lambda: 3000)
    calls: list[str] = []
    monkeypatch.setattr(ae, "_enrich_one", lambda it: (calls.append(it.url) or it))

    items = [
        FetchedItem(title="a", url="u1", body_markdown="x" * 5000 + " Read more"),  # long teaser
        FetchedItem(title="b", url="u2", body_markdown="y" * 5000),                 # long clean → skip
        FetchedItem(title="c", url="u3", body_markdown="short teaser [...]"),        # short → attempt
    ]
    out = ae.enrich_items(items)
    assert calls == ["u1", "u3"]      # u2 skipped, no HTTP attempt
    assert len(out) == 3              # length never changes


def test_enrich_items_respects_max_items(monkeypatch):
    monkeypatch.setenv("ARTICLE_ENRICH_MAX_ITEMS", "2")
    calls: list[str] = []
    monkeypatch.setattr(ae, "_enrich_one", lambda it: (calls.append(it.url) or it))
    items = [FetchedItem(title=str(i), url=f"u{i}", body_markdown="t") for i in range(5)]
    ae.enrich_items(items)
    assert len(calls) == 2  # capped


# --------------------------------------------------------------------------- #
# _enrich_one — keep the LONGER body; never downgrade                          #
# --------------------------------------------------------------------------- #

_PAGE = "<html><body><p>full</p><img src='/fig.png'></body></html>"


def _resp(html=_PAGE, status=200):
    return httpx.Response(
        status, content=html.encode("utf-8"),
        request=httpx.Request("GET", "https://blog.example.com/p"),
    )


def test_enrich_one_replaces_with_longer_full_text_and_keeps_article_images(monkeypatch):
    monkeypatch.setattr(inf, "_safe_get", lambda *a, **k: _resp())
    monkeypatch.setattr(ae, "_extract_text", lambda _b: "FULL ARTICLE " + "x" * 2000)
    item = FetchedItem(
        title="t", url="https://blog.example.com/p", body_markdown="teaser [...]",
    )
    out = ae._enrich_one(item)
    assert out is not item
    assert out.body_markdown.startswith("FULL ARTICLE ")
    assert len(out.body_markdown) > 1000
    # raw_html is the article page (img src absolutized) → asset cache pulls it.
    assert "https://blog.example.com/fig.png" in out.raw_html


def test_enrich_one_keeps_original_when_fetched_is_shorter(monkeypatch):
    monkeypatch.setenv("ALPHA_ALLOW_LOCAL_INGEST", "1")  # skip DNS in proxy guard
    monkeypatch.setattr(inf, "_safe_get", lambda *a, **k: _resp())
    monkeypatch.setattr(ae, "_extract_text", lambda _b: "tiny")  # shorter than body
    item = FetchedItem(
        title="t", url="https://blog.example.com/p", body_markdown="Z" * 2000,
    )
    out = ae._enrich_one(item)
    assert out is item  # unchanged — never downgrade a richer feed body


def test_enrich_one_keeps_original_when_extraction_below_floor(monkeypatch):
    monkeypatch.setenv("ARTICLE_ENRICH_MIN_BODY", "500")
    monkeypatch.setenv("ALPHA_ALLOW_LOCAL_INGEST", "1")
    monkeypatch.setattr(inf, "_safe_get", lambda *a, **k: _resp())
    monkeypatch.setattr(ae, "_extract_text", lambda _b: "x" * 200)  # >body but < floor
    item = FetchedItem(title="t", url="https://blog.example.com/p", body_markdown="hi")
    out = ae._enrich_one(item)
    assert out is item  # 200 < 500 floor → not a trustworthy full body


def test_enrich_one_non_http_url_is_noop(monkeypatch):
    monkeypatch.setattr(
        inf, "_safe_get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch for bad url")),
    )
    item = FetchedItem(title="t", url="ftp://x/y", body_markdown="teaser [...]")
    assert ae._enrich_one(item) is item


# --------------------------------------------------------------------------- #
# Full-text PDF following (paper repositories: HTML summary + separate PDF)    #
# --------------------------------------------------------------------------- #


def test_is_paper_url():
    assert ae._is_paper_url("https://www.bis.org/publ/work1353.htm")
    assert ae._is_paper_url("https://bis.org/publ/work1.htm")
    assert not ae._is_paper_url("https://blog.example.com/p")
    assert not ae._is_paper_url("https://notbis.org.evil.com/x")  # suffix-anchored
    assert not ae._is_paper_url("")


def test_find_fulltext_pdf_url_prefers_stem_match():
    html = (
        '<a href="/publ/work1353.fr.pdf">Texte intégral</a>'
        '<a href="/publ/work1353.pdf">PDF full text (2,015kb)</a>'
    )
    out = ae._find_fulltext_pdf_url("https://www.bis.org/publ/work1353.htm", html)
    assert out == "https://www.bis.org/publ/work1353.pdf"  # stem match wins over the .fr.pdf


def test_find_fulltext_pdf_url_falls_back_to_anchor_hint():
    html = '<a href="https://cdn.x/paper-abc.pdf">Download full paper</a>'
    out = ae._find_fulltext_pdf_url("https://journal.example.com/article/abc", html)
    assert out == "https://cdn.x/paper-abc.pdf"


def test_find_fulltext_pdf_url_none_when_no_pdf_link():
    assert ae._find_fulltext_pdf_url("https://blog.example.com/p", "<p>no pdfs here</p>") is None
    assert ae._find_fulltext_pdf_url("https://x/p", "") is None


def test_find_fulltext_pdf_url_skips_footer_policy_pdfs():
    # Regression: a WordPress page (Hudson & Thames) whose only PDF links are
    # footer privacy/GDPR docs must NOT be mistaken for the article's full text.
    html = (
        '<a href="/wp-content/uploads/2021/06/PrivacyPolicy.pdf">Privacy Policy</a>'
        '<a href="/wp-content/uploads/2021/06/GDPR-Policy.pdf">GDPR</a>'
    )
    assert ae._find_fulltext_pdf_url("https://hudsonthames.org/release-x/", html) is None


def test_find_fulltext_pdf_url_no_loose_first_pdf_fallback():
    # A single unrelated PDF with a neutral anchor and no stem match → None
    # (the old "return the first PDF" fallback is gone).
    html = '<a href="https://cdn.x/some-attachment.pdf">attachment</a>'
    assert ae._find_fulltext_pdf_url("https://blog.example.com/post-42", html) is None


def test_extract_pdf_text_rejects_non_pdf_bytes():
    assert ae._extract_pdf_text(b"") == ""
    assert ae._extract_pdf_text(b"<html>not a pdf</html>") == ""  # missing %PDF- magic


def test_enrich_one_follows_pdf_link_and_keeps_longer(monkeypatch):
    monkeypatch.setenv("ALPHA_ALLOW_LOCAL_INGEST", "1")  # skip DNS in SSRF guard
    page = (
        "<html><body><p>Summary only.</p>"
        '<a href="work1353.pdf">PDF full text</a></body></html>'
    )

    def _fake_get(u, headers=None, timeout=None, **k):
        if u.endswith(".pdf"):
            return httpx.Response(200, content=b"%PDF-1.7 ...binary...",
                                  request=httpx.Request("GET", u))
        return httpx.Response(200, content=page.encode(),
                              request=httpx.Request("GET", u))

    monkeypatch.setattr(inf, "_safe_get", _fake_get)
    # HTML extraction yields the short summary; the PDF yields the full paper.
    monkeypatch.setattr(ae, "_extract_text", lambda _b: "Summary only.")
    monkeypatch.setattr(ae, "_extract_pdf_text", lambda _b: "FULL PAPER " + "y" * 5000)

    item = FetchedItem(title="t", url="https://www.bis.org/publ/work1353.htm",
                       body_markdown="Summary only.")
    out = ae._enrich_one(item)
    assert out.body_markdown.startswith("FULL PAPER ")
    assert len(out.body_markdown) > 4000
    # The article page is kept as raw_html so its figures still reach asset cache.
    assert "work1353.pdf" in out.raw_html or "bis.org" in out.raw_html


def test_enrich_items_attempts_paper_url_even_when_long(monkeypatch):
    monkeypatch.setattr(ae, "_skip_above", lambda: 3000)
    calls: list[str] = []
    monkeypatch.setattr(ae, "_enrich_one", lambda it: (calls.append(it.url) or it))
    items = [
        # 5000-char clean summary on a paper domain → must still be attempted.
        FetchedItem(title="p", url="https://www.bis.org/publ/work1.htm",
                    body_markdown="z" * 5000),
        # 5000-char clean body on a normal blog → skipped.
        FetchedItem(title="b", url="https://blog.example.com/p",
                    body_markdown="z" * 5000),
    ]
    ae.enrich_items(items)
    assert calls == ["https://www.bis.org/publ/work1.htm"]


# --------------------------------------------------------------------------- #
# _normalize_pdf_math — repair pypdf's styled math-glyph mojibake             #
# --------------------------------------------------------------------------- #


def test_is_doubled_run_only_true_for_clean_pairs():
    assert ae._is_doubled_run("KK")          # 𝐾𝐾-style
    assert ae._is_doubled_run("xxii")        # (xx)(ii)
    assert not ae._is_doubled_run("K")       # length 1
    assert not ae._is_doubled_run("FFR")     # odd length → leave (real "FFR")
    assert not ae._is_doubled_run("error")   # "e r r o r" not all-pairs
    assert not ae._is_doubled_run("xy")      # distinct pair


def test_normalize_pdf_math_folds_styled_letters_to_ascii_and_greek():
    # MATHEMATICAL ITALIC letters (U+1D400+) fold to plain ASCII / Greek.
    assert ae._normalize_pdf_math("\U0001d6fe\U0001d439\U0001d445") == "γFR"   # 𝛾𝐹𝑅
    assert ae._normalize_pdf_math("\U0001d44e") == "a"                          # 𝑎
    # Letterlike math variables: ℎ PLANCK (U+210E) → h, ℓ SCRIPT L (U+2113) → l.
    assert ae._normalize_pdf_math("ℎℓ") == "hl"


def test_normalize_pdf_math_dedoubles_only_clean_pairs():
    # Doubled run ("fake bold") is halved: 𝐾𝐾 → K, 𝑥𝑥𝑖𝑖 → xi.
    assert ae._normalize_pdf_math("\U0001d43e\U0001d43e clusters") == "K clusters"
    assert ae._normalize_pdf_math(
        "\U0001d465\U0001d465\U0001d456\U0001d456"  # 𝑥𝑥𝑖𝑖
    ) == "xi"
    # A NON-doubled run with genuine repeats must survive intact:
    #   𝐹𝐹𝑅𝑖 → "FFRi"  (the two F's are real — Federal Funds Rate)
    #   𝑒𝑟𝑟𝑜𝑟𝑖 → "errori"  (the "rr" is real)
    assert ae._normalize_pdf_math(
        "\U0001d439\U0001d439\U0001d445\U0001d456"
    ) == "FFRi"
    assert ae._normalize_pdf_math(
        "\U0001d452\U0001d45f\U0001d45f\U0001d45c\U0001d45f\U0001d456"
    ) == "errori"


def test_normalize_pdf_math_leaves_prose_dollars_and_fffd_untouched():
    # Dollar amounts must NOT be disturbed (they are not styled runs).
    s = "consumption below $50 or above $50,000 with a median of $3,000"
    assert ae._normalize_pdf_math(s) == s
    # Superscript digit and an unmapped-glyph replacement char are left as-is;
    # only the surrounding styled letters fold. 𝐽𝐽(𝐶𝐶) = � ‖𝑥𝑥𝑖𝑖‖2
    src = "\U0001d43d\U0001d43d(\U0001d436\U0001d436) = � ‖\U0001d465\U0001d465\U0001d456\U0001d456‖2"
    assert ae._normalize_pdf_math(src) == "J(C) = � ‖xi‖2"


def test_normalize_pdf_math_is_idempotent_and_noop_on_plain_text():
    assert ae._normalize_pdf_math("") == ""
    plain = "Ordinary ASCII prose with no math at all."
    assert ae._normalize_pdf_math(plain) == plain
    once = ae._normalize_pdf_math("\U0001d43e\U0001d43e-means and \U0001d6fc")  # 𝐾𝐾-means, 𝛼
    assert once == "K-means and α"
    assert ae._normalize_pdf_math(once) == once  # second pass changes nothing
