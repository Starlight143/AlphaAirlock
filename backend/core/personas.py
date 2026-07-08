"""Named agent personas for the Mission Control UI.

The reference system attaches a human-like persona to each agent role. The
frontend Agent Team card row reads this mapping verbatim through the
GET /api/agents endpoint.

P8-FIX/H-9: each persona now declares which side of the Team A / Team B
adversarial split it belongs to ("ops" for cross-cutting roles). Three new
personas added — Team A lead (Aaron), Team B lead (Bella) and the
post-mortem writer (Mira) — so the AgentTeam UI can group cards as the
reference video shows.

Single source of truth — never duplicate these names anywhere else.
"""

from __future__ import annotations

from typing import Dict, List, Literal, TypedDict


# Team allegiance — drives the AgentTeam grouping in Mission Control.
AgentTeam = Literal["A", "B", "ops"]


class AgentPersona(TypedDict):
    key: str           # internal agent identifier (matches orchestrator's `active_agent`)
    name: str          # display name shown on the card
    role: str          # title under the name
    team: AgentTeam    # Team A (research/exec) | Team B (criticism) | ops
    color: str         # accent hex used for the avatar ring + status dot
    description: str   # short tooltip line
    capabilities: List[str]  # tag chips at the bottom of the card


AGENT_PERSONAS: Dict[str, AgentPersona] = {
    "team_a_lead": AgentPersona(
        key="team_a_lead",
        name="Aaron",
        role="Team A Lead",
        team="A",
        color="#0EA5E9",
        description="Coordinates Research / Coder / Backtester loop and owns Stage 0-4 hand-offs.",
        capabilities=["coordination", "prioritization", "handoff"],
    ),
    "intake": AgentPersona(
        key="intake",
        name="Terry",
        role="Data Collector",
        team="A",
        color="#06B6D4",
        description="Harvests raw market commentary into structured KnowledgeNodes.",
        capabilities=["intake", "tagging", "dedup"],
    ),
    "researcher": AgentPersona(
        key="researcher",
        name="Eamon",
        role="Alpha Researcher",
        team="A",
        color="#22C55E",
        description="Synthesizes knowledge nodes into testable Alpha Stories with backtest config.",
        capabilities=["thesis", "yaml-config", "regime-aware"],
    ),
    "coder": AgentPersona(
        key="coder",
        name="Peter",
        role="CTO",
        team="A",
        color="#A855F7",
        description="Translates an Alpha Story into sandboxed compute_factor() code.",
        capabilities=["pandas", "numpy", "look-ahead-safe"],
    ),
    "backtester": AgentPersona(
        key="backtester",
        name="Jimmy",
        role="XR Builder",
        team="A",
        color="#EF4444",
        description="Runs hourly vectorized backtests with fees + slippage.",
        capabilities=["vectorized", "sharpe", "drawdown"],
    ),
    "team_b_lead": AgentPersona(
        key="team_b_lead",
        name="Bella",
        role="Team B Lead",
        team="B",
        color="#F59E0B",
        description="Coordinates adversarial review and owns Go/No-Go escalation.",
        capabilities=["risk-policy", "veto", "escalation"],
    ),
    "critic": AgentPersona(
        key="critic",
        name="Conor",
        role="CEO",
        team="B",
        color="#F97316",
        description="Team B adversarial risk critic with hard rejection thresholds.",
        capabilities=["risk-review", "soul-questions", "veto"],
    ),
    "postmortem_writer": AgentPersona(
        key="postmortem_writer",
        name="Mira",
        role="Postmortem Writer",
        team="ops",
        color="#D946EF",
        description="Writes postmortem KnowledgeNodes for rejected / graveyard strategies and rewires the knowledge graph.",
        capabilities=["narrative", "lessons-learned", "kb-write"],
    ),
    "keeper": AgentPersona(
        key="keeper",
        name="Helen",
        role="Data Keeper",
        team="ops",
        color="#14B8A6",
        description="Maintains the knowledge graph + ingest source health.",
        capabilities=["scheduler", "asset-cache", "dedup"],
    ),
    "portfolio": AgentPersona(
        key="portfolio",
        name="Calvin",
        role="Portfolio Manager",
        team="ops",
        color="#3B82F6",
        description="Combines approved strategies into a weighted portfolio.",
        capabilities=["risk-parity", "vol-target", "cvar"],
    ),
    "orchestrator": AgentPersona(
        key="orchestrator",
        name="Owen",
        role="Workflow Orchestrator",
        team="ops",
        color="#8B5CF6",
        description="Drives stage transitions and persona hand-offs across the full Stage 0-7 pipeline.",
        capabilities=["scheduling", "handoff", "state-machine"],
    ),
    "risk_auditor": AgentPersona(
        key="risk_auditor",
        name="Riley",
        role="Risk Auditor",
        team="B",
        color="#DC2626",
        description="Runs the operational risk framework — drawdown caps, exposure limits, incident audits.",
        capabilities=["risk-framework", "audit", "incident-review"],
    ),
}


def list_personas() -> List[AgentPersona]:
    """Stable order for UI rendering — Team A first (left-to-right pipeline flow),
    Team B second, ops last. Mission Control groups by ``team`` for display."""
    order = [
        # Team A — pipeline execution roles (Stage 0-4)
        "team_a_lead",
        "intake",
        "researcher",
        "coder",
        "backtester",
        # Team B — adversarial review roles
        "team_b_lead",
        "critic",
        "risk_auditor",
        # Ops — cross-cutting operational roles
        "keeper",
        "portfolio",
        "orchestrator",
        "postmortem_writer",
    ]
    return [AGENT_PERSONAS[k] for k in order if k in AGENT_PERSONAS]


def persona_for(agent_key: str) -> AgentPersona:
    """Lookup with a sane fallback so unknown agent keys never crash the UI."""
    if agent_key in AGENT_PERSONAS:
        return AGENT_PERSONAS[agent_key]
    return AgentPersona(
        key=agent_key,
        name=agent_key.title() if agent_key else "Unknown",
        role="Agent",
        team="ops",
        color="#64748B",
        description="Agent without registered persona.",
        capabilities=[],
    )


__all__ = ["AGENT_PERSONAS", "AgentPersona", "AgentTeam", "list_personas", "persona_for"]
