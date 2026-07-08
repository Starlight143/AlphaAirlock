"""Team B — Adversarial Risk Critic (P1 rewrite).

Receives the Alpha Story + factor code + backtest metrics and produces a
review that mirrors the reference YouTube system's strategy-detail view:

- Six "soul questions" the demo highlights (Q1-Q6)
- A Formula Quality Check section
- A Production Redundancy Assessment with severity tag
- An optional inline `Flag as <issue>` array for callouts
- Hard rejection thresholds that override the LLM verdict

The hard guardrails are unchanged from the P0 contract (Sharpe < 0.5, MaxDD
worse than -35%, < 20 trades, profit factor < 1.05) so existing tests and
orchestrator logic continue to work without modification.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List, Optional

from backend._envloader import env_bool, env_int
from backend.agents._client import (
    LLMBudgetExceededError,
    LLMProviderError,
    call_messages,
    extract_json,
)
from backend.core.thresholds import (
    CRITIC_MAX_DRAWDOWN,
    MIN_PROFIT_FACTOR,
    MIN_SHARPE,
    MIN_TRADES_BACKTEST,
)

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

# Thresholds are interpolated so the prompt and the runtime enforcement at the
# bottom of `review_strategy()` can never drift apart.
_HARD_FAIL_BLOCK = (
    f"- annualized_sharpe < {MIN_SHARPE}\n"
    f"- max_drawdown < {CRITIC_MAX_DRAWDOWN:.2f} "
    f"(worse than {int(round(CRITIC_MAX_DRAWDOWN * 100))}%)\n"
    f"- trades < {MIN_TRADES_BACKTEST} (insufficient sample)\n"
    f"- profit_factor < {MIN_PROFIT_FACTOR} (no real edge)"
)

CRITIC_SYSTEM_PROMPT = """You are Team B, the adversarial Risk Critic of an
Agentic Alpha Research System. Your job is to STRESS-TEST an Alpha hypothesis
and decide whether it should be deployed.

You receive:
- The Alpha Story (the thesis the Researcher produced).
- The factor code (sandbox-validated).
- The backtest metrics (Sharpe, MaxDD, win_rate, profit_factor, trades, etc.).

You MUST address every one of the six "soul questions" below in the markdown
critique, each as its own H3 (`###`) section, in this exact order and with the
exact headings shown:

### Q1 — Why does it work?
A 2-3 sentence economic / mechanical explanation of the edge. Reference the
specific market actors (HFTs, market makers, retail liquidation cascades,
options dealers, miners, etc.) whose behaviour generates the inefficiency.

### Q2 — What would kill it?
A 2-3 sentence description of the regime / structural change that destroys
the edge. Be concrete: which metric flipping which way?

### Q3 — Who is the counterparty?
A 1-2 sentence identification of who is on the other side of these trades and
why they are willing to keep losing. If unclear, say "Counterparty unclear —
flag for review".

### Q4 — Simple explanation?
A single sentence summary a non-quant could understand. Plain English.

### Q5 — Data availability?
A 2-3 sentence audit of the data this factor needs. Are the required columns
(see Required Columns from the story) present in the synthetic dataset? If
the story references a column not in the whitelist (open, high, low, close,
volume, open_interest, funding_rate, liquidations) you MUST also output a
`flags` entry with type `datasource_issue`.

### Q6 — Alpha decay speed?
A 2-3 sentence guess at how quickly arbitrageurs would compete this edge
away. Categorize as "fast (days)", "medium-term (weeks)", or "structural
(months+)".

After the six soul questions, append TWO more H3 sections:

### Formula Quality Check
Comment on the code quality (lookahead safety, NaN handling, edge cases). Use
short bullet points. Each bullet is a single observation.

### Production Redundancy Assessment
Estimate overlap risk vs. the typical existing alpha catalogue (funding-rate,
basis, momentum, liquidation, on-chain). Emit a severity_tag of exactly one
of: "NO OVERLAP", "LOW OVERLAP", "MODERATE OVERLAP", "HIGH OVERLAP",
"SEVERE OVERLAP". The severity should reflect how undifferentiated this alpha
is — anything labeled SEVERE OVERLAP is at best a redundant marginal addition.

# Hard rejection thresholds (any one triggers No-Go — these are enforced
# server-side regardless of your verdict, but mention any that are tripped):
""" + _HARD_FAIL_BLOCK + """

# Alpha Category
Classify the strategy into exactly one of these 8 categories (this populates
the Alpha Category doughnut on the Backtest Panel):
- Funding & Basis
- On Chain
- Options
- Sentiment
- Macro
- Liquidation
- Microstructure
- Other

Output: reply with EXACTLY ONE valid JSON object with this schema:
{
  "verdict": "Go" | "No-Go",
  "critique_markdown": string,        // all 8 H3 sections above, in order
  "violation_tags": string[],         // e.g. ["overfitting", "high_drawdown"]
  "severity_tag": string,             // one of the five overlap labels
  "alpha_category": string,           // one of the 8 categories
  "flags": [                          // optional; empty array if no issues
    { "type": string, "message": string }
  ],
  "confidence": number,               // 0.0 - 1.0
  "soul_questions": {                 // P8-FIX/H-4: structured Q1-Q6 answers
    "q1_why_works":          string,  // <= 240 chars each
    "q2_what_kills":         string,
    "q3_counterparty":       string,
    "q4_simple_explanation": string,
    "q5_data_availability":  string,
    "q6_alpha_decay":        string
  }
}
No prose outside the JSON. Do NOT wrap the JSON in markdown code fences.
The soul_questions block MUST be a one-sentence-each summary of the matching
H3 section above — keep each value under 240 characters."""


# ---------------------------------------------------------------------------
# Constants — single source of truth for severity / categories
# ---------------------------------------------------------------------------

SEVERITY_TAGS: List[str] = [
    "NO OVERLAP",
    "LOW OVERLAP",
    "MODERATE OVERLAP",
    "HIGH OVERLAP",
    "SEVERE OVERLAP",
]

ALPHA_CATEGORIES: List[str] = [
    "Funding & Basis",
    "On Chain",
    "Options",
    "Sentiment",
    "Macro",
    "Liquidation",
    "Microstructure",
    "Other",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_metrics(metrics: Dict[str, Any]) -> str:
    keys = [
        "annualized_sharpe", "annualized_return", "cumulative_return",
        "max_drawdown", "win_rate", "profit_factor",
        "mean_hourly_return", "std_hourly_return",
    ]
    return "\n".join(f"- {k}: {metrics.get(k)}" for k in keys)


# ---------------------------------------------------------------------------
# T1-A — Blind / anti-confirmation-bias critic pass
# ---------------------------------------------------------------------------
# The standard critic sees the persuasive alpha story alongside the metrics, so
# a well-written narrative can rescue a statistically weak strategy. The blind
# pass is a SEPARATE mechanical reviewer that sees ONLY code + raw metrics (no
# story). OBSERVE-by-default (records agreement; never changes the verdict);
# enforcement (blind No-Go vetoes a story-aware Go) is opt-in.

def _critic_blind_enabled() -> bool:
    return env_bool("CRITIC_BLIND_PASS", False)


def _critic_blind_enforced() -> bool:
    return env_bool("CRITIC_BLIND_ENFORCE", False)


def _critic_blind_max_tokens() -> int:
    return env_int("CRITIC_BLIND_MAX_TOKENS", 700, minimum=128, maximum=4000)


# Deliberately a SEPARATE whitelist from _format_metrics: the mechanical pass
# SHOULD see the honesty metrics (PSR/Sortino/skew/OOS) the story-aware prompt
# omits. Mutating _format_metrics instead would change the story-aware prompt
# and could shift existing verdicts — forbidden.
_BLIND_METRIC_KEYS = [
    "annualized_sharpe", "annualized_return", "cumulative_return",
    "max_drawdown", "win_rate", "profit_factor",
    "mean_hourly_return", "std_hourly_return", "num_trades", "trades",
    "probabilistic_sharpe_ratio", "sortino_ratio", "sharpe_autocorr_adjusted",
    "lag1_autocorrelation", "return_skewness", "return_kurtosis",
    "oos_annualized_sharpe", "oos_probabilistic_sharpe_ratio",
]


def _format_metrics_blind(metrics: Dict[str, Any]) -> str:
    return "\n".join(
        f"- {k}: {metrics.get(k)}" for k in _BLIND_METRIC_KEYS if k in metrics
    )


CRITIC_BLIND_SYSTEM_PROMPT = """You are a MECHANICAL strategy auditor. You receive ONLY a factor's code and
its raw backtest statistics — NO narrative, NO economic thesis. Judge purely on
the numbers and the code: is the measured edge statistically credible and not an
obvious overfit / look-ahead / over-trading artefact?

Do NOT speculate about economic rationale you were not given. Base the verdict
ONLY on: risk-adjusted return (Sharpe/Sortino), probabilistic Sharpe, drawdown,
profit factor, trade count/sample size, skew/kurtosis, and in-vs-out-of-sample
consistency.

Reply with EXACTLY this JSON object and nothing else:
{"blind_verdict": "Go" | "No-Go", "blind_confidence": <float 0..1>,
 "blind_reasons": ["short reason", ...]}"""


def _run_blind_pass(
    *, factor_code: str, full_metrics: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Mechanical, story-blind critic pass. Returns a normalized
    {blind_verdict, blind_confidence, blind_reasons} dict, or None on any
    provider/parse failure (so a broken blind pass NEVER spuriously rejects a
    strategy the story-aware pass approves). Re-raises LLMBudgetExceededError so
    a budget breach still surfaces on the (cheaper) blind call rather than being
    swallowed."""
    user = (
        "FACTOR CODE:\n```python\n"
        f"{factor_code.strip()}\n```\n\n"
        "BACKTEST METRICS:\n"
        f"{_format_metrics_blind(full_metrics)}\n"
    )
    try:
        raw = call_messages(
            system=CRITIC_BLIND_SYSTEM_PROMPT,
            user=user,
            max_tokens=_critic_blind_max_tokens(),
            temperature=0.0,
            response_format={"type": "json_object"},
            agent="critic",  # same budget bucket + model routing as the critic
        )
        parsed = extract_json(raw)
    except LLMBudgetExceededError:
        raise  # MUST propagate — never swallow a budget breach
    except (LLMProviderError, ValueError):
        return None

    bv = str(parsed.get("blind_verdict", "No-Go")).strip().lower()
    blind_verdict = "Go" if bv == "go" else "No-Go"
    try:
        bc = float(parsed.get("blind_confidence", 0.5))
    except (TypeError, ValueError):
        bc = 0.5
    if not math.isfinite(bc):
        bc = 0.5
    bc = max(0.0, min(1.0, bc))
    reasons = parsed.get("blind_reasons", []) or []
    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    reasons = [str(r) for r in reasons][:8]
    return {"blind_verdict": blind_verdict, "blind_confidence": bc, "blind_reasons": reasons}


def _normalize_severity(raw: Any) -> str:
    """Coerce an LLM-supplied severity string into the closest known tag."""
    s = str(raw or "").strip().upper()
    if not s:
        return "MODERATE OVERLAP"  # safe default — caller can see the field exists
    for tag in SEVERITY_TAGS:
        if tag == s:
            return tag
    # Fuzzy match by removing whitespace.
    compact = s.replace(" ", "")
    for tag in SEVERITY_TAGS:
        if tag.replace(" ", "") == compact:
            return tag
    return "MODERATE OVERLAP"


def _normalize_category(raw: Any) -> str:
    """Coerce LLM-supplied category to one of the 8 canonical strings."""
    s = str(raw or "").strip()
    if not s:
        return "Other"
    s_lower = s.lower()
    for cat in ALPHA_CATEGORIES:
        if cat.lower() == s_lower:
            return cat
    # Partial keyword matches for common renamings
    if "fund" in s_lower or "basis" in s_lower:
        return "Funding & Basis"
    if "chain" in s_lower or "onchain" in s_lower:
        return "On Chain"
    if "option" in s_lower or "gamma" in s_lower or "vega" in s_lower:
        return "Options"
    if "sentiment" in s_lower or "social" in s_lower:
        return "Sentiment"
    if "macro" in s_lower:
        return "Macro"
    if "liquid" in s_lower:
        return "Liquidation"
    if "micro" in s_lower or "order" in s_lower:
        return "Microstructure"
    return "Other"


def _coerce_flags(raw: Any) -> List[Dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            t = str(item.get("type", "")).strip()
            m = str(item.get("message", "")).strip()
            if t and m:
                out.append({"type": t, "message": m})
        elif isinstance(item, str) and item.strip():
            out.append({"type": "note", "message": item.strip()})
    return out


# ---------------------------------------------------------------------------
# P8-FIX/H-4 — Soul-questions extraction
# ---------------------------------------------------------------------------

SOUL_QUESTION_KEYS: List[str] = [
    "q1_why_works",
    "q2_what_kills",
    "q3_counterparty",
    "q4_simple_explanation",
    "q5_data_availability",
    "q6_alpha_decay",
]

# Capture each ``### Q{n} — ...`` H3 section's body. Matches any lines that
# do NOT start with ``###`` (tempered-greedy token), correctly handling empty
# bodies and blank separator lines without bleeding into the next H3 header.
# DOTALL is intentionally absent: ``.`` must NOT cross newlines here so the
# negative-lookahead ``(?!^###)`` is anchored per-line via MULTILINE.
_SOUL_QUESTION_RE = re.compile(
    r"^###\s+Q(\d)\s*[—\-:][^\n]*\n((?:(?!^###).*\n?)*)",
    re.MULTILINE,
)


def _truncate(s: Any, cap: int = 240) -> str:
    text = str(s or "").strip()
    if len(text) <= cap:
        return text
    return text[: cap - 1].rstrip() + "…"


def _coerce_soul_questions(raw: Any) -> Dict[str, str]:
    """Coerce LLM-supplied soul_questions dict to a fixed 6-key shape.

    Missing keys default to empty string. Every value is trimmed + capped at
    240 chars so the UI can render them in a compact card list.
    """
    out: Dict[str, str] = {k: "" for k in SOUL_QUESTION_KEYS}
    if isinstance(raw, dict):
        for k in SOUL_QUESTION_KEYS:
            out[k] = _truncate(raw.get(k), cap=240)
    return out


def _extract_soul_questions_from_markdown(markdown: str) -> Dict[str, str]:
    """Fallback parser used when the LLM emits ``critique_markdown`` but
    forgets the ``soul_questions`` JSON block. Walks the H3 sections and maps
    Q1..Q6 onto the canonical dict keys.
    """
    out: Dict[str, str] = {k: "" for k in SOUL_QUESTION_KEYS}
    if not markdown or "###" not in markdown:
        return out
    for match in _SOUL_QUESTION_RE.finditer(markdown):
        try:
            n = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if n < 1 or n > 6:
            continue
        body = match.group(2) or ""
        # Strip leading bullet markers / blank lines / surrounding whitespace.
        body = body.strip()
        # Collapse multi-line bodies into a single condensed paragraph for UI.
        body = re.sub(r"\s+", " ", body)
        key = SOUL_QUESTION_KEYS[n - 1]
        if not out[key]:
            out[key] = _truncate(body, cap=240)
    return out


# ---------------------------------------------------------------------------
# P23 — NaN-safe hard-gate metric comparison.
# ---------------------------------------------------------------------------

def _metric_hard_fail(
    metrics: Dict[str, Any], key: str, threshold: float
) -> bool:
    """Return True iff ``metrics[key]`` should trigger a hard-fail.

    The original D-H4 pattern ``float(metrics.get(key, 0.0) or 0.0) < threshold``
    silently passes ``NaN`` values because IEEE 754 mandates that **every**
    comparison with NaN evaluates to ``False`` (including ``<``). A NaN sharpe
    / drawdown / profit_factor means the upstream metric calculation failed
    — for example ``float(nr.mean())`` on an all-NaN return series in
    ``backend/core/engine.py`` — which must hard-fail the strategy rather
    than rubber-stamp it.

    This helper:
      * preserves the previous ``or 0.0`` coercion semantic so ``None`` and
        the literal ``0`` continue to behave exactly as before;
      * treats NaN as "below threshold" so failed metric calculations no
        longer leak past the critic's gates;
      * treats uncoercible values (``TypeError`` / ``ValueError`` from
        ``float()``) as hard-fail rather than crashing the orchestrator.
    """
    raw = metrics.get(key, 0.0)
    try:
        v = float(raw or 0.0)
    except (TypeError, ValueError):
        return True
    if math.isnan(v):
        return True
    return v < threshold


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def review_strategy(
    *,
    alpha_story: str,
    factor_code: str,
    backtest_metrics: Dict[str, Any],
    trades: int,
) -> Dict[str, Any]:
    """Run the Risk Critic.

    Returns a dict with:
      verdict          : "Go" | "No-Go"           (hard-fail enforced)
      critique_markdown: str                       (Q1-Q6 + Formula Quality + Production Redundancy)
      violation_tags   : List[str]
      severity_tag     : one of SEVERITY_TAGS
      alpha_category   : one of ALPHA_CATEGORIES
      flags            : List[{type,message}]
      confidence       : float 0..1
      metrics_snapshot : dict
    """
    full_metrics = dict(backtest_metrics)
    full_metrics["trades"] = trades

    # T1-A — run the story-blind mechanical pass FIRST (before the more
    # expensive story-aware call), so a per-strategy budget breach trips on the
    # cheaper call. None when disabled or on soft failure.
    blind = None
    if _critic_blind_enabled():
        blind = _run_blind_pass(factor_code=factor_code, full_metrics=full_metrics)

    user = (
        "ALPHA STORY:\n"
        f"{alpha_story.strip()}\n\n"
        "FACTOR CODE:\n```python\n"
        f"{factor_code.strip()}\n```\n\n"
        "BACKTEST METRICS:\n"
        f"{_format_metrics(full_metrics)}\n"
        f"- trades: {trades}\n"
    )

    raw = call_messages(
        system=CRITIC_SYSTEM_PROMPT,
        user=user,
        max_tokens=2400,
        temperature=0.25,
        response_format={"type": "json_object"},
        agent="critic",  # P13/D-L2 — per-agent budget attribution
    )
    parsed = extract_json(raw)

    verdict = str(parsed.get("verdict", "No-Go")).strip()
    if verdict.lower() not in {"go", "no-go", "nogo", "no go"}:
        verdict = "No-Go"
    verdict_norm = "Go" if verdict.lower() == "go" else "No-Go"

    critique = str(parsed.get("critique_markdown", "")).strip() or "(no critique provided)"

    violation_tags = parsed.get("violation_tags", []) or []
    if not isinstance(violation_tags, list):
        violation_tags = [str(violation_tags)]
    violation_tags = [str(t) for t in violation_tags]

    severity_tag = _normalize_severity(parsed.get("severity_tag"))
    alpha_category = _normalize_category(parsed.get("alpha_category"))
    flags = _coerce_flags(parsed.get("flags"))

    # P8-FIX/H-4: structured soul questions. Prefer the JSON block; fall back
    # to regex-parsing the H3 sections if the model omitted it. Always emit a
    # complete 6-key dict so the frontend doesn't need null-checks per key.
    soul_questions = _coerce_soul_questions(parsed.get("soul_questions"))
    if not any(soul_questions.values()):
        soul_questions = _extract_soul_questions_from_markdown(critique)

    try:
        confidence = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    # P31-CONF-NAN1: json.loads accepts bare NaN; float(nan) survives the try,
    # and max(0.0, min(1.0, nan)) returns 1.0 in CPython — silently promoting a
    # broken LLM value to MAX confidence. Coerce non-finite to the 0.5 default.
    if not math.isfinite(confidence):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    # Hard guardrails — these override the LLM verdict (thresholds sourced
    # from backend.core.thresholds so prompt + runtime never drift).
    hard_fail_reasons: List[str] = []
    # D-H4 + P23 — defensively float-coerce so a JSON ``null`` value from
    # upstream doesn't crash hard-gate evaluation with ``None < float``, AND
    # treat NaN as below-threshold (NaN comparisons return False per IEEE 754
    # so the naive ``float(...) < threshold`` form silently rubber-stamps a
    # broken upstream metric calculation).
    if _metric_hard_fail(backtest_metrics, "annualized_sharpe", MIN_SHARPE):
        hard_fail_reasons.append("sharpe_below_threshold")
    if _metric_hard_fail(backtest_metrics, "max_drawdown", CRITIC_MAX_DRAWDOWN):
        hard_fail_reasons.append("drawdown_above_35pct")
    if trades < MIN_TRADES_BACKTEST:
        hard_fail_reasons.append("insufficient_trades")
    if _metric_hard_fail(backtest_metrics, "profit_factor", MIN_PROFIT_FACTOR):
        hard_fail_reasons.append("profit_factor_below_threshold")
    # P11-B-12: extend hard-fail set with severity + flag-driven gates so the
    # critic can veto on qualitative-but-clear failure modes (severe overlap or
    # counterparty / datasource red flags) without relying on the model's own
    # Go/No-Go.
    if severity_tag == "SEVERE OVERLAP":
        hard_fail_reasons.append("severe_overlap")
    if any(
        str(f.get("type") or "").strip() in {"counter_party_unclear", "datasource_issue"}
        for f in (flags or [])
        if isinstance(f, dict)
    ):
        hard_fail_reasons.append("counterparty_or_datasource_flag")
    if hard_fail_reasons:
        verdict_norm = "No-Go"
        violation_tags = sorted(set(violation_tags + hard_fail_reasons))
        # SEVERE OVERLAP is also added if extreme overfitting symptoms are
        # detected to keep severity in lockstep with rejection.
        if "drawdown_above_35pct" in hard_fail_reasons and severity_tag in {
            "NO OVERLAP", "LOW OVERLAP",
        }:
            severity_tag = "MODERATE OVERLAP"

    # T1-A — blind-pass merge. OBSERVE (default): record agreement only. ENFORCE:
    # a blind No-Go vetoes a story-aware Go (anti-confirmation-bias). The blind
    # pass can never RESCUE a No-Go — it only ever adds rejections.
    if blind is not None:
        if (
            _critic_blind_enforced()
            and blind["blind_verdict"] == "No-Go"
            and verdict_norm == "Go"
        ):
            verdict_norm = "No-Go"
            violation_tags = sorted(set(violation_tags + ["blind_critic_veto"]))

    return {
        "verdict": verdict_norm,
        "critique_markdown": critique,
        "violation_tags": violation_tags,
        "severity_tag": severity_tag,
        "alpha_category": alpha_category,
        "flags": flags,
        "confidence": confidence,
        "soul_questions": soul_questions,
        "metrics_snapshot": full_metrics,
        # T1-A — additive blind-pass telemetry (None/[] when disabled; existing
        # consumers read by explicit key and are unaffected).
        "blind_verdict": blind["blind_verdict"] if blind else None,
        "blind_confidence": blind["blind_confidence"] if blind else None,
        "blind_reasons": blind["blind_reasons"] if blind else [],
        "blind_agreement": (None if blind is None else (blind["blind_verdict"] == verdict_norm)),
    }


__all__ = [
    "review_strategy",
    "CRITIC_SYSTEM_PROMPT",
    "SEVERITY_TAGS",
    "ALPHA_CATEGORIES",
    "SOUL_QUESTION_KEYS",
]
