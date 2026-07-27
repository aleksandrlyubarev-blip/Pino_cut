"""Data classes, access contours and redaction for the media agent platform.

The platform runs two logical contours over one project: an internal contour for
the crew (scripts, call sheets, budgets) and a fan-facing contour that must never
see unpublished material. Every document carries a :class:`DataClass`; every
request carries a :class:`Contour`. Retrieval results are filtered before they
ever reach a model prompt, not after.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class DataClass(str, Enum):
    """Sensitivity class of a document or artifact."""

    PRIVATE_DEVELOPMENT = "private_development"  # scripts, budget notes, internal cuts
    RESTRICTED_PRODUCTION = "restricted_production"  # call sheets, crew, locations
    PARTNER_SHARED = "partner_shared"  # approved dailies, technical metadata
    PUBLIC_FAN_SAFE = "public_fan_safe"  # published material only


#: Higher number means more sensitive.
SENSITIVITY_RANK: dict[DataClass, int] = {
    DataClass.PUBLIC_FAN_SAFE: 0,
    DataClass.PARTNER_SHARED: 1,
    DataClass.RESTRICTED_PRODUCTION: 2,
    DataClass.PRIVATE_DEVELOPMENT: 3,
}


class Contour(str, Enum):
    """Who is asking. Determines the highest data class that may be read."""

    INTERNAL = "internal"  # director, writer, line producer, crew
    PARTNER = "partner"  # vendors, contractors, partner MCP callbacks
    FAN = "fan"  # fan-facing assistant, marketing surfaces


#: Ceiling per contour. A contour may read its own rank and everything below it.
CONTOUR_CEILING: dict[Contour, int] = {
    Contour.INTERNAL: SENSITIVITY_RANK[DataClass.PRIVATE_DEVELOPMENT],
    Contour.PARTNER: SENSITIVITY_RANK[DataClass.PARTNER_SHARED],
    Contour.FAN: SENSITIVITY_RANK[DataClass.PUBLIC_FAN_SAFE],
}


@dataclass(frozen=True, slots=True)
class AccessViolation:
    """A read that was denied by policy. Surfaced to the governance agent."""

    contour: Contour
    data_class: DataClass
    resource_id: str
    reason: str

    def __str__(self) -> str:
        return (
            f"{self.contour.value} may not read {self.data_class.value} "
            f"({self.resource_id}): {self.reason}"
        )


class AccessPolicy:
    """Least-privilege read policy across contours."""

    def __init__(self, ceiling: dict[Contour, int] | None = None):
        self._ceiling = dict(ceiling or CONTOUR_CEILING)

    def can_read(self, contour: Contour, data_class: DataClass) -> bool:
        return SENSITIVITY_RANK[data_class] <= self._ceiling[contour]

    def check(
        self, contour: Contour, data_class: DataClass, resource_id: str
    ) -> AccessViolation | None:
        """Return a violation when the read is not allowed, otherwise ``None``."""
        if self.can_read(contour, data_class):
            return None
        return AccessViolation(
            contour=contour,
            data_class=data_class,
            resource_id=resource_id,
            reason=(
                f"contour ceiling is {self._ceiling[contour]}, "
                f"resource rank is {SENSITIVITY_RANK[data_class]}"
            ),
        )


#: Patterns stripped from any text leaving the internal contour.
DEFAULT_REDACTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"[\w.+-]+@[\w-]+\.[\w.]+", "[redacted:email]"),
    (r"\+?\d[\d\s().-]{8,}\d", "[redacted:phone]"),
    (r"(?i)\b(?:iban|account\s*no\.?)\s*[:#]?\s*[\w-]{6,}", "[redacted:financial]"),
)


@dataclass
class Redactor:
    """Removes contact and financial details before fan-facing publication.

    Redaction is a last line of defence, not the access control itself: a
    document that fails :class:`AccessPolicy` is dropped entirely and never
    reaches this class.
    """

    patterns: tuple[tuple[str, str], ...] = DEFAULT_REDACTION_PATTERNS
    spoiler_terms: tuple[str, ...] = ()
    hits: list[str] = field(default_factory=list)

    def apply(self, text: str) -> str:
        result = text
        for pattern, replacement in self.patterns:
            result, count = re.subn(pattern, replacement, result)
            if count:
                self.hits.append(pattern)
        for term in self.spoiler_terms:
            result, count = re.subn(re.escape(term), "[redacted:spoiler]", result, flags=re.I)
            if count:
                self.hits.append(term)
        return result
