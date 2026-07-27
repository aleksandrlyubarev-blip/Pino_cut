"""Model routing, pricing and budget guard for the Gemini agent platform.

Routing policy from the specification: Flash Lite is the default for routing and
metadata work, Flash carries the assistant layer, Pro is reserved for gated
reasoning nodes, and the Live tier is only for realtime voice/video sessions.
The router exists so that tier choice is a policy decision in one place rather
than a per-agent habit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ModelTier(str, Enum):
    """Cost/capability tier, not a specific model version."""

    FLASH_LITE = "flash_lite"
    FLASH = "flash"
    PRO = "pro"
    LIVE = "live"


#: Tier -> model id. Kept in one place so a platform upgrade is a single edit.
MODEL_IDS: dict[ModelTier, str] = {
    ModelTier.FLASH_LITE: "gemini-2.5-flash-lite",
    ModelTier.FLASH: "gemini-2.5-flash",
    ModelTier.PRO: "gemini-2.5-pro",
    ModelTier.LIVE: "gemini-2.5-flash-live",
}


@dataclass(frozen=True, slots=True)
class TokenPrice:
    """USD per 1M tokens."""

    input_per_mtok: float
    output_per_mtok: float


#: List prices from the specification. Treat as planning figures, verify before
#: committing to a budget — vendor pricing moves.
PRICING: dict[ModelTier, TokenPrice] = {
    ModelTier.FLASH_LITE: TokenPrice(0.10, 0.40),
    ModelTier.FLASH: TokenPrice(0.30, 2.50),
    ModelTier.PRO: TokenPrice(1.25, 10.00),
    ModelTier.LIVE: TokenPrice(0.50, 2.00),  # text in/out; audio is billed higher
}

#: Per-1000-call prices for grounded retrieval surfaces.
RETRIEVAL_PRICING_PER_1K: dict[str, float] = {
    "grounding_own_data": 2.50,
    "grounding_google_search": 35.00,
    "agent_search_standard": 1.50,
    "agent_search_enterprise": 4.00,
    "agent_search_media": 2.00,
}


class TaskKind(str, Enum):
    """What the agent is about to do — the routing input."""

    ROUTING = "routing"
    METADATA = "metadata"
    DRAFTING = "drafting"
    RETRIEVAL_QA = "retrieval_qa"
    DEEP_REASONING = "deep_reasoning"
    QUALITY_REVIEW = "quality_review"
    REALTIME_VOICE = "realtime_voice"


DEFAULT_ROUTING: dict[TaskKind, ModelTier] = {
    TaskKind.ROUTING: ModelTier.FLASH_LITE,
    TaskKind.METADATA: ModelTier.FLASH_LITE,
    TaskKind.DRAFTING: ModelTier.FLASH,
    TaskKind.RETRIEVAL_QA: ModelTier.FLASH,
    TaskKind.DEEP_REASONING: ModelTier.PRO,
    TaskKind.QUALITY_REVIEW: ModelTier.PRO,
    TaskKind.REALTIME_VOICE: ModelTier.LIVE,
}

#: Where a tier falls back to when the budget guard asks for a downgrade.
_DOWNGRADE: dict[ModelTier, ModelTier] = {
    ModelTier.PRO: ModelTier.FLASH,
    ModelTier.FLASH: ModelTier.FLASH_LITE,
    ModelTier.FLASH_LITE: ModelTier.FLASH_LITE,
    ModelTier.LIVE: ModelTier.LIVE,  # realtime has no cheaper equivalent
}


@dataclass(frozen=True, slots=True)
class UsageEntry:
    """One model call, priced."""

    label: str
    tier: ModelTier
    input_tokens: int
    output_tokens: int

    @property
    def cost_usd(self) -> float:
        price = PRICING[self.tier]
        return (
            self.input_tokens * price.input_per_mtok + self.output_tokens * price.output_per_mtok
        ) / 1_000_000


@dataclass
class UsageLedger:
    """Running cost of a workflow run. Attached to the audit trail."""

    entries: list[UsageEntry] = field(default_factory=list)
    retrieval_calls: dict[str, int] = field(default_factory=dict)

    def record(
        self, label: str, tier: ModelTier, input_tokens: int, output_tokens: int
    ) -> UsageEntry:
        entry = UsageEntry(
            label=label, tier=tier, input_tokens=input_tokens, output_tokens=output_tokens
        )
        self.entries.append(entry)
        return entry

    def record_retrieval(self, surface: str, calls: int = 1) -> None:
        self.retrieval_calls[surface] = self.retrieval_calls.get(surface, 0) + calls

    @property
    def model_cost_usd(self) -> float:
        return sum(entry.cost_usd for entry in self.entries)

    @property
    def retrieval_cost_usd(self) -> float:
        return sum(
            calls * RETRIEVAL_PRICING_PER_1K.get(surface, 0.0) / 1000
            for surface, calls in self.retrieval_calls.items()
        )

    @property
    def total_usd(self) -> float:
        return self.model_cost_usd + self.retrieval_cost_usd

    def by_tier(self) -> dict[ModelTier, float]:
        totals: dict[ModelTier, float] = {}
        for entry in self.entries:
            totals[entry.tier] = totals.get(entry.tier, 0.0) + entry.cost_usd
        return totals

    def summary(self) -> dict:
        return {
            "model_cost_usd": round(self.model_cost_usd, 6),
            "retrieval_cost_usd": round(self.retrieval_cost_usd, 6),
            "total_usd": round(self.total_usd, 6),
            "calls": len(self.entries),
            "by_tier": {tier.value: round(cost, 6) for tier, cost in self.by_tier().items()},
            "retrieval_calls": dict(self.retrieval_calls),
        }


class BudgetState(str, Enum):
    OK = "ok"
    ALERT = "alert"  # past the alert ratio, downgrade routing
    EXCEEDED = "exceeded"  # hard stop for non-critical work


@dataclass
class BudgetGuard:
    """Monthly cap with an alert threshold below it.

    The alert fires *before* the cap so routing can degrade to cheaper tiers
    instead of the run failing at the cap.
    """

    cap_usd: float
    alert_ratio: float = 0.8
    spent_usd: float = 0.0

    def charge(self, amount_usd: float) -> BudgetState:
        self.spent_usd += amount_usd
        return self.state

    @property
    def state(self) -> BudgetState:
        if self.cap_usd <= 0:
            return BudgetState.OK
        if self.spent_usd >= self.cap_usd:
            return BudgetState.EXCEEDED
        if self.spent_usd >= self.cap_usd * self.alert_ratio:
            return BudgetState.ALERT
        return BudgetState.OK


class ModelRouter:
    """Maps a task to a model tier under the current budget state."""

    def __init__(
        self,
        routing: dict[TaskKind, ModelTier] | None = None,
        budget: BudgetGuard | None = None,
    ):
        self._routing = dict(routing or DEFAULT_ROUTING)
        self.budget = budget

    def tier_for(self, task: TaskKind) -> ModelTier:
        tier = self._routing[task]
        if self.budget is not None and self.budget.state is not BudgetState.OK:
            return _DOWNGRADE[tier]
        return tier

    def model_id(self, task: TaskKind) -> str:
        return MODEL_IDS[self.tier_for(task)]
