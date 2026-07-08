"""Image asset cache (P6-B7).

The reference YouTube system shows ingested articles with their original
illustrations rendered inline — so the agent can read both prose *and* charts.
Our backend already had an ``AssetCache`` table but nothing ever wrote to it.
This module wires up the missing piece:

1. **Extract** ``<img src=...>`` URLs from a fetched item's ``raw_html``.
2. **Download** each image (httpx, hard byte cap + timeout) and **content-hash**
   it (sha256). The hash is the de-dup key — the same image reused across
   sources / posts stores once.
3. **Persist** the bytes to ``storage/assets/<first2>/<full-hash>.<ext>`` and
   write an ``AssetCache`` row pointing at it.
4. **Rewrite** the node's markdown body so the inline references point at
   ``/api/assets/<hash>`` (served by the FastAPI route in ``main.py``). The
   original feed URL is preserved in ``AssetCache.original_url`` for audit.

Safety rails (CLAUDE.md cost-guard rule)
----------------------------------------
* **Default OFF** — gated by ``ASSET_CACHE_ENABLED``. When off this module is
  a no-op so dev environments without persistent storage don't accumulate
  random downloads.
* **Per-image cap** ``ASSET_CACHE_MAX_BYTES_PER_IMAGE`` (default 5 MB).
* **Per-item cap** ``ASSET_CACHE_MAX_IMAGES_PER_ITEM`` (default 8).
* **HTTP timeout** ``ASSET_CACHE_TIMEOUT_SECONDS`` (default 10).
* **Path-traversal guard** — every served path is realpath-checked against
  ``ASSETS_DIR`` so a poisoned ``AssetCache.local_path`` can't read arbitrary
  files (defense in depth; we control all writers, but defend anyway).
* **Sniffed mime** — we trust the file content (magic bytes), not the HTTP
  ``Content-Type`` header, to decide ``mime_type``.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import socket
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend._envloader import env_bool, env_int
from backend.core.database import KB_CONTENT_MAX_CHARS, PROJECT_ROOT, AssetCache

# P30-S3: shared SSRF guard for asset downloads. Inline <img src=> URLs
# come from attacker-controlled feed content. Per-hop IP-class check
# must run on every redirect target rather than only initial URL.
_ASSET_MAX_REDIRECTS = 5


def _assert_public_host(url: str) -> Optional[str]:
    # P31-D2: canonical strict-whitelist env_bool (already imported).
    # Returns the validated public IP literal so the caller can pin the
    # connection and defeat DNS-rebinding/TOCTOU. Returns None when bypassed.
    if env_bool("ALPHA_ALLOW_LOCAL_INGEST", False):
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
    return str(candidates[0])


def _pin_request(
    url: str,
    headers: dict,
    pinned_ip: Optional[str],
) -> Tuple[str, dict, dict]:
    """Rewrite a request to connect to a pre-validated IP while preserving the
    original Host header + TLS SNI. See ingest_fetchers._pin_request."""
    if not pinned_ip:
        return url, headers, {}
    parsed = httpx.URL(url)
    original_host = parsed.host
    if not original_host:
        return url, headers, {}
    try:
        ipaddress.ip_address(original_host)
        return url, headers, {}
    except ValueError:
        pass
    connect_url = str(parsed.copy_with(host=pinned_ip))
    out_hdrs = dict(headers)
    port = parsed.port
    host_header = original_host if port is None else f"{original_host}:{port}"
    for k in list(out_hdrs.keys()):
        if k.lower() == "host":
            del out_hdrs[k]
    out_hdrs["Host"] = host_header
    return connect_url, out_hdrs, {"sni_hostname": original_host}

logger = logging.getLogger("alpha.asset_cache")

ASSETS_DIR = PROJECT_ROOT / "storage" / "assets"

_storage_ready: bool = False
_STORAGE_LOCK = __import__("threading").Lock()


def _ensure_storage() -> None:
    """Lazily create ASSETS_DIR on first actual use (not at import time).

    Deferred so that importing this module with ASSET_CACHE_ENABLED=False
    (or in a read-only container image before the volume is mounted) does
    not trigger a filesystem write — preserving the documented 'no-op when
    disabled' contract.
    """
    global _storage_ready
    if _storage_ready:  # fast-path without lock
        return
    with _STORAGE_LOCK:
        if not _storage_ready:  # re-check inside lock
            ASSETS_DIR.mkdir(parents=True, exist_ok=True)
            _storage_ready = True


_IMG_SRC_RE = re.compile(
    r'<img[^>]+src=["\']([^"\']+)["\']',
    re.IGNORECASE,
)

# UI chrome that must never be cached as an article figure: site logos, nav/menu
# icons, avatars, sprites, CTA buttons, tracking pixels and tiny badges. Without
# this, a page whose first images are chrome (e.g. a HubSpot blog: logo + a row
# of section icons + megamenu thumbnails) exhausts the per-item cap before the
# real charts are reached. Matched by unambiguous path/name patterns plus a
# tiny-dimension floor. NOTE: SVG is intentionally NOT excluded wholesale —
# several sources publish genuine charts as inline SVG.
_CHROME_IMG_RE = re.compile(
    r"/lucide-icons?/|/megamenu/|/icons?/|/sprites?/|/emoji/"
    r"|[-_/]logos?[-_./]|gravatar|/avatars?/|[-_/]avatar[-_.]"
    r"|/cta/|/hs/cta|no-cache\.hubspot|trust-center"
    r"|[-_/](?:spacer|pixel|blank|1x1|tracking)[-_.]",
    re.IGNORECASE,
)
# Explicit width/height in the URL below this many px ⇒ an icon/badge, not a
# figure. ``(?:amp;)?`` tolerates HTML-encoded ``&amp;`` query separators.
_CHROME_DIM_RE = re.compile(r"[?&](?:amp;)?(?:width|height)=(\d+)", re.IGNORECASE)
_CHROME_MIN_DIM = 64


def _is_chrome_img(url: str) -> bool:
    """True for non-content UI imagery that should not be cached as a figure."""
    if _CHROME_IMG_RE.search(url):
        return True
    for m in _CHROME_DIM_RE.finditer(url):
        if int(m.group(1)) < _CHROME_MIN_DIM:
            return True
    return False

# Magic-byte mime sniffer — keeps us off PIL / python-magic system deps.
_MAGIC_PREFIXES: list[tuple[bytes, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
    (b"RIFF", "image/webp", "webp"),  # WebP — RIFF....WEBP signature, deeper check below
]


def is_enabled() -> bool:
    return env_bool("ASSET_CACHE_ENABLED", False)


def _max_bytes() -> int:
    return env_int("ASSET_CACHE_MAX_BYTES_PER_IMAGE", 5 * 1024 * 1024, minimum=1024)


def _max_per_item() -> int:
    return env_int("ASSET_CACHE_MAX_IMAGES_PER_ITEM", 8, minimum=0, maximum=64)


def _timeout_seconds() -> float:
    return float(env_int("ASSET_CACHE_TIMEOUT_SECONDS", 10, minimum=1, maximum=120))


def _sniff_mime(data: bytes) -> Optional[Tuple[str, str]]:
    """Return ``(mime, extension)`` for a known image type, else None."""
    if not data or len(data) < 12:
        return None
    for prefix, mime, ext in _MAGIC_PREFIXES:
        if data.startswith(prefix):
            if mime == "image/webp":
                # RIFF....WEBP — only accept genuine WebP.
                if data[8:12] != b"WEBP":
                    continue
            return mime, ext
    return None


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _absolute_path_for(hash_hex: str, ext: str) -> Path:
    _ensure_storage()
    bucket = ASSETS_DIR / hash_hex[:2]
    bucket.mkdir(parents=True, exist_ok=True)
    return bucket / f"{hash_hex}.{ext}"


def _download(url: str, limit: int, timeout_s: float) -> Optional[bytes]:
    """Download with hard byte cap + UA, manual redirect loop. P30-S3."""
    # Browser UA: the old branded UA was bot-fingerprinted by Cloudflare/CDNs and
    # served a 403 challenge instead of the image (mirrors the feed-fetch fix in
    # ingest_fetchers.USER_AGENT). Kept inline — this module deliberately
    # duplicates its SSRF helpers rather than importing ingest_fetchers.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    cur = url
    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 5.0)),
            follow_redirects=False,
        ) as cli:
            for _ in range(_ASSET_MAX_REDIRECTS + 1):
                pinned_ip = _assert_public_host(cur)
                connect_url, req_hdrs, req_ext = _pin_request(cur, headers, pinned_ip)
                with cli.stream("GET", connect_url, headers=req_hdrs, extensions=req_ext) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location") or ""
                        if not loc:
                            logger.debug("Asset cache: %s redirect w/o Location", cur)
                            return None
                        cur = str(httpx.URL(cur).join(loc))
                        resp.read()  # drain body so connection returns to pool
                        continue
                    if resp.status_code >= 400:
                        logger.debug("Asset cache: %s HTTP %s", cur, resp.status_code)
                        return None
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > limit:
                            logger.info("Asset cache: %s exceeded %s bytes, aborting", cur, limit)
                            return None
                    return b"".join(chunks)
            logger.debug("Asset cache: too many redirects (>%d) for %s", _ASSET_MAX_REDIRECTS, url)
            return None
    except ValueError as exc:
        logger.info("Asset cache: SSRF-refused for %s: %s", url, exc)
        return None
    except httpx.RequestError as exc:
        logger.debug("Asset cache: network error for %s: %s", url, exc)
        return None
    except Exception:  # noqa: BLE001
        logger.exception("Asset cache: unexpected error for %s", url)
        return None


def _cache_one(
    *,
    url: str,
    source_id: Optional[int],
    session: Session,
    prefetched: Optional[dict] = None,
) -> Optional[AssetCache]:
    """Cache a single image URL. Returns the AssetCache row or None.

    ``prefetched`` (optional) is a ``{url: bytes | None}`` map of bytes already
    downloaded *outside* the caller's DB transaction (see
    ``prefetch_image_bytes``). When provided AND it contains ``url``, the bytes
    are taken from it instead of issuing a network ``_download`` here — this is
    what keeps the scheduler's persist transaction free of network I/O (the
    ``database is locked`` / SQLITE_BUSY_SNAPSHOT root cause). A ``None`` value
    means the pre-download already failed, so we treat it as a miss without
    re-downloading. URLs absent from the map fall back to ``_download`` so other
    callers (the relink job, tests) keep working unchanged.
    """
    # De-dup by source URL first (avoid re-downloading on every relink).
    existing = (
        session.query(AssetCache)
        .filter(AssetCache.original_url == url)
        .first()
    )
    if existing is not None and existing.sha256:
        return existing

    if prefetched is not None and url in prefetched:
        data = prefetched[url]  # bytes, or None when the pre-download failed
    else:
        data = _download(url, _max_bytes(), _timeout_seconds())
    if data is None:
        return None
    sniff = _sniff_mime(data)
    if sniff is None:
        logger.debug("Asset cache: %s skipped (unknown image format)", url)
        return None
    mime, ext = sniff
    digest = _hash_bytes(data)

    # Content-level dedup: same bytes from a different URL → reuse path.
    dup = (
        session.query(AssetCache)
        .filter(AssetCache.sha256 == digest)
        .first()
    )
    if dup is not None:
        # Update the URL alias so we don't redownload from this URL again.
        # SAVEPOINT-guard: a concurrent worker may have inserted this same
        # original_url first (ux_asset_cache_original_url). On collision the
        # nested rollback leaves the outer transaction usable (critical on
        # PostgreSQL) and we fall through to return the shared content row.
        if existing is None:
            try:
                with session.begin_nested():
                    session.add(
                        AssetCache(
                            source_id=source_id,
                            original_url=url[:1024],
                            local_path=dup.local_path,
                            mime_type=dup.mime_type,
                            size_bytes=dup.size_bytes,
                            sha256=digest,
                        )
                    )
                    session.flush()
            except IntegrityError:
                logger.debug("Asset cache: alias INSERT race for %s; reusing", url)
        return dup

    target = _absolute_path_for(digest, ext)
    try:
        target.write_bytes(data)
    except OSError as exc:
        logger.warning("Asset cache: write failed for %s: %s", target, exc)
        return None

    row = AssetCache(
        source_id=source_id,
        original_url=url[:1024],
        local_path=str(target),
        mime_type=mime,
        size_bytes=len(data),
        sha256=digest,
    )
    # SAVEPOINT-guard the INSERT: under multi-worker/cross-source ingest a
    # concurrent flow may have inserted this original_url between our dedup
    # SELECT (L205) and here. On IntegrityError the nested rollback keeps the
    # outer transaction alive and we return the winning row. The bytes we wrote
    # to `target` are content-addressed (<sha256>.<ext>), identical to the
    # winner's path, so no orphan file results.
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        session.expunge(row)
        winner = (
            session.query(AssetCache)
            .filter(AssetCache.original_url == url[:1024])
            .first()
        )
        if winner is not None:
            logger.debug("Asset cache: fresh INSERT race for %s; using winner", url)
            return winner
        logger.warning("Asset cache: IntegrityError for %s but no winner found", url)
        return None
    return row


def _iter_cacheable_img_urls(raw_html: str):
    """Yield de-duped, http(s)-only ``<img src>`` URLs in document order,
    capped at ``_max_per_item()``. Single source of truth shared by the
    pre-download pass and the persist pass so both honour the identical
    filtering + cap (otherwise a URL downloaded up-front could be skipped at
    persist time, or vice-versa)."""
    if not raw_html:
        return
    cap = _max_per_item()
    seen: set[str] = set()
    yielded = 0
    for url in _IMG_SRC_RE.findall(raw_html):
        url = url.strip()
        if not url or url in seen:
            continue
        if not (url.startswith("http://") or url.startswith("https://")):
            continue  # data: / relative URLs not supported
        if _is_chrome_img(url):
            continue  # logo / nav icon / sprite / CTA / tracking pixel / tiny badge
        seen.add(url)
        yield url
        yielded += 1
        if yielded >= cap:
            return


def prefetch_image_bytes(raw_html: str) -> dict:
    """Download every cacheable ``<img>`` in ``raw_html`` and return a
    ``{url: bytes | None}`` map — performed with **no DB session**.

    The scheduler calls this *before* opening its persist transaction so the
    actual byte downloads (the slow, network-bound part) never run while a
    SQLite read-snapshot / write-lock is held. The returned map is then handed
    to ``enrich_markdown_with_images(prefetched=...)`` which reuses the bytes
    instead of downloading again. Returns ``{}`` when the cache is disabled or
    there are no images. Never raises — a failed download is recorded as
    ``None`` for that URL (treated as a miss downstream).
    """
    if not raw_html or not is_enabled():
        return {}
    limit = _max_bytes()
    timeout_s = _timeout_seconds()
    out: dict = {}
    for url in _iter_cacheable_img_urls(raw_html):
        if url in out:
            continue
        try:
            out[url] = _download(url, limit, timeout_s)
        except Exception:  # noqa: BLE001 — defensive: never let prefetch break the poll
            logger.debug("Asset cache: prefetch failed for %s", url, exc_info=True)
            out[url] = None
    return out


def cache_images_for_html(
    *,
    raw_html: str,
    source_id: Optional[int],
    session: Session,
    prefetched: Optional[dict] = None,
) -> List[Tuple[str, AssetCache]]:
    """Extract every ``<img src=...>``, cache each. Returns ``[(original_url, row), ...]``.

    Hits the per-item cap. Order preserved. ``prefetched`` (optional) carries
    bytes already downloaded by ``prefetch_image_bytes`` so this pass — which
    runs inside the caller's DB transaction — performs zero network I/O.
    """
    if not raw_html or not is_enabled():
        return []
    out: List[Tuple[str, AssetCache]] = []
    for url in _iter_cacheable_img_urls(raw_html):
        row = _cache_one(
            url=url, source_id=source_id, session=session, prefetched=prefetched,
        )
        if row is None:
            continue
        out.append((url, row))
    return out


def enrich_markdown_with_images(
    *,
    body_markdown: str,
    raw_html: str,
    source_id: Optional[int],
    session: Session,
    prefetched: Optional[dict] = None,
) -> Tuple[str, List[int]]:
    """Cache every inline image in ``raw_html`` and append markdown references
    pointing at ``/api/assets/<hash>`` to ``body_markdown``.

    Returns ``(enriched_body, [asset_id, ...])``. When the cache is disabled
    or no images are found, the body is returned unchanged with an empty list.
    ``prefetched`` (optional) supplies bytes already downloaded outside the DB
    transaction (see ``prefetch_image_bytes``).
    """
    pairs = cache_images_for_html(
        raw_html=raw_html, source_id=source_id, session=session, prefetched=prefetched,
    )
    if not pairs:
        return body_markdown, []

    parts: List[str] = []
    for original_url, row in pairs:
        if not row.sha256:
            continue
        parts.append(f"![image]({serve_url_for(row.sha256)})")
    valid_ids = [int(r.id) for _, r in pairs if r.sha256]
    if not parts:
        return body_markdown, []
    sep = "\n\n" if body_markdown.strip() else ""
    block = "\n\n".join(parts)
    # The scheduler caps KnowledgeNode.content at KB_CONTENT_MAX_CHARS AFTER this
    # append. A full-text body already at the cap would push the image refs
    # off the end, silently losing every image — trim the BODY instead so the
    # refs always survive.
    limit = KB_CONTENT_MAX_CHARS
    if len(body_markdown) + len(sep) + len(block) > limit:
        keep = max(0, limit - len(block) - len(sep))
        body_markdown = body_markdown[:keep]
    return body_markdown + sep + block, valid_ids


def serve_url_for(hash_hex: str) -> str:
    """Public path the FastAPI endpoint serves. Centralised so any future
    base-prefix change (e.g. CDN) is a single edit."""
    return f"/api/assets/{hash_hex}"


def resolve_serve_path(hash_hex: str, session: Session) -> Optional[Tuple[Path, str]]:
    """Endpoint-side lookup. Returns ``(realpath, mime)`` or None on miss.

    Refuses to serve any path that escapes ``ASSETS_DIR`` (defense in depth
    against a poisoned DB row pointing at e.g. ``/etc/passwd``).
    """
    if not re.fullmatch(r"[0-9a-f]{64}", hash_hex or ""):
        return None
    row = session.query(AssetCache).filter(AssetCache.sha256 == hash_hex).first()
    if row is None or not row.local_path:
        return None
    try:
        real = Path(row.local_path).resolve()
    except (OSError, RuntimeError):
        return None
    assets_real = ASSETS_DIR.resolve()
    try:
        real.relative_to(assets_real)
    except ValueError:
        logger.warning("Asset cache: refusing path outside ASSETS_DIR: %s", real)
        return None
    if not real.is_file():
        return None
    return real, row.mime_type or "application/octet-stream"


def attach_asset_node_ids(asset_ids: Iterable[int], node_id: int, session: Session) -> None:
    """After a KnowledgeNode is flushed, link the cached assets back to it.

    No-op if asset_ids is empty.
    """
    ids = [int(x) for x in asset_ids]
    if not ids:
        return
    (
        session.query(AssetCache)
        .filter(AssetCache.id.in_(ids))
        .update({"node_id": int(node_id)}, synchronize_session=False)
    )


__all__ = [
    "ASSETS_DIR",
    "is_enabled",
    "cache_images_for_html",
    "prefetch_image_bytes",
    "enrich_markdown_with_images",
    "serve_url_for",
    "resolve_serve_path",
    "attach_asset_node_ids",
]
