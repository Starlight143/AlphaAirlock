"""Regression tests for asset_cache.enrich_markdown_with_images cap handling.

The scheduler caps KnowledgeNode.content at 32 000 chars AFTER the image refs
are appended; a full-text body already at the cap used to push every ref off
the end. The body must be trimmed instead so the refs always survive.
Fully network/DB-free — cache_images_for_html is monkeypatched.
"""

from __future__ import annotations

from types import SimpleNamespace

import backend.core.asset_cache as asset_cache
from backend.core.database import KB_CONTENT_MAX_CHARS


_SHA = "a" * 64


def _fake_pairs(*_args, **_kwargs):
    row = SimpleNamespace(id=7, sha256=_SHA)
    return [("https://cdn.example.com/img.png", row)]


def test_image_refs_survive_the_content_cap(monkeypatch):
    monkeypatch.setattr(asset_cache, "cache_images_for_html", _fake_pairs)
    body = "x" * KB_CONTENT_MAX_CHARS  # already at the downstream cap
    out, ids = asset_cache.enrich_markdown_with_images(
        body_markdown=body, raw_html="<img src='https://x/i.png'>",
        source_id=1, session=None,
    )
    assert ids == [7]
    assert len(out) <= KB_CONTENT_MAX_CHARS
    ref = f"![image](/api/assets/{_SHA})"
    # The ref is intact at the END — i.e. it would survive scheduler truncation.
    assert out.endswith(ref)


def test_short_body_is_not_trimmed(monkeypatch):
    monkeypatch.setattr(asset_cache, "cache_images_for_html", _fake_pairs)
    body = "short body"
    out, ids = asset_cache.enrich_markdown_with_images(
        body_markdown=body, raw_html="<img src='https://x/i.png'>",
        source_id=1, session=None,
    )
    assert out.startswith("short body\n\n")
    assert out.endswith(f"![image](/api/assets/{_SHA})")
    assert ids == [7]


def test_no_images_returns_body_unchanged(monkeypatch):
    monkeypatch.setattr(asset_cache, "cache_images_for_html", lambda **_: [])
    body = "y" * (KB_CONTENT_MAX_CHARS + 8_000)  # over cap, but no refs → untouched here
    out, ids = asset_cache.enrich_markdown_with_images(
        body_markdown=body, raw_html="", source_id=1, session=None,
    )
    assert out == body
    assert ids == []


# --------------------------------------------------------------------------- #
# Pre-download (prefetch_image_bytes) — the 'database is locked' fix: image     #
# downloads must happen OUTSIDE the persist transaction. These lock in that     #
# (a) prefetch downloads each cacheable URL once, honouring the per-item cap,   #
# and (b) _cache_one consumes the prefetched bytes instead of hitting the net.  #
# --------------------------------------------------------------------------- #

import hashlib  # noqa: E402

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.core.database import AssetCache  # noqa: E402

# Minimal valid PNG: 8-byte magic + 4 pad bytes => 12 bytes (_sniff_mime needs ≥12).
_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00"


def _boom_download(*_a, **_k):  # any network attempt is a test failure
    raise AssertionError("network _download must not be called when prefetched")


def test_prefetch_image_bytes_downloads_each_url_once(monkeypatch):
    monkeypatch.setattr(asset_cache, "is_enabled", lambda: True)
    calls: list[str] = []

    def _fake_download(url, _limit, _timeout):
        calls.append(url)
        return _PNG

    monkeypatch.setattr(asset_cache, "_download", _fake_download)
    html = (
        '<img src="https://cdn.x/a.png">'
        '<img src="https://cdn.x/a.png">'   # dup — must collapse
        '<img src="https://cdn.x/b.png">'
        '<img src="/relative.png">'          # relative — skipped
        '<img src="data:image/png;base64,AA==">'  # data: — skipped
    )
    out = asset_cache.prefetch_image_bytes(html)
    assert set(out) == {"https://cdn.x/a.png", "https://cdn.x/b.png"}
    assert out["https://cdn.x/a.png"] == _PNG
    assert calls == ["https://cdn.x/a.png", "https://cdn.x/b.png"]  # once each, no dup


def test_prefetch_image_bytes_respects_per_item_cap(monkeypatch):
    monkeypatch.setattr(asset_cache, "is_enabled", lambda: True)
    monkeypatch.setattr(asset_cache, "_max_per_item", lambda: 2)
    monkeypatch.setattr(asset_cache, "_download", lambda *_: _PNG)
    html = "".join(f'<img src="https://cdn.x/{i}.png">' for i in range(5))
    out = asset_cache.prefetch_image_bytes(html)
    assert len(out) == 2  # capped


def test_prefetch_image_bytes_failed_download_recorded_as_none(monkeypatch):
    monkeypatch.setattr(asset_cache, "is_enabled", lambda: True)
    monkeypatch.setattr(asset_cache, "_download", lambda *_: None)
    out = asset_cache.prefetch_image_bytes('<img src="https://cdn.x/a.png">')
    assert out == {"https://cdn.x/a.png": None}


def test_prefetch_image_bytes_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(asset_cache, "is_enabled", lambda: False)
    monkeypatch.setattr(asset_cache, "_download", _boom_download)
    assert asset_cache.prefetch_image_bytes('<img src="https://cdn.x/a.png">') == {}


def _mem_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    AssetCache.__table__.create(bind=engine, checkfirst=True)
    return sessionmaker(bind=engine)(), engine


def test_cache_one_uses_prefetched_bytes_without_downloading(monkeypatch, tmp_path):
    monkeypatch.setattr(asset_cache, "is_enabled", lambda: True)
    monkeypatch.setattr(asset_cache, "_download", _boom_download)  # would raise if hit
    monkeypatch.setattr(
        asset_cache, "_absolute_path_for", lambda h, ext: tmp_path / f"{h}.{ext}"
    )
    url = "https://cdn.x/a.png"
    s, engine = _mem_session()
    try:
        row = asset_cache._cache_one(
            url=url, source_id=1, session=s, prefetched={url: _PNG},
        )
        assert row is not None
        assert row.sha256 == hashlib.sha256(_PNG).hexdigest()
        assert row.mime_type == "image/png"
        # And the cacheable-URL pass (cache_images_for_html) threads it through too.
        s2_pairs = asset_cache.cache_images_for_html(
            raw_html=f'<img src="{url}">', source_id=1, session=s, prefetched={url: _PNG},
        )
        assert len(s2_pairs) == 1
    finally:
        s.close()
        engine.dispose()


def test_cache_one_prefetched_none_is_a_miss_no_download(monkeypatch, tmp_path):
    monkeypatch.setattr(asset_cache, "is_enabled", lambda: True)
    monkeypatch.setattr(asset_cache, "_download", _boom_download)
    url = "https://cdn.x/gone.png"
    s, engine = _mem_session()
    try:
        # Prefetched value None == the pre-download already failed → miss, NOT a
        # re-download (which would raise via _boom_download).
        row = asset_cache._cache_one(
            url=url, source_id=1, session=s, prefetched={url: None},
        )
        assert row is None
    finally:
        s.close()
        engine.dispose()


# --------------------------------------------------------------------------- #
# _is_chrome_img / _iter_cacheable_img_urls — skip UI chrome, keep real charts #
#                                                                             #
# A page whose first <img>s are chrome (HubSpot blog: logo + lucide section    #
# icons + megamenu thumbnails) used to exhaust the per-item cap before any     #
# real figure was reached. The iterator now skips logos/icons/avatars/sprites/ #
# CTAs/tracking-pixels/tiny-badges — but NEVER excludes SVG wholesale, because #
# some sources publish genuine charts as inline SVG. Network/DB-free.          #
# --------------------------------------------------------------------------- #


def _img(src: str) -> str:
    return f'<img src="{src}">'


def test_is_chrome_img_classifies_chrome_and_figures():
    chrome = [
        "https://blog.x.io/hubfs/assets/lucide-icons/zoom-in.svg",
        "https://blog.x.io/hubfs/assets/megamenu/promo.png",
        "https://x.io/icons/rss.png",
        "https://x.io/site-logo.png",
        "https://x.io/hubfs/amberdata_logo_color.svg",
        "https://cdn.x.io/avatar/author.png",
        "https://x.io/cta/subscribe.png",
        "https://no-cache.hubspot.com/cta/default/1/abc.png",
        "https://x.io/img/tracking-pixel.gif",
        "https://x.io/i/spacer.gif",
        "https://x.io/fig.png?width=40",                       # tiny badge
        "https://x.io/b.png?width=24&amp;height=24",           # &amp;-encoded tiny dims
    ]
    figures = [
        "https://blog.x.io/hubfs/OPTIONSMACRO-Image-1.jpg?width=400&height=493",
        "https://x.io/figures/vol-surface.svg",                # real inline-SVG chart
        "https://x.io/2026/06/realized-vol.png?width=810",
        "https://x.io/wp-content/uploads/returns.jpg",
    ]
    for u in chrome:
        assert asset_cache._is_chrome_img(u), f"should be chrome: {u}"
    for u in figures:
        assert not asset_cache._is_chrome_img(u), f"should be a figure: {u}"


def test_iter_skips_chrome_keeps_real_figures_in_order():
    html = "".join(_img(u) for u in [
        "https://blog.x.io/hubfs/amberdata_logo_color.svg",
        "https://blog.x.io/hubfs/assets/lucide-icons/vault.svg",
        "https://blog.x.io/hubfs/assets/megamenu/dash.png",
        "https://cdn.x.io/avatar/author.png",
        "https://x.io/cta/subscribe.png",
        "https://x.io/img/tracking.gif",
        "https://blog.x.io/hubfs/square-kanban.png?width=24&height=24",
        "https://blog.x.io/hubfs/OPTIONSMACRO-Image-1.jpg?width=400&height=493",
        "https://blog.x.io/hubfs/figure-2.png?width=810",
    ])
    assert list(asset_cache._iter_cacheable_img_urls(html)) == [
        "https://blog.x.io/hubfs/OPTIONSMACRO-Image-1.jpg?width=400&height=493",
        "https://blog.x.io/hubfs/figure-2.png?width=810",
    ]


def test_iter_does_not_exclude_svg_charts():
    html = _img("https://quant.example.com/charts/efficient-frontier.svg")
    assert list(asset_cache._iter_cacheable_img_urls(html)) == [
        "https://quant.example.com/charts/efficient-frontier.svg"
    ]


def test_iter_filters_chrome_before_consuming_the_cap(monkeypatch):
    monkeypatch.setenv("ASSET_CACHE_MAX_IMAGES_PER_ITEM", "2")
    html = "".join(_img(u) for u in [
        "https://x.io/site-logo.png",            # chrome — must not consume a slot
        "https://x.io/icons/menu.png",           # chrome
        "https://x.io/hubfs/chart-a.png?width=600",
        "https://x.io/hubfs/chart-b.png?width=600",
        "https://x.io/hubfs/chart-c.png?width=600",
    ])
    # cap=2 applies to the REAL figures, not the chrome that preceded them.
    assert list(asset_cache._iter_cacheable_img_urls(html)) == [
        "https://x.io/hubfs/chart-a.png?width=600",
        "https://x.io/hubfs/chart-b.png?width=600",
    ]
