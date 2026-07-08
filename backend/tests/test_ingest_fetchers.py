"""Regression tests for the ingest fetcher HTTP layer.

These lock in the fix for the double-decode bug in ``_safe_get``: it streamed a
response through ``iter_bytes()`` (which content-decodes gzip/deflate/br) and
then rebuilt a fresh ``httpx.Response`` from those *already-decoded* bytes while
keeping the original ``Content-Encoding`` header. ``httpx.Response.__init__``
re-ran the decoder over the plain body and raised
``DecodingError("Error -3 … incorrect header check")`` for EVERY compressed
response — masking real HTTP statuses (e.g. a Cloudflare 403 challenge) as a
cryptic decompression error. ``_rebuffer_response`` fixes it by stripping the
now-inaccurate framing headers before rebuilding.

The suite is fully network-free.
"""

from __future__ import annotations

import httpx
import pytest

from types import SimpleNamespace

from backend.core.ingest_fetchers import (
    FetchedItem,
    _absolutize_img_srcs,
    _arxiv_html_url,
    _enrich_reddit_comments,
    _extract_youtube_channel_id,
    _fetch_post_comments,
    _format_reddit_comments,
    _guess_kb_category,
    _rebuffer_response,
    _reddit_author,
    _reddit_comment_rss_url,
    _strip_boilerplate_tail,
    _strip_html,
    items_to_knowledge_nodes,
)


def _streamed_stub(status: int, headers: dict[str, str]) -> httpx.Response:
    """Stand-in for the consumed streaming response.

    Only the attributes ``_rebuffer_response`` reads — ``status_code``,
    ``headers``, ``request`` — need to be real. Built with no body so the stub's
    own construction never triggers content-decoding; the stale wire-framing
    headers are overlaid afterwards.
    """
    resp = httpx.Response(status, request=httpx.Request("GET", "https://feed.example/rss"))
    resp.headers.update(headers)
    return resp


def test_rebuffer_strips_stale_encoding_headers_and_returns_decoded_body():
    decoded = b"<?xml version='1.0'?><rss><channel><title>ok</title></channel></rss>"
    streamed = _streamed_stub(
        200,
        {
            "Content-Type": "application/rss+xml",
            "Content-Encoding": "gzip",   # stale: body handed in is already decoded
            "Content-Length": "999999",   # stale: described the compressed wire length
        },
    )

    out = _rebuffer_response(streamed, decoded)

    # Accessing .content must NOT re-run the gunzip decoder over plain bytes.
    assert out.status_code == 200
    assert out.content == decoded
    # The stale wire-encoding header is gone (this is what stopped the re-decode).
    assert "content-encoding" not in out.headers
    # The stale compressed-wire Content-Length must not survive; httpx re-derives
    # an accurate one from the rebuilt body (== len(decoded)), which is fine.
    assert out.headers.get("content-length") in (None, str(len(decoded)))
    # Every other header is preserved verbatim.
    assert out.headers["content-type"] == "application/rss+xml"


def test_rebuffer_preserves_honest_status_for_empty_body():
    # The DecodingError fallback path rebuilds with an empty body; the caller's
    # status check must still see the true code (e.g. a Cloudflare 403).
    streamed = _streamed_stub(403, {"Content-Encoding": "br", "Server": "cloudflare"})

    out = _rebuffer_response(streamed, b"")

    assert out.status_code == 403
    assert out.content == b""
    assert "content-encoding" not in out.headers
    assert out.headers["server"] == "cloudflare"


def test_rebuffer_passthrough_when_no_encoding_header():
    # Plain (identity) responses must be untouched aside from the rebuild.
    streamed = _streamed_stub(200, {"Content-Type": "application/atom+xml"})

    out = _rebuffer_response(streamed, b"<feed/>")

    assert out.status_code == 200
    assert out.content == b"<feed/>"
    assert out.headers["content-type"] == "application/atom+xml"


def test_double_decode_is_the_bug_being_fixed():
    """Anchor: rebuilding from already-decoded bytes WITHOUT stripping the stale
    ``Content-Encoding`` reproduces the exact masking error this fix removes."""
    with pytest.raises(httpx.DecodingError):
        bad = httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            content=b"plain text, not actually gzip",
            request=httpx.Request("GET", "https://feed.example/rss"),
        )
        bad.read()  # force decode in case a future httpx defers it past __init__


# --------------------------------------------------------------------------- #
# YouTube channel-id extraction (_resolve_youtube_channel scrape step)         #
# --------------------------------------------------------------------------- #

# Real ids from the diagnostic: MKBHD's own channel vs. a recommended channel
# that appears EARLIER in the page's HTML.
_MKBHD_OWN = "UCBJycsmduvYEL83R_U4JriQ"
_RECOMMENDED = "UCG7J20LhUeLl6y_Emi7OJrA"


def test_youtube_channel_id_prefers_externalid_over_recommended_channelid():
    # The recommended channel's `channelId` appears first in document order, but
    # `externalId` (the page's own channel) must win — otherwise the feed would
    # silently subscribe to the wrong channel.
    html = (
        f'...,"channelId":"{_RECOMMENDED}",...'        # recommended, appears first
        f'..."externalId":"{_MKBHD_OWN}",...'
        f'<link rel="canonical" href="https://www.youtube.com/channel/{_MKBHD_OWN}">'
    )
    assert _extract_youtube_channel_id(html) == _MKBHD_OWN


def test_youtube_channel_id_uses_canonical_when_no_externalid():
    html = f'<link rel="canonical" href="https://www.youtube.com/channel/{_MKBHD_OWN}">'
    assert _extract_youtube_channel_id(html) == _MKBHD_OWN


def test_youtube_channel_id_bare_channelid_is_last_resort():
    html = f'noise "channelId":"{_MKBHD_OWN}" more noise'
    assert _extract_youtube_channel_id(html) == _MKBHD_OWN


def test_youtube_channel_id_none_when_absent():
    assert _extract_youtube_channel_id("<html>no channel markers</html>") is None
    assert _extract_youtube_channel_id("") is None


# --------------------------------------------------------------------------- #
# arXiv full-text URL derivation (fetch_arxiv enrichment step)                 #
# --------------------------------------------------------------------------- #


def test_arxiv_html_url_maps_abs_to_html_keeping_version():
    assert (
        _arxiv_html_url("http://arxiv.org/abs/2506.01234v1")
        == "https://arxiv.org/html/2506.01234v1"
    )
    # Versionless and https inputs work too.
    assert (
        _arxiv_html_url("https://arxiv.org/abs/2506.01234")
        == "https://arxiv.org/html/2506.01234"
    )


def test_arxiv_html_url_strips_query_and_fragment():
    assert (
        _arxiv_html_url("https://arxiv.org/abs/2506.01234v2?context=q-fin#sec1")
        == "https://arxiv.org/html/2506.01234v2"
    )


def test_arxiv_html_url_none_for_non_arxiv():
    assert _arxiv_html_url("https://example.com/abs/2506.01234") is None
    assert _arxiv_html_url("") is None
    assert _arxiv_html_url(None) is None


# --------------------------------------------------------------------------- #
# <img src> absolutization (asset-cache only downloads absolute http(s) URLs)  #
# --------------------------------------------------------------------------- #


def test_absolutize_img_srcs_resolves_relative_against_base():
    html = '<p>x</p><img src="x1.png"><img src="figures/f2.jpg" alt="f">'
    out = _absolutize_img_srcs(html, "https://arxiv.org/html/2506.01234v1/")
    assert '<img src="https://arxiv.org/html/2506.01234v1/x1.png">' in out
    assert 'src="https://arxiv.org/html/2506.01234v1/figures/f2.jpg"' in out


def test_absolutize_img_srcs_resolves_root_relative_and_protocol_relative():
    html = "<img src='/media/a.png'><img src='//cdn.example.com/b.png'>"
    out = _absolutize_img_srcs(html, "https://blog.example.com/posts/123")
    assert "src='https://blog.example.com/media/a.png'" in out
    assert "src='https://cdn.example.com/b.png'" in out


def test_absolutize_img_srcs_leaves_absolute_and_data_untouched():
    html = '<img src="https://cdn.x/y.png"><img src="data:image/png;base64,AA==">'
    assert _absolutize_img_srcs(html, "https://blog.example.com/") == html


def test_guess_kb_category_maps_known_sources():
    assert _guess_kb_category("https://rss.arxiv.org/rss/q-fin", "rss") == "papers"
    assert _guess_kb_category("anything", "arxiv") == "papers"
    assert _guess_kb_category("https://www.reddit.com/r/quant", "reddit") == "contextual"
    assert _guess_kb_category("https://quantpedia.com/feed/", "rss") == "alpha-ideas"
    assert _guess_kb_category("https://research.glassnode.com/rss/", "glassnode") == "factor-data"
    assert _guess_kb_category("https://insights.deribit.com/feed/", "rss") == "market-structure"
    assert _guess_kb_category("https://moontower.substack.com/feed", "rss") == "mental-models"
    assert _guess_kb_category("https://elmwealth.com/feed/", "rss") == "portfolio-management"
    assert _guess_kb_category("https://thegradient.pub/rss/", "rss") == "analysis-methods"
    assert _guess_kb_category("https://fredblog.stlouisfed.org/feed/", "rss") == "contextual"


def test_strip_html_decodes_entities_and_strips_tags():
    import backend.core.ingest_fetchers as inf
    out = inf._strip_html("<p>S&amp;P 500 doesn&#8217;t&nbsp;like &lt;risk&gt;</p>")
    assert out == "S&P 500 doesn’t like <risk>"
    # idempotent on already-clean text; no stray entities left
    assert "&amp;" not in inf._strip_html("a &amp; b")
    assert inf._strip_html("") == ""


def test_guess_kb_category_unknown_returns_none():
    assert _guess_kb_category("https://unknown-blog.example.com/feed", "rss") is None
    assert _guess_kb_category("", "rss") is None
    assert _guess_kb_category(None, "manual") is None


def test_absolutize_img_srcs_handles_empty_inputs():
    assert _absolutize_img_srcs("", "https://x.example/") == ""
    assert _absolutize_img_srcs("<img src='a.png'>", "") == "<img src='a.png'>"
    assert _absolutize_img_srcs(None, "https://x.example/") == ""


# --------------------------------------------------------------------------- #
# _parse_feed body selection: content:encoded full text must beat the teaser   #
# --------------------------------------------------------------------------- #


_FULL_TEXT_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>t</title>
<item>
  <title>Full-content post</title>
  <link>https://blog.example.com/p1</link>
  <description>Short teaser only.</description>
  <content:encoded><![CDATA[
    <p>The complete article body, much longer than the teaser, with figures.</p>
    <img src="https://cdn.example.com/fig1.png">
  ]]></content:encoded>
</item>
</channel></rss>"""


def test_parse_feed_prefers_full_content_over_teaser(monkeypatch):
    import backend.core.ingest_fetchers as inf

    def _fake_safe_get(url, headers=None, timeout=None, **kwargs):
        return httpx.Response(
            200,
            headers={"Content-Type": "application/rss+xml"},
            content=_FULL_TEXT_FEED,
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(inf, "_safe_get", _fake_safe_get)
    monkeypatch.setenv("ARTICLE_ENRICH_ENABLED", "0")

    out = inf._parse_feed("https://blog.example.com/rss")
    assert out.error is None
    assert len(out.items) == 1
    item = out.items[0]
    assert "complete article body" in item.body_markdown
    assert "Short teaser only." not in item.body_markdown
    # The full-content HTML (with its inline images) is what reaches the
    # asset-cache pass, not the teaser.
    assert 'src="https://cdn.example.com/fig1.png"' in item.raw_html


# --------------------------------------------------------------------------- #
# Reddit comment enrichment — .rss carries only posts; comments add the signal #
# --------------------------------------------------------------------------- #


def test_reddit_comment_rss_url_maps_post_permalink():
    assert (
        _reddit_comment_rss_url(
            "https://www.reddit.com/r/quant/comments/abc123/some_title/", limit=50
        )
        == "https://www.reddit.com/r/quant/comments/abc123/some_title/.rss?sort=top&limit=50"
    )
    # Query/fragment dropped; trailing .rss stripped before re-appending.
    assert _reddit_comment_rss_url(
        "https://old.reddit.com/r/algotrading/comments/xy9/t.rss?x=1#c", limit=25
    ) == "https://old.reddit.com/r/algotrading/comments/xy9/t/.rss?sort=top&limit=25"


def test_reddit_comment_rss_url_none_for_non_post():
    assert _reddit_comment_rss_url("https://www.reddit.com/user/foo", limit=50) is None
    assert _reddit_comment_rss_url("https://example.com/r/x/comments/abc", limit=50) is None
    assert _reddit_comment_rss_url("", limit=50) is None
    assert _reddit_comment_rss_url(None, limit=50) is None


def test_reddit_author_normalises_prefixes():
    assert _reddit_author("/u/Clicketrie") == "Clicketrie"
    assert _reddit_author("u/bob") == "bob"
    assert _reddit_author("/user/carol") == "carol"
    assert _reddit_author("plain") == "plain"
    assert _reddit_author("") == ""


# Each comment <entry>'s link carries an extra id segment after the slug; the
# post's own echo entry stops at the slug (so it is filtered out).
_POST = "https://www.reddit.com/r/quant/comments/abc/slug"


def _entry(author, body, *, comment_id="c0"):
    link = f"{_POST}/" if comment_id is None else f"{_POST}/{comment_id}/"
    return {"author": author, "link": link, "content": [{"value": body}]}


def test_format_reddit_comments_filters_and_caps():
    entries = [
        _entry("/u/OP", "the original self-post echo, fairly long body here", comment_id=None),
        _entry("/u/alice", "A substantive top comment about factor construction.", comment_id="c1"),
        _entry("/u/AutoModerator", "Bot boilerplate that must be filtered out always.", comment_id="c2"),
        _entry("/u/bob", "+1", comment_id="c3"),                       # too short
        _entry("/u/carol", "Second useful comment with real detail inside.", comment_id="c4"),
        _entry("/u/dave", "Third comment, also long enough to be kept here.", comment_id="c5"),
    ]
    out = _format_reddit_comments(entries, per_post=2, min_chars=10, max_chars=10_000)
    assert out.startswith("## Top comments")
    # Feed order preserved; first 2 QUALIFYING comments kept (alice, carol).
    assert "u/alice" in out and "factor construction" in out
    assert "u/carol" in out
    assert "u/dave" not in out          # beyond per_post cap
    assert "OP" not in out              # post echo dropped (no comment id in link)
    assert "AutoModerator" not in out   # bot dropped
    assert "u/bob" not in out           # too short


def test_format_reddit_comments_empty_when_nothing_qualifies():
    assert _format_reddit_comments([], per_post=5, min_chars=10, max_chars=100) == ""
    only_post = [_entry("/u/OP", "x" * 200, comment_id=None)]   # post echo only
    assert _format_reddit_comments(only_post, per_post=5, min_chars=10, max_chars=100) == ""


def test_format_reddit_comments_respects_max_chars():
    entries = [_entry("/u/a", "z" * 500, comment_id="c1")]
    out = _format_reddit_comments(entries, per_post=5, min_chars=1, max_chars=50)
    assert len(out) <= 50


_ATOM_TMPL = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>comments</title>
  <entry>
    <author><name>/u/OP</name></author>
    <link href="{post}/" />
    <content type="html">original self post echo body</content>
  </entry>
  <entry>
    <author><name>/u/alice</name></author>
    <link href="{post}/c1/" />
    <content type="html">This is a substantive comment about momentum factors.</content>
  </entry>
</feed>"""


def _rss_response(url, body, status=200):
    return httpx.Response(
        status, content=body.encode("utf-8"), request=httpx.Request("GET", url)
    )


def test_fetch_post_comments_parses_comment_rss(monkeypatch):
    import backend.core.ingest_fetchers as inf

    atom = _ATOM_TMPL.format(post=_POST)

    def _fake_get(u, headers=None, timeout=None, **kw):
        return _rss_response(u, atom)

    monkeypatch.setattr(inf, "_safe_get", _fake_get)
    out = _fetch_post_comments(
        "https://www.reddit.com/r/quant/comments/abc/slug/",
        timeout_s=5.0, per_post=5, min_chars=10, max_chars=5000,
    )
    assert out is not None
    assert "u/alice" in out and "momentum factors" in out
    assert "self post echo" not in out  # OP echo entry dropped


def test_fetch_post_comments_falls_back_www_to_old(monkeypatch):
    import backend.core.ingest_fetchers as inf

    atom = _ATOM_TMPL.format(post=_POST.replace("www.reddit.com", "old.reddit.com"))

    def _fake_get(u, headers=None, timeout=None, **kw):
        if "www.reddit.com" in u:
            return _rss_response(u, "blocked", status=403)  # www blocked
        return _rss_response(u, atom)                        # old.reddit.com OK

    monkeypatch.setattr(inf, "_safe_get", _fake_get)
    out = _fetch_post_comments(
        "https://www.reddit.com/r/quant/comments/abc/slug/",
        timeout_s=5.0, per_post=5, min_chars=10, max_chars=5000,
    )
    assert out is not None and "u/alice" in out


def test_enrich_reddit_comments_appends_and_pins_dedup_body(monkeypatch):
    import backend.core.ingest_fetchers as inf

    monkeypatch.setattr(inf, "_fetch_post_comments", lambda *a, **k: "## Top comments\n\nx")
    item = FetchedItem(
        title="Q",
        url="https://www.reddit.com/r/quant/comments/abc/x/",
        body_markdown="original post",
    )
    _enrich_reddit_comments([item])
    assert item.body_markdown == "original post\n\n## Top comments\n\nx"
    # dedup_body pinned to the PRE-comment body so the node hash stays stable.
    assert item.dedup_body == "original post"


def test_enrich_reddit_comments_caps_attempts_and_skips_non_posts(monkeypatch):
    import backend.core.ingest_fetchers as inf

    monkeypatch.setenv("REDDIT_COMMENTS_MAX_POSTS", "2")
    calls: list[str] = []

    def _fake_fetch(url, **k):
        calls.append(url)
        return "## Top comments\n\nc"

    monkeypatch.setattr(inf, "_fetch_post_comments", _fake_fetch)
    items = [
        FetchedItem(title="non-post", url="https://www.reddit.com/user/foo", body_markdown="b"),
        FetchedItem(title="p1", url="https://www.reddit.com/r/quant/comments/a1/x/", body_markdown="b1"),
        FetchedItem(title="p2", url="https://www.reddit.com/r/quant/comments/a2/x/", body_markdown="b2"),
        FetchedItem(title="p3", url="https://www.reddit.com/r/quant/comments/a3/x/", body_markdown="b3"),
    ]
    _enrich_reddit_comments(items)
    # Only the 2 (cap) post permalinks attempted; the user-feed item never counts.
    assert len(calls) == 2
    assert items[0].dedup_body is None  # untouched non-post


def test_enrich_reddit_comments_spaces_requests(monkeypatch):
    import backend.core.ingest_fetchers as inf

    monkeypatch.setenv("REDDIT_COMMENTS_MAX_POSTS", "3")
    monkeypatch.setenv("REDDIT_COMMENTS_DELAY_MS", "20")  # tiny; assert spacing fires
    sleeps: list[float] = []
    monkeypatch.setattr(inf.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(inf, "_fetch_post_comments", lambda *a, **k: "## Top comments\n\nc")

    def _posts():
        return [
            FetchedItem(title=f"p{i}", body_markdown=f"b{i}",
                        url=f"https://www.reddit.com/r/quant/comments/a{i}/x/")
            for i in range(3)
        ]

    _enrich_reddit_comments(_posts())
    # 3 fetches → spacing fires only BETWEEN them: first is immediate, so N-1.
    assert len(sleeps) == 2
    assert all(0.0 < s <= 0.02 for s in sleeps)

    # Disabled via env → no spacing, byte-identical to the legacy back-to-back path.
    sleeps.clear()
    monkeypatch.setenv("REDDIT_COMMENTS_DELAY_MS", "0")
    _enrich_reddit_comments(_posts())
    assert sleeps == []


# --------------------------------------------------------------------------- #
# dedup_body keeps a thread on ONE node even as its comments drift             #
# --------------------------------------------------------------------------- #


def test_items_to_knowledge_nodes_dedup_body_stabilises_hash():
    src = SimpleNamespace(source_type="reddit", url="https://www.reddit.com/r/quant.rss")
    base = dict(title="T", url="https://www.reddit.com/r/quant/comments/abc/x/")
    poll1 = FetchedItem(**base, body_markdown="post\n\n## Top comments\n\nalice 10pts",
                        dedup_body="post")
    poll2 = FetchedItem(**base, body_markdown="post\n\n## Top comments\n\nalice 12pts; bob 3pts",
                        dedup_body="post")
    h1 = items_to_knowledge_nodes([poll1], src)[0]["content_hash"]
    h2 = items_to_knowledge_nodes([poll2], src)[0]["content_hash"]
    # Comments drifted between polls but the dedup hash is identical → one node.
    assert h1 == h2
    # …yet the stored content reflects the latest comments.
    assert "bob 3pts" in items_to_knowledge_nodes([poll2], src)[0]["content"]


def test_items_to_knowledge_nodes_without_dedup_body_hashes_full_body():
    src = SimpleNamespace(source_type="rss", url="https://blog.example.com/feed")
    a = FetchedItem(title="T", url="https://blog.example.com/p", body_markdown="body A")
    b = FetchedItem(title="T", url="https://blog.example.com/p", body_markdown="body B")
    ha = items_to_knowledge_nodes([a], src)[0]["content_hash"]
    hb = items_to_knowledge_nodes([b], src)[0]["content_hash"]
    assert ha != hb  # no dedup_body → behaviour unchanged (full body hashed)


def test_content_hash_for_item_matches_items_to_knowledge_nodes():
    # The scheduler's pre-download dup check hashes via _content_hash_for_item;
    # it MUST equal the hash the persist path (items_to_knowledge_nodes) computes,
    # or the optimisation would skip downloads for the wrong items.
    from backend.core.ingest_fetchers import _content_hash_for_item

    src = SimpleNamespace(source_type="reddit", url="https://www.reddit.com/r/quant.rss")
    for it in (
        FetchedItem(title="A", url="https://x/p", body_markdown="post\n\ncomments",
                    dedup_body="post"),
        FetchedItem(title="B", url="https://x/q", body_markdown="plain body"),
    ):
        assert _content_hash_for_item(it) == items_to_knowledge_nodes([it], src)[0]["content_hash"]


# --------------------------------------------------------------------------- #
# _strip_boilerplate_tail — drop CMS style-guide / lorem-ipsum template tails  #
#                                                                             #
# Webflow blogs (e.g. Paradigm.co) expose a hidden rich-text "style guide"     #
# demo that extractors scrape onto the END of the article body: a "Heading     #
# 1..6" run, a Lorem-ipsum paragraph, then "Block quote / Bold text / Emphasis #
# / Superscript / Subscript". Strip it — trailing-only, anchored on placeholder #
# strings that never occur in real prose, never gutting a real article.        #
# --------------------------------------------------------------------------- #


def test_strip_boilerplate_tail_removes_webflow_styleguide():
    body = (
        "Real article paragraph about implied volatility. " * 8 + "\n"
        "Heading 1\nHeading 2\nHeading 3\nHeading 4\nHeading 5\nHeading 6\n"
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit.\n"
        "Block quote\nBold text\nEmphasis\nSuperscript\nSubscript"
    )
    out = _strip_boilerplate_tail(body)
    assert "Lorem ipsum" not in out
    assert "Heading 6" not in out
    assert "Superscript" not in out
    assert out.rstrip().endswith("implied volatility.")


def test_strip_boilerplate_tail_removes_bare_lorem():
    body = "A genuine sentence about option Greeks. " * 10 + "\nLorem ipsum dolor sit amet, foo."
    out = _strip_boilerplate_tail(body)
    assert "Lorem ipsum" not in out
    assert out.startswith("A genuine sentence")


def test_strip_boilerplate_tail_leaves_real_prose_untouched():
    body = "An article that legitimately discusses heading strategies and option Greeks."
    assert _strip_boilerplate_tail(body) == body
    # A lowercase 'heading' mid-sentence is NOT the placeholder run.
    assert _strip_boilerplate_tail("Heading into Q3, vol rose sharply.") == \
        "Heading into Q3, vol rose sharply."


def test_strip_boilerplate_tail_noop_guard_when_overmatch_would_gut_body():
    # If stripping would leave < the floor, keep the original (never destroy a node).
    body = "x\nLorem ipsum dolor sit amet and then nothing of substance here."
    assert _strip_boilerplate_tail(body) == body


def test_strip_html_drops_trailing_styleguide():
    html = (
        "<p>" + "Body text about funding rates. " * 8 + "</p>"
        "<div class='w-richtext-figure'>"
        "<p>Heading 1</p><p>Heading 2</p><p>Heading 3</p>"
        "<p>Heading 4</p><p>Heading 5</p><p>Heading 6</p>"
        "<p>Lorem ipsum dolor sit amet, consectetur.</p>"
        "<p>Bold text</p><p>Emphasis</p></div>"
    )
    out = _strip_html(html)
    assert "Lorem ipsum" not in out
    assert "Heading 6" not in out
    assert "Body text about funding rates." in out
