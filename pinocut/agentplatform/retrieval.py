"""Grounded retrieval with contour-aware filtering.

In production this sits on Agent Search / RAG Engine; the interface is the same
either way. What matters architecturally is the order of operations: the access
filter runs *inside* retrieval, so a document above the caller's contour ceiling
is never placed in a prompt, and the attempt is recorded as a violation for the
governance agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pinocut.agentplatform.classification import (
    AccessPolicy,
    AccessViolation,
    Contour,
    DataClass,
)


@dataclass(frozen=True, slots=True)
class Document:
    """One indexed source document."""

    doc_id: str
    source: str  # display name used in citations
    data_class: DataClass
    text: str
    uri: str | None = None


@dataclass(frozen=True, slots=True)
class Citation:
    """A grounded reference the UI can render and a reviewer can open.

    Carries its ``data_class`` so the governance agent can re-verify, at the end
    of a run, that nothing above the caller's ceiling reached the output — a
    check independent of the filter that produced it.
    """

    doc_id: str
    source: str
    snippet: str
    data_class: DataClass = DataClass.PUBLIC_FAN_SAFE
    uri: str | None = None

    def to_dict(self) -> dict:
        return {
            "docId": self.doc_id,
            "source": self.source,
            "snippet": self.snippet,
            "dataClass": self.data_class.value,
            "uri": self.uri,
        }


@dataclass
class GroundedContext:
    """Retrieval output: what the model may see, plus what was filtered out.

    ``denied`` is expected traffic, not an incident: a fan question against a
    corpus that also holds scripts denies the scripts every time. A real
    incident is a denied class appearing in ``documents``, which is what the
    governance agent checks for.
    """

    documents: list[Document] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    denied: list[AccessViolation] = field(default_factory=list)

    @property
    def is_grounded(self) -> bool:
        return bool(self.documents)

    def as_prompt_context(self, max_chars: int = 4000) -> str:
        parts: list[str] = []
        budget = max_chars
        for doc in self.documents:
            chunk = doc.text[:budget]
            parts.append(f"[{doc.source}]\n{chunk}")
            budget -= len(chunk)
            if budget <= 0:
                break
        return "\n\n".join(parts)


class RetrievalService(Protocol):
    """Swap for a RAG Engine / Agent Search client without touching agents."""

    def retrieve(self, query: str, *, contour: Contour, top_k: int = 5) -> GroundedContext: ...


class InMemoryRetrievalService:
    """Keyword-scored corpus. Offline stand-in for Agent Search.

    Scoring is deliberately simple (term overlap): this class exists to exercise
    the access-filter contract and the citation shape, not to be a search engine.
    """

    def __init__(self, documents: list[Document], policy: AccessPolicy | None = None):
        self._documents = list(documents)
        self._policy = policy or AccessPolicy()

    def add(self, document: Document) -> None:
        self._documents.append(document)

    def retrieve(self, query: str, *, contour: Contour, top_k: int = 5) -> GroundedContext:
        terms = _content_terms(query)
        context = GroundedContext()
        scored: list[tuple[float, Document]] = []

        for doc in self._documents:
            haystack = _content_terms(f"{doc.source} {doc.text}")
            overlap = len(terms & haystack)
            if not overlap:
                continue
            violation = self._policy.check(contour, doc.data_class, doc.doc_id)
            if violation is not None:
                # The query would have hit this document; the filter dropped it.
                context.denied.append(violation)
                continue
            scored.append((overlap / max(1, len(terms)), doc))

        scored.sort(key=lambda pair: (-pair[0], pair[1].doc_id))
        for _score, doc in scored[:top_k]:
            context.documents.append(doc)
            context.citations.append(
                Citation(
                    doc_id=doc.doc_id,
                    source=doc.source,
                    snippet=doc.text[:180].strip(),
                    data_class=doc.data_class,
                    uri=doc.uri,
                )
            )
        return context


#: Words too common to carry retrieval signal in either working language.
STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "for", "with", "from", "that", "this", "what", "where", "when",
        "does", "did", "has", "have", "are", "was", "were", "about", "into", "your",
        "you", "our", "its", "it's", "not", "but", "all", "any", "can", "will",
        "что", "как", "где", "для", "или", "это", "его", "она", "они", "мне", "нам",
        "при", "над", "под", "без", "уже", "еще", "ещё", "так", "там", "тут",
    }
)


def _tokenize(text: str) -> list[str]:
    return [token.strip(".,;:!?()[]\"'—–-").lower() for token in text.split()]


def _content_terms(text: str) -> set[str]:
    return {token for token in _tokenize(text) if len(token) > 2 and token not in STOPWORDS}
