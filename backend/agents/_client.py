"""Multi-provider LLM client.

Supports three backends behind one identical `call_messages()` interface so the
intake / researcher / coder / critic agents never need to know which provider
is active:

    LLM_PROVIDER=anthropic     (default)  -> Anthropic Python SDK
    LLM_PROVIDER=openrouter               -> OpenRouter (OpenAI-compatible) via httpx
    LLM_PROVIDER=minimax                  -> MiniMax international (api.minimax.io,
                                             OpenAI-compatible) via httpx

Environment matrix
------------------
Anthropic provider:
    ANTHROPIC_API_KEY     required
    ANTHROPIC_MODEL       optional, defaults to "claude-3-5-sonnet-latest"

OpenRouter provider:
    OPENROUTER_API_KEY        required
    OPENROUTER_MODEL          optional, defaults to "anthropic/claude-sonnet-4.6"
    OPENROUTER_BASE_URL       optional, defaults to "https://openrouter.ai/api/v1"
    OPENROUTER_HTTP_REFERER   optional, sent as HTTP-Referer for OpenRouter analytics
    OPENROUTER_X_TITLE        optional, sent as X-Title (defaults to project name)

MiniMax provider (direct, international platform — NOT via OpenRouter, uses its
own key so OPENROUTER_API_KEY is never read):
    MINIMAX_API_KEY           required
    MINIMAX_MODEL             optional, defaults to "MiniMax-M3"
    MINIMAX_BASE_URL          optional, defaults to "https://api.minimax.io/v1"

The OpenRouter provider passes `response_format={"type":"json_object"}` straight
through (most modern Anthropic/OpenAI/Google models respect it). The Anthropic
provider doesn't expose that param natively, so we translate it into the
canonical assistant-prefill `{` technique inside this module.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("alpha.llm")

# ---------------------------------------------------------------------------
# Public exports (preserved for backward compatibility)
# ---------------------------------------------------------------------------

# P15/D-L2 — module-level DEFAULT_MODEL is kept for back-compat with code that
# imports it directly. Prefer get_provider().describe()["model"] for the
# RUNTIME value, which respects LLM_PROVIDER + per-call overrides.
DEFAULT_MODEL: str = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")


class LLMProviderError(RuntimeError):
    """Raised when a provider is mis-configured or returns a transport error."""


# ---------------------------------------------------------------------------
# Helpers shared by every provider
# ---------------------------------------------------------------------------

# P15/D-H1 — placeholder filter now lives in backend._envloader.is_real_secret
# so the prefix list ("your_*", "xxxx*", "placeholder*", "changeme*") has a
# single source of truth across _client.py / telegram_notifier.py /
# telegram_inbound.py / discord_inbound.py. The local alias preserves the
# legacy _is_real_key call sites unchanged.
from backend._envloader import is_real_secret as _is_real_key


# ---------------------------------------------------------------------------
# P29-T5 retry helpers — exponential backoff + jitter on transient upstream
# failures (429, 5xx, 408, 529). Public exceptions unchanged.
# ---------------------------------------------------------------------------

import random as _retry_random
import time as _retry_time

_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})


def _retryable_status(code: int) -> bool:
    try:
        return int(code) in _RETRYABLE_STATUS
    except (TypeError, ValueError):
        return False


# P30-I2: server hints up to 5 minutes are honoured verbatim. The previous
# 60s cap forced premature retries against providers (Anthropic, OpenRouter)
# that emit 60-120s Retry-After during sustained 429 storms, guaranteeing
# the next attempt would re-hit the same rate limit.
_RETRY_AFTER_MAX_SECS: float = 300.0


def _parse_retry_after(value: Any) -> Optional[float]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        secs = float(raw)
        return max(0.0, min(_RETRY_AFTER_MAX_SECS, secs))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime as _dt, timezone as _tz
        target = parsedate_to_datetime(raw)
        if target is None:
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=_tz.utc)
        delta = (target - _dt.now(_tz.utc)).total_seconds()
        return max(0.0, min(_RETRY_AFTER_MAX_SECS, delta))
    except Exception:  # noqa: BLE001
        return None


def _retry_call(fn, *, max_attempts: int = 3):
    """Run fn(); retry transient HTTP failures with exp backoff + jitter."""
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            status: Optional[int] = None
            retry_after_hdr: Any = None
            resp = getattr(exc, "response", None)
            if resp is not None:
                status = getattr(resp, "status_code", None)
                hdrs = getattr(resp, "headers", None)
                if hdrs is not None:
                    try:
                        retry_after_hdr = hdrs.get("retry-after")
                    except Exception:  # noqa: BLE001
                        retry_after_hdr = None
            if status is None:
                status = getattr(exc, "status_code", None)
            cls_name = type(exc).__name__
            # P31-I1: hard non-retryable auth/validation classes — short-circuit
            # so AuthenticationError/PermissionDeniedError aren't retried under
            # the generic "APIError" umbrella, and 4xx auth status codes don't
            # fall through to transient retry via status alone.
            non_retryable_cls = cls_name in {
                "AuthenticationError", "PermissionDeniedError",
                "BadRequestError", "NotFoundError", "UnprocessableEntityError",
            }
            transient_cls = cls_name in {
                # Anthropic SDK transient transport classes (it wraps httpx
                # network errors into these before they reach us).
                "APIConnectionError", "APITimeoutError", "RateLimitError",
                "InternalServerError", "APIError",
                # Raw httpx transport classes — raised DIRECTLY on the
                # OpenRouter path (no SDK wrapping). After a macOS screen-lock /
                # sleep-wake the pooled socket is dead, so the next request
                # raises one of these; without listing them here they fell
                # through as "non-retryable" and the whole agent call
                # hard-failed ("can't reach OpenRouter"). Every one is a
                # transient network condition that a fresh-connection retry with
                # backoff heals. NOTE: LocalProtocolError is deliberately absent
                # — it signals a client-side bug, not a network blip.
                "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
                "PoolTimeout", "ReadError", "WriteError", "NetworkError",
                "RemoteProtocolError", "ProtocolError", "TimeoutException",
                "ConnectionError", "ConnectionResetError",
            }
            non_retryable_status = status in (400, 401, 403, 404, 422)
            is_retryable = (
                not non_retryable_cls
                and not non_retryable_status
                and (
                    (status is not None and _retryable_status(status))
                    or transient_cls
                )
            )
            if not is_retryable or attempt >= (max_attempts - 1):
                raise
            sleep_s: Optional[float] = _parse_retry_after(retry_after_hdr)
            if sleep_s is None:
                # P30-I7: AWS full-jitter (sleep = U(0, min(cap, 2^attempt))).
                # Previous `2^attempt + random()` produced a synchronised
                # ~1s window — concurrent agent threads re-collided each
                # retry and prolonged 429 storms.
                sleep_s = _retry_random.uniform(0.0, min(30.0, 2.0 ** attempt))
            logger.warning(
                "LLM transient failure (%s status=%s); retry %d/%d in %.2fs",
                cls_name, status, attempt + 1, max_attempts, sleep_s,
            )
            _retry_time.sleep(sleep_s)
            attempt += 1


# ---------------------------------------------------------------------------
# Vision helpers (P4)
# ---------------------------------------------------------------------------

# Model substrings known to accept image inputs. Conservative — anything not
# in this list is rejected at the API layer rather than silently ignored.
_VISION_SUBSTRINGS = (
    "claude-3-5-sonnet",
    "claude-3-7",
    "claude-sonnet-4",
    "claude-opus-4",
    "claude-haiku-4",
    "gpt-4o",
    "gpt-4.1",
    "gemini-1.5",
    "gemini-2",
    "llama-3.2-vision",
)


def _supports_vision(model_id: str) -> bool:
    """Hardcoded allowlist — cheaper than runtime introspection."""
    m = (model_id or "").lower()
    return any(s in m for s in _VISION_SUBSTRINGS)


def _read_image_b64(path: str | Path) -> tuple[str, str]:
    """Return (base64 string, mime type). Mime is inferred from extension."""
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise LLMProviderError(f"Image not found: {path}")
    if p.stat().st_size > 8 * 1024 * 1024:
        raise LLMProviderError(
            f"Image too large ({p.stat().st_size} bytes > 8 MB cap): {path}"
        )
    ext = p.suffix.lower().lstrip(".")
    mime_map = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
    }
    mime = mime_map.get(ext, "image/png")
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return data, mime


# ---------------------------------------------------------------------------
# Real-usage capture (OpenRouter usage.include → billed cost + token counts)
# ---------------------------------------------------------------------------
# get_provider() returns a process-wide singleton shared across worker threads,
# so per-call usage cannot live on the provider instance. A thread-local carries
# it from provider.call() back to call_messages() within the SAME synchronous
# call, with no cross-thread races.
import threading as _threading

_USAGE_TLS = _threading.local()


def _stash_usage(usage: Optional[Dict[str, Any]]) -> None:
    _USAGE_TLS.value = usage


def _pop_usage() -> Optional[Dict[str, Any]]:
    u = getattr(_USAGE_TLS, "value", None)
    _USAGE_TLS.value = None
    return u


def _openrouter_usage_accounting() -> bool:
    """Whether to ask OpenRouter for usage accounting (real billed cost + token
    counts) via ``usage.include`` in the request body. Default ON; the kill-switch
    OPENROUTER_USAGE_ACCOUNTING=0 reverts the ledger to the char-based estimate."""
    from backend._envloader import env_bool
    return env_bool("OPENROUTER_USAGE_ACCOUNTING", True)


def _prompt_cache_enabled() -> bool:
    """P-CACHE — mark the (stable) system prompt as an Anthropic prompt-cache
    breakpoint so repeated agent calls within the ~5-min TTL re-read it at ~0.1×
    input cost instead of paying full tokens every time. The factory re-uses the
    same per-agent system prompt across every strategy, every critic-revise retry,
    and every evolution child, so the hit rate is high. Default ON; below a model's
    minimum cacheable size the API silently no-ops, so it is always safe to set.
    Kill-switch LLM_PROMPT_CACHE=0 restores the old uncached system-string path."""
    from backend._envloader import env_bool
    return env_bool("LLM_PROMPT_CACHE", True)


def _system_param_for(system: str, model: str, *, require_claude: bool) -> Any:
    """Shape the request's ``system`` field. Returns a single-breakpoint cached
    multipart block when prompt caching is on AND the prompt is non-empty (and,
    for OpenRouter where the model may be non-Anthropic, the model is a Claude
    model that honors ``cache_control``). Otherwise returns the plain string —
    byte-identical to the legacy path. Pure + key-free so the shaping is unit
    testable without constructing a provider."""
    if not (_prompt_cache_enabled() and system):
        return system
    if require_claude:
        mid = (model or "").lower()
        if not ("claude" in mid or mid.startswith("anthropic/")):
            return system
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def _parse_openrouter_usage(usage: Any) -> Optional[Dict[str, Any]]:
    """Extract the real billed cost + token counts from an OpenRouter ``usage``
    block (present when the request set usage.include). ``cost`` is OpenRouter's
    actual USD charge for the call. Returns None when absent / unparseable."""
    if not isinstance(usage, dict):
        return None

    def _num(v, cast):
        try:
            return cast(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    out = {
        "cost": _num(usage.get("cost"), float),
        "prompt_tokens": _num(usage.get("prompt_tokens"), int),
        "completion_tokens": _num(usage.get("completion_tokens"), int),
    }
    return out if any(v is not None for v in out.values()) else None


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------

class LLMProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def call(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        model: Optional[str],
        response_format: Optional[Dict[str, str]],
        images: Optional[List[str]] = None,
    ) -> str:
        ...

    # Convenience accessor used by /api/health and the test harness.
    @abstractmethod
    def describe(self) -> Dict[str, Any]:
        ...


# ---------------------------------------------------------------------------
# Anthropic native provider
# ---------------------------------------------------------------------------

class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self) -> None:
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover
            raise LLMProviderError(
                "The `anthropic` package is required for LLM_PROVIDER=anthropic. "
                "Install via `pip install -r requirements.txt`."
            ) from exc

        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not _is_real_key(api_key):
            raise LLMProviderError(
                "ANTHROPIC_API_KEY is not set (or is a placeholder). "
                "Either export ANTHROPIC_API_KEY, or set LLM_PROVIDER=openrouter "
                "and supply OPENROUTER_API_KEY."
            )
        self._client = Anthropic(api_key=api_key)
        self._default_model = (
            os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest").strip()
            or "claude-3-5-sonnet-latest"
        )

    def call(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        model: Optional[str],
        response_format: Optional[Dict[str, str]],
        images: Optional[List[str]] = None,
    ) -> str:
        effective_model = (model or self._default_model).strip()
        if images and not _supports_vision(effective_model):
            raise LLMProviderError(
                f"Model {effective_model!r} does not support image input."
            )

        # Build user content. If images are present, switch to the list-of-parts
        # format with one text block + N image blocks.
        if images:
            content_blocks: List[Dict[str, Any]] = [{"type": "text", "text": user}]
            for path in images:
                b64, mime = _read_image_b64(path)
                content_blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": b64},
                })
            messages: List[Dict[str, Any]] = [{"role": "user", "content": content_blocks}]
        else:
            messages = [{"role": "user", "content": user}]

        json_mode = bool(response_format and response_format.get("type") == "json_object")
        if json_mode:
            # Force the model to continue from an opening brace.
            messages.append({"role": "assistant", "content": "{"})

        # P25 — explicit timeout. The Anthropic SDK's default request timeout
        # is 10 minutes (600s) per the v0.7+ httpx client, which is far too
        # long for the agent pipeline: a stalled critic call would block the
        # scheduler thread for ten minutes. 120s matches the OpenRouter read
        # budget at line 276 and is enough for the slowest legitimate critic
        # response in production (multi-image vision calls). Network errors
        # raised by the underlying httpx client surface as anthropic.*Error
        # subclasses which the existing callers already catch.
        # P-CACHE — mark the system prompt as a cache breakpoint so the SDK
        # re-reads it from cache on repeat calls. Native Anthropic path is always
        # a Claude model, so no model gate. Below the model's min cacheable size
        # the API silently ignores the marker; empty system stays a plain string.
        system_param = _system_param_for(system, effective_model, require_claude=False)
        msg = _retry_call(
            lambda: self._client.messages.create(
                model=effective_model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_param,
                messages=messages,
                timeout=120.0,
            ),
            max_attempts=3,
        )
        parts: List[str] = []
        for block in msg.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        raw = "".join(parts).strip()
        if json_mode:
            # The assistant-prefill seed is "{". Only re-prepend it when the
            # model did NOT echo the opening brace itself; some Claude
            # responses (esp. temperature>0) re-emit the seed token, which
            # would otherwise produce a malformed "{{...}" string.
            if not raw.lstrip().startswith("{"):
                raw = "{" + raw
        return raw

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "model": self._default_model,
            "key_present": True,
            "supports_vision": _supports_vision(self._default_model),
        }


# ---------------------------------------------------------------------------
# OpenRouter (OpenAI-compatible) provider via httpx
# ---------------------------------------------------------------------------

# P-MINIMAX compat — retry budget for transient model-side hiccups (see
# OpenRouterProvider.call). MiniMax (and some other reasoning models) sometimes
# return an HTTP 200 carrying a transient "midstream / chat content is empty"
# error payload, or a 200 with empty content. Re-issuing the request a couple of
# times clears it; healthy responses return on the first pass.
_OPENROUTER_MODEL_RETRIES = 3
_OPENROUTER_RETRY_BACKOFF = 0.8  # base seconds; multiplied by the attempt index


def _openrouter_transient_payload(data: Any) -> Optional[str]:
    """Return a short reason string when `data` (a parsed OpenRouter response
    body) carries a TRANSIENT model-side error worth retrying — e.g. MiniMax's
    intermittent 'midstream' / 'chat content is empty', or provider overload /
    timeout. Returns None for healthy responses AND for permanent errors
    (model-not-found, auth, invalid request) so those still fail fast."""
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if not err:
        return None
    msg = str(err.get("message") or err) if isinstance(err, dict) else str(err)
    low = msg.lower()
    markers = (
        "midstream", "content is empty", "empty content",
        "timeout", "timed out", "overloaded", "temporarily",
        "try again", "please retry", "internal error", "rate limit",
        "server is busy", "service unavailable",
    )
    return msg[:200] if any(m in low for m in markers) else None


# P-MINIMAX reasoning hygiene — MiniMax-M3 (and other reasoning models) spend
# completion tokens on hidden reasoning. Two failure modes are defended here:
#   1. Truncation — a small ``max_tokens`` is eaten by reasoning, leaving the
#      visible answer empty (``finish_reason='length'``, the coder.failed error).
#      So the request runs UNCAPPED by default (``OPENROUTER_MAX_TOKENS=0`` →
#      max_tokens omitted) and the answer always has room.
#   2. Leakage — reasoning is still GENERATED and returned (in the separate
#      ``message.reasoning`` field, which the extractor never reads); we do NOT
#      ask the provider to suppress it, because on MiniMax suppression can blank
#      the answer and trip the "empty content" guard (a false "no response").
#      The only way reasoning reaches ``content`` is an inline ``<think>…</think>``
#      block, which is stripped client-side so only the final answer reaches the
#      agents — the model keeps reasoning fully; we just don't treat it as output.
_REASONING_BLOCK_RE = re.compile(
    r"<\s*(think|thinking|reasoning|reason)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_REASONING_CLOSE_RE = re.compile(
    r"<\s*/\s*(?:think|thinking|reasoning|reason)\s*>", re.IGNORECASE
)
_REASONING_OPEN_RE = re.compile(
    r"<\s*(?:think|thinking|reasoning|reason)\b[^>]*>", re.IGNORECASE
)


def _strip_reasoning(text: str) -> str:
    """Remove inline chain-of-thought so only the model's final answer remains.

    Handles three shapes seen from OpenRouter reasoning models: a well-formed
    ``<think>…</think>`` block (removed); reasoning followed by a bare closing
    tag then the answer, with no opening tag (keep only the tail after the last
    close); and a stray opening tag with no close (drop the tag). Text with no
    reasoning markup is returned unchanged.
    """
    if not text or "<" not in text:
        return text
    cleaned = _REASONING_BLOCK_RE.sub("", text)
    closes = list(_REASONING_CLOSE_RE.finditer(cleaned))
    if closes:
        cleaned = cleaned[closes[-1].end():]
    cleaned = _REASONING_OPEN_RE.sub("", cleaned)
    return cleaned.strip()


def _openrouter_max_tokens() -> Optional[int]:
    """Output-token ceiling for OpenRouter calls. Default 0 → omit ``max_tokens``
    entirely (uncapped) so a reasoning model's hidden tokens never starve the
    visible answer. Set ``OPENROUTER_MAX_TOKENS`` > 0 to re-impose a cost ceiling
    (it must then be large enough to cover reasoning + the answer)."""
    try:
        v = int(str(os.environ.get("OPENROUTER_MAX_TOKENS", "0")).strip() or "0")
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


class OpenRouterProvider(LLMProvider):
    name = "openrouter"
    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
    DEFAULT_MODEL = "anthropic/claude-sonnet-4.6"
    # P-MINIMAX-DIRECT — env-var names + SSRF host allowlist are class attributes
    # so a direct OpenAI-compatible vendor (MiniMaxProvider) can retarget them
    # without copying __init__/call. OpenRouter's own values are unchanged.
    API_KEY_ENV = "OPENROUTER_API_KEY"
    MODEL_ENV = "OPENROUTER_MODEL"
    BASE_URL_ENV = "OPENROUTER_BASE_URL"
    ALLOWED_HOSTS = ("openrouter.ai",)

    def __init__(self) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise LLMProviderError(
                "httpx is required for LLM_PROVIDER=openrouter. "
                "Install via `pip install -r requirements.txt`."
            ) from exc

        api_key = os.environ.get(self.API_KEY_ENV, "").strip()
        if not _is_real_key(api_key):
            raise LLMProviderError(
                f"{self.API_KEY_ENV} is not set (or is a placeholder). "
                f"Either export {self.API_KEY_ENV}, or point LLM_PROVIDER at a "
                f"provider whose key you have set."
            )
        self._api_key = api_key
        self._default_model = (
            os.environ.get(self.MODEL_ENV, self.DEFAULT_MODEL).strip()
            or self.DEFAULT_MODEL
        )
        self._base_url = os.environ.get(
            self.BASE_URL_ENV, self.DEFAULT_BASE_URL,
        ).strip().rstrip("/") or self.DEFAULT_BASE_URL
        # SSRF guard: only allow https and a known-safe host suffix.
        _parsed_base = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(self._base_url)
        _allowed_hosts = set(self.ALLOWED_HOSTS)
        if _parsed_base.scheme != "https" or not any(
            _parsed_base.netloc == h or _parsed_base.netloc.endswith("." + h)
            for h in _allowed_hosts
        ):
            raise LLMProviderError(
                f"{self.BASE_URL_ENV} must use https and point to one of "
                f"{sorted(_allowed_hosts)}, got: {self._base_url!r}"
            )
        self._referer = os.environ.get("OPENROUTER_HTTP_REFERER", "").strip()
        self._x_title = (
            os.environ.get("OPENROUTER_X_TITLE", "Agentic Alpha Research System").strip()
            or "Agentic Alpha Research System"
        )
        # Lazy import already done above; keep handles for runtime use.
        self._httpx_mod = httpx
        # 120s read budget covers slow critic responses; 15s connect timeout
        # surfaces network issues fast on bad routes. Explicit pool limits with
        # a bounded keepalive so idle sockets are recycled quickly: after a
        # macOS screen-lock / sleep-wake any pooled connection is dead, and a
        # short keepalive_expiry means the NEXT call almost always dials a fresh
        # connection instead of handing out a zombie socket. The transport-error
        # retry in _retry_call (see the httpx classes in its transient set) is
        # the ultimate guard for the narrow window where a sleep lands mid-burst
        # and a stale socket is still reused before it expires.
        self._client = httpx.Client(
            timeout=httpx.Timeout(120.0, connect=15.0),
            follow_redirects=False,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=30.0,
            ),
        )

    def _headers(self) -> Dict[str, str]:
        h = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Title": self._x_title,
        }
        if self._referer:
            h["HTTP-Referer"] = self._referer
        return h

    def _wants_usage_accounting(self) -> bool:
        """Whether to add OpenRouter's ``usage.include`` body extension. A direct
        OpenAI-compatible vendor (MiniMaxProvider) overrides this to False — it
        returns a ``usage`` block by default and may reject the unknown field."""
        return _openrouter_usage_accounting()

    @staticmethod
    def _extract_content(message_block: Dict[str, Any]) -> str:
        """Tolerate both string and list-of-parts content formats."""
        content = message_block.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, dict):
                    txt = item.get("text") or item.get("content") or ""
                    if isinstance(txt, str):
                        parts.append(txt)
                elif isinstance(item, str):
                    parts.append(item)
            return "".join(parts)
        return ""

    def call(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        model: Optional[str],
        response_format: Optional[Dict[str, str]],
        images: Optional[List[str]] = None,
    ) -> str:
        effective_model = (model or self._default_model).strip()
        if images and not _supports_vision(effective_model):
            raise LLMProviderError(
                f"Model {effective_model!r} does not support image input."
            )

        # Build user message in OpenAI chat-completions multi-modal format
        # when images are attached.
        if images:
            content_blocks: List[Dict[str, Any]] = [{"type": "text", "text": user}]
            for path in images:
                b64, mime = _read_image_b64(path)
                content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                })
            user_msg = {"role": "user", "content": content_blocks}
        else:
            user_msg = {"role": "user", "content": user}

        # P-CACHE — OpenRouter honors Anthropic cache_control breakpoints for
        # Claude models when the system content is multipart. Mark it cacheable
        # for those; any other model keeps the plain string (byte-identical
        # legacy) so a provider that dislikes multipart system content is never
        # surprised.
        system_content = _system_param_for(system, effective_model, require_claude=True)
        body: Dict[str, Any] = {
            "model": effective_model,
            "messages": [
                {"role": "system", "content": system_content},
                user_msg,
            ],
            "temperature": temperature,
        }
        # P-MINIMAX: omit max_tokens by default so a reasoning model's hidden
        # tokens never truncate the visible answer to empty (the coder.failed
        # finish_reason='length' case). OPENROUTER_MAX_TOKENS>0 re-imposes a
        # ceiling; the per-agent `max_tokens` arg still bounds the Anthropic path.
        _cap = _openrouter_max_tokens()
        if _cap is not None:
            body["max_tokens"] = _cap
        if response_format:
            body["response_format"] = response_format
        # T-FINOPS — ask OpenRouter to return real usage accounting (billed USD
        # cost + token counts) so the cost ledger records actuals, not a char
        # estimate. usage.include is an OpenRouter-level field (not forwarded to
        # the model), safe across models; kill-switch OPENROUTER_USAGE_ACCOUNTING=0.
        if self._wants_usage_accounting():
            body["usage"] = {"include": True}

        url = f"{self._base_url}/chat/completions"

        def _do_post():
            r = self._client.post(url, headers=self._headers(), json=body)
            if _retryable_status(r.status_code):
                err = self._httpx_mod.HTTPStatusError(
                    f"OpenRouter HTTP {r.status_code}", request=r.request, response=r,
                )
                err.status_code = r.status_code  # type: ignore[attr-defined]
                raise err
            return r

        # P-MINIMAX compat — some models (notably MiniMax) intermittently return
        # an HTTP 200 carrying a transient "midstream / chat content is empty"
        # error payload, or a 200 with empty assistant content. These are flaky
        # generation hiccups, not permanent failures, so re-issue the request a
        # few times (with backoff) before giving up. Permanent errors
        # (model-not-found, auth, invalid request) are NOT looped — they raise on
        # the first response. Healthy responses return on the first pass, so this
        # is a behavioural no-op for every well-behaved model/call.
        last_transient: Optional[str] = None
        for _attempt in range(_OPENROUTER_MODEL_RETRIES):
            try:
                resp = _retry_call(_do_post, max_attempts=3)
            except self._httpx_mod.HTTPError as exc:
                r_after = getattr(exc, "response", None)
                if r_after is not None and getattr(r_after, "status_code", 0) >= 400:
                    raise LLMProviderError(
                        f"OpenRouter HTTP {r_after.status_code} from {url}: "
                        f"{r_after.text[:600]}"
                    ) from exc
                raise LLMProviderError(f"OpenRouter request failed: {exc}") from exc

            if resp.status_code >= 400:
                raise LLMProviderError(
                    f"OpenRouter HTTP {resp.status_code} from {url}: "
                    f"{resp.text[:600]}"
                )
            try:
                data = resp.json()
            except json.JSONDecodeError as exc:
                raise LLMProviderError(
                    f"OpenRouter returned non-JSON body ({resp.status_code}): "
                    f"{resp.text[:300]}"
                ) from exc

            # Transient model-side error payload (HTTP 200 + {"error": ...}) →
            # retry with backoff; only fail after exhausting the budget.
            transient = _openrouter_transient_payload(data)
            if transient is not None:
                last_transient = transient
                if _attempt < _OPENROUTER_MODEL_RETRIES - 1:
                    _retry_time.sleep(_OPENROUTER_RETRY_BACKOFF * (_attempt + 1))
                    continue
                raise LLMProviderError(
                    f"OpenRouter transient model error after "
                    f"{_OPENROUTER_MODEL_RETRIES} attempts: {transient}"
                )

            # Non-transient error payload → permanent, fail fast.
            if isinstance(data, dict) and data.get("error"):
                raise LLMProviderError(f"OpenRouter error payload: {data['error']}")

            choices = data.get("choices") or []
            if not choices:
                raise LLMProviderError(f"OpenRouter response had no choices: {data}")
            message_block = choices[0].get("message") or {}
            content = _strip_reasoning(self._extract_content(message_block)).strip()
            if not content:
                # Empty content with no error payload — also a transient hiccup
                # for reasoning models that occasionally emit only hidden
                # reasoning tokens. Retry; fail clearly if it persists.
                last_transient = (
                    f"empty content (finish_reason="
                    f"{choices[0].get('finish_reason')!r})"
                )
                if _attempt < _OPENROUTER_MODEL_RETRIES - 1:
                    _retry_time.sleep(_OPENROUTER_RETRY_BACKOFF * (_attempt + 1))
                    continue
                raise LLMProviderError(
                    f"OpenRouter returned empty content after "
                    f"{_OPENROUTER_MODEL_RETRIES} attempts (finish_reason="
                    f"{choices[0].get('finish_reason')!r})"
                )
            # T-FINOPS — capture the real billed usage for the cost ledger
            # (None when the provider omitted it ⇒ ledger falls back to estimate).
            _stash_usage(_parse_openrouter_usage(data.get("usage")))
            return content

        # Defensive — the final iteration always returns or raises, so this is
        # unreachable; kept so the method has a terminal statement.
        raise LLMProviderError(
            f"OpenRouter exhausted retries: {last_transient or 'unknown'}"
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "model": self._default_model,
            "base_url": self._base_url,
            "key_present": True,
            "supports_vision": _supports_vision(self._default_model),
        }


# ---------------------------------------------------------------------------
# MiniMax direct provider (international platform, OpenAI-compatible via httpx)
# ---------------------------------------------------------------------------

class MiniMaxProvider(OpenRouterProvider):
    """Direct MiniMax international API (https://api.minimax.io/v1).

    MiniMax's OpenAI-compatible chat-completions endpoint is the same shape the
    OpenRouter provider already speaks, and all the MiniMax resilience already
    lives in ``OpenRouterProvider.call`` (transient-payload retry, ``<think>``
    stripping, uncapped tokens). So this subclass only retargets the
    key/base/model/SSRF-host and trims OpenRouter-only request extras. It reads
    ``MINIMAX_API_KEY`` exclusively — ``OPENROUTER_API_KEY`` is never touched.
    """

    name = "minimax"
    DEFAULT_BASE_URL = "https://api.minimax.io/v1"
    DEFAULT_MODEL = "MiniMax-M3"
    API_KEY_ENV = "MINIMAX_API_KEY"
    MODEL_ENV = "MINIMAX_MODEL"
    BASE_URL_ENV = "MINIMAX_BASE_URL"
    ALLOWED_HOSTS = ("minimax.io",)

    def _headers(self) -> Dict[str, str]:
        # Standard OpenAI-compatible auth only — no OpenRouter analytics headers.
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _wants_usage_accounting(self) -> bool:
        # MiniMax is a strict server: it already returns a ``usage`` block and may
        # reject OpenRouter's ``usage.include`` extension. The inherited call()
        # still PARSES whatever usage block comes back, so token counts are
        # captured; only the OpenRouter-specific request flag is suppressed.
        return False


# ---------------------------------------------------------------------------
# Factory + back-compat wrappers
# ---------------------------------------------------------------------------

_PROVIDER: Optional[LLMProvider] = None
_PROVIDER_NAME_REQUESTED: Optional[str] = None
# P31-CC2: double-checked locking for the provider singleton so two
# concurrent first-request callers don't construct two SDK clients.
_PROVIDER_LOCK = __import__("threading").Lock()


def _resolve_provider_name() -> str:
    raw = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if raw in {"", "auto"}:
        # Auto-pick: prefer Anthropic if its key is set, else OpenRouter.
        if _is_real_key(os.environ.get("ANTHROPIC_API_KEY")):
            return "anthropic"
        if _is_real_key(os.environ.get("OPENROUTER_API_KEY")):
            return "openrouter"
        return "anthropic"  # fail loudly later with a clear message
    if raw in {"anthropic", "claude"}:
        return "anthropic"
    if raw in {"minimax", "minimaxi", "minimax-m3"}:
        return "minimax"
    if raw in {"openrouter", "openai_compatible", "openai-compatible", "openai"}:
        return "openrouter"
    raise LLMProviderError(
        f"Unknown LLM_PROVIDER={raw!r}. Use 'anthropic', 'openrouter', or 'minimax'."
    )


def get_provider() -> LLMProvider:
    """Return the configured singleton provider, building it on first use."""
    global _PROVIDER, _PROVIDER_NAME_REQUESTED
    requested = _resolve_provider_name()
    # P31-CC2: fast path is lock-free; only cold-start or provider-swap
    # paths take the lock so two callers can't race to construct two SDK
    # clients (which would leak the loser's httpx.Client).
    if _PROVIDER is not None and _PROVIDER_NAME_REQUESTED == requested:
        return _PROVIDER
    with _PROVIDER_LOCK:
        if _PROVIDER is not None and _PROVIDER_NAME_REQUESTED == requested:
            return _PROVIDER
        if requested == "anthropic":
            new_provider: LLMProvider = AnthropicProvider()
        elif requested == "minimax":
            new_provider = MiniMaxProvider()
        else:
            new_provider = OpenRouterProvider()
        # Close the old provider's connection pool before replacing it so that
        # a runtime LLM_PROVIDER swap (e.g. anthropic -> openrouter) does not
        # leak httpx / Anthropic SDK sockets.  Mirrors reset_provider_for_tests.
        if _PROVIDER is not None:
            _old_client = getattr(_PROVIDER, "_client", None)
            if _old_client is not None:
                try:
                    _old_client.close()
                except Exception:  # noqa: BLE001
                    logger.exception("get_provider: old client.close() failed (ignored)")
        _PROVIDER = new_provider
        _PROVIDER_NAME_REQUESTED = requested
        logger.info("LLM provider activated: %s", _PROVIDER.describe())
        return _PROVIDER


def reset_provider_for_tests() -> None:
    """Reset the singleton — useful in unit tests that swap env vars."""
    global _PROVIDER, _PROVIDER_NAME_REQUESTED
    # P32-D10 / MEM32-5 — close any underlying HTTP client first. Both the
    # Anthropic SDK and the OpenRouter httpx.Client hold sockets / connection
    # pools that would otherwise leak between tests until GC. Wrapped because
    # neither close() is part of a contract we control.
    if _PROVIDER is not None:
        client = getattr(_PROVIDER, "_client", None)
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                logger.exception("reset_provider_for_tests: client.close() failed (ignored)")
    _PROVIDER = None
    _PROVIDER_NAME_REQUESTED = None


def describe_provider_config() -> Dict[str, Any]:
    """Lightweight introspection used by /api/health.

    Reports the requested provider plus whether the relevant key is present,
    WITHOUT actually constructing the provider (so an unset key does not
    raise — useful for the frontend to render a friendly warning instead).
    """
    requested = (os.environ.get("LLM_PROVIDER") or "").strip().lower() or "auto"
    resolved: str
    try:
        resolved = _resolve_provider_name()
    except LLMProviderError as exc:
        return {
            "requested": requested,
            "resolved": None,
            "configured": False,
            "error": str(exc),
        }
    if resolved == "anthropic":
        model = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
        key_present = _is_real_key(os.environ.get("ANTHROPIC_API_KEY"))
        return {
            "requested": requested,
            "resolved": "anthropic",
            "model": model,
            "key_env_var": "ANTHROPIC_API_KEY",
            "key_present": key_present,
            "configured": key_present,
            "supports_vision": _supports_vision(model),
        }
    if resolved == "minimax":
        model = os.environ.get("MINIMAX_MODEL", MiniMaxProvider.DEFAULT_MODEL)
        base_url = os.environ.get("MINIMAX_BASE_URL", MiniMaxProvider.DEFAULT_BASE_URL)
        key_present = _is_real_key(os.environ.get("MINIMAX_API_KEY"))
        return {
            "requested": requested,
            "resolved": "minimax",
            "model": model,
            "base_url": base_url,
            "key_env_var": "MINIMAX_API_KEY",
            "key_present": key_present,
            "configured": key_present,
            "supports_vision": _supports_vision(model),
        }
    # openrouter
    model = os.environ.get("OPENROUTER_MODEL", OpenRouterProvider.DEFAULT_MODEL)
    base_url = os.environ.get("OPENROUTER_BASE_URL", OpenRouterProvider.DEFAULT_BASE_URL)
    key_present = _is_real_key(os.environ.get("OPENROUTER_API_KEY"))
    return {
        "requested": requested,
        "resolved": "openrouter",
        "model": model,
        "base_url": base_url,
        "key_env_var": "OPENROUTER_API_KEY",
        "key_present": key_present,
        "configured": key_present,
        "supports_vision": _supports_vision(model),
    }


# Back-compat: existing code imports `get_client()` expecting an Anthropic
# client object. Keep the function but only honour it when the active provider
# really is Anthropic — otherwise raise a clear error.
def get_client():
    prov = get_provider()
    if isinstance(prov, AnthropicProvider):
        return prov._client  # noqa: SLF001 - intentional pass-through
    raise LLMProviderError(
        f"get_client() is Anthropic-only; active provider is {prov.name}."
    )


def _model_for_agent(agent: str) -> Optional[str]:
    """P-MODELROUTE: optional per-agent model override via env
    ``LLM_MODEL_<AGENT>`` (e.g. ``LLM_MODEL_INTAKE``, ``LLM_MODEL_RESEARCHER``,
    ``LLM_MODEL_CODER``, ``LLM_MODEL_CRITIC``). Returns ``None`` when unset/blank
    so the provider's default model is used — i.e. existing behavior is unchanged
    until an operator opts in.

    Rule-based routing only (the user's research shows learned routers rarely beat
    simple per-task assignment): point cheap/mechanical agents (intake) at a small
    model and keep reasoning-heavy agents (critic) on the strong one. This reads
    only the model *name* env var — it never reads or affects the API key.
    """
    a = (agent or "").strip().upper()
    if not a:
        return None
    m = (os.environ.get(f"LLM_MODEL_{a}", "") or "").strip()
    return m or None


def call_messages(
    *,
    system: str,
    user: str,
    max_tokens: int = 2048,
    temperature: float = 0.2,
    model: Optional[str] = None,
    response_format: Optional[Dict[str, str]] = None,
    images: Optional[List[str]] = None,
    agent: str = "",
) -> str:
    """Provider-agnostic single-turn completion. Used by every agent.

    Pass ``images`` (a list of absolute file paths) to invoke the multi-modal
    path. Both providers will raise ``LLMProviderError`` if the active model
    is not in the vision allowlist (see ``_supports_vision``).

    P6: routes every call through the daily USD budget cap
    (``ALPHA_LLM_DAILY_USD_CAP``). When the cap is unset/zero the gate is a
    no-op; otherwise calls that would exceed the projected cap raise
    ``LLMBudgetExceededError`` (re-exported from this module). The optional
    ``agent`` label is recorded only inside the budget error message — it does
    not change provider behaviour.
    """
    # Lazy import keeps the budget module out of the cold-start path of unit
    # tests that don't touch LLMs (and prevents circular imports if llm_budget
    # ever wants to read provider metadata).
    from backend.core.llm_budget import (
        LLMBudgetExceededError as _BudgetExc,  # noqa: F401 — re-exported below
        reserve_budget,
        settle_reservation,
    )

    # P-MODELROUTE: honor an explicit model, else fall back to a per-agent env
    # override, else None (provider default). Additive — no env set ⇒ unchanged.
    effective_model = (model.strip() if (model and model.strip())
                       else _model_for_agent(agent))

    # T2-D — log the cascade decision (which model each stage resolved to and
    # why). Observability only; does not change routing. DEBUG so it never spams
    # production INFO logs.
    if logger.isEnabledFor(logging.DEBUG):
        _route_src = ("explicit" if (model and model.strip())
                      else (f"env:LLM_MODEL_{(agent or '').upper()}"
                            if _model_for_agent(agent) else "provider-default"))
        logger.debug("llm.route agent=%s resolved_model=%s source=%s",
                     agent or "-", effective_model or "<provider-default>", _route_src)

    prompt_chars = len(system or "") + len(user or "")
    # T-FINOPS — clear any stale per-thread usage so a non-OpenRouter (or failed)
    # call never inherits a previous call's billed-usage block.
    _stash_usage(None)
    # P-RACE: atomic check-and-reserve closes the TOCTOU window; the reservation
    # is settled (and freed) in the finally below even on provider failure.
    _budget_token = reserve_budget(prompt_chars, agent=agent)

    # P29-C11 / P30-I5: input-side budget accounting MUST happen on provider
    # call failures (timeouts, mid-request transport errors, 5xx). BUT
    # provider construction (key validation, SDK import) is pre-flight — a
    # missing-key LLMProviderError must NOT charge budget because no request
    # was sent. We still MUST release the reservation on get_provider() failure
    # to avoid leaking budget (the try/finally below is not entered if this raises).
    try:
        provider = get_provider()
    except Exception:
        # Release the reservation at zero cost — no request was sent.
        try:
            settle_reservation(_budget_token, 0, 0, agent=agent)
        except Exception:  # noqa: BLE001
            pass
        raise
    completion: str = ""
    completion_chars = 0
    call_attempted = False
    _start_mono = _retry_time.monotonic()
    _outcome = "error"
    try:
        call_attempted = True
        completion = provider.call(
            system=system,
            user=user,
            max_tokens=max_tokens,
            temperature=temperature,
            model=effective_model,
            response_format=response_format,
            images=images,
        )
        completion_chars = len(completion or "")
        _outcome = "ok"
    finally:
        _dur_ms = round((_retry_time.monotonic() - _start_mono) * 1000.0, 1)
        if call_attempted:
            try:
                settle_reservation(
                    _budget_token, prompt_chars, completion_chars, agent=agent
                )
            except Exception:  # noqa: BLE001
                import logging as _logging
                _logging.getLogger("alpha.llm_client").exception(
                    "settle_reservation bookkeeping failed (non-fatal)"
                )
            # P31-OBS3: single structured INFO line per LLM call — drives the
            # latency SLO board AND lets ops correlate burst-spend with slow
            # calls without enabling DEBUG logging.
            logger.info(
                "llm.call provider=%s agent=%s prompt_chars=%d "
                "completion_chars=%d dur_ms=%.1f outcome=%s images=%d",
                getattr(provider, "name", type(provider).__name__),
                agent or "-", prompt_chars,
                completion_chars, _dur_ms, _outcome,
                len(images) if images else 0,
            )
            # FinOps — append this settled call to the cost ledger. Best-effort,
            # never raises; only successful calls (mirrors the budget's 0-charge
            # on failure). strategy_id is read from the budget contextvar.
            if _outcome == "ok":
                try:
                    from backend.core import cost_ledger
                    from backend.core.llm_budget import current_strategy_id
                    cost_ledger.record_call(
                        agent=agent, model=(effective_model or "<default>"),
                        input_chars=prompt_chars, output_chars=completion_chars,
                        usage=_pop_usage(),  # real OpenRouter usage.cost when present
                        strategy_id=current_strategy_id(),
                    )
                except Exception:  # noqa: BLE001
                    pass
        else:
            # Provider construction failed before a byte was sent: no charge,
            # but the reservation MUST be released to avoid leaking budget.
            try:
                settle_reservation(_budget_token, 0, 0, agent=agent)
            except Exception:  # noqa: BLE001
                pass
    return completion


# Re-export so callers can `from backend.agents._client import LLMBudgetExceededError`
# without needing to know about the core.llm_budget module layout.
from backend.core.llm_budget import LLMBudgetExceededError  # noqa: E402


# ---------------------------------------------------------------------------
# JSON / code-block extraction utilities (provider-independent)
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Dict[str, Any]:
    """Pull a JSON object out of a raw LLM response.

    Order of attempts:
    1. Direct json.loads on the trimmed string.
    2. Strip the first ```...``` fence and parse.
    3. Greedy match the first {...} block and parse.
    Raises ValueError if all attempts fail.
    """
    if not text:
        raise ValueError("Empty response from model")

    raw = text.strip()

    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    fence = _FENCE_RE.search(raw)
    if fence:
        candidate = fence.group(1).strip()
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # A top-level JSON array (e.g. '[{"title":"x"}]') is NOT a JSON object; do
    # not let the greedy brace fallback mine an embedded {...} out of it, or the
    # documented "JSON object" contract would be violated. Reject it cleanly so
    # callers (intake/critic) retry instead of receiving an unintended dict.
    brace_match = None if raw.lstrip().startswith("[") else re.search(
        r"\{.*\}", raw, re.DOTALL
    )
    if brace_match:
        candidate = brace_match.group(0)
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            # Recovery: a doubled leading brace ("{{...}") from an echoed
            # assistant-prefill seed. Collapse one leading "{" and retry once.
            stripped = candidate.lstrip()
            if stripped.startswith("{{"):
                try:
                    obj = json.loads(stripped[1:])
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    pass
            raise ValueError(
                f"Found {{...}} block but failed to parse: {candidate[:200]}"
            )

    raise ValueError(f"No JSON object found in response: {raw[:200]}")


def extract_code_block(text: str, language: str = "python") -> str:
    """Pull a fenced code block (defaults to python) out of an LLM response.
    Falls back to returning the trimmed text if no fence is present.
    """
    if not text:
        return ""
    pattern = re.compile(
        rf"```(?:{re.escape(language)})?\s*(.*?)```",
        re.DOTALL | re.IGNORECASE,
    )
    m = pattern.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


__all__ = [
    "DEFAULT_MODEL",
    "LLMProvider",
    "LLMProviderError",
    "LLMBudgetExceededError",
    "AnthropicProvider",
    "OpenRouterProvider",
    "MiniMaxProvider",
    "get_provider",
    "get_client",
    "call_messages",
    "extract_json",
    "extract_code_block",
    "describe_provider_config",
    "reset_provider_for_tests",
]
