"""Corpus manifests: the local stand-in for Agent Search data stores.

A manifest makes the data class of every document explicit and reviewable in
version control. That property is what the production indexes must preserve too:
classification is metadata a human sets, never something inferred at query time.
"""

from __future__ import annotations

import json
from pathlib import Path

from pinocut.agentplatform.classification import DataClass
from pinocut.agentplatform.retrieval import Document


def load_manifest(path: Path) -> list[Document]:
    """Load documents from a JSON manifest.

    Each entry needs ``docId``, ``source``, ``dataClass`` and either inline
    ``text`` or a ``path`` relative to the manifest. An entry with an unknown
    data class is a hard error — defaulting it would be the exact failure the
    classification exists to prevent.
    """
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list):
        raise ValueError(f"{path}: manifest must be a JSON array of documents")

    documents: list[Document] = []
    for index, entry in enumerate(manifest):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}[{index}]: entry must be an object")
        missing = [key for key in ("docId", "source", "dataClass") if key not in entry]
        if missing:
            raise ValueError(f"{path}[{index}]: missing {', '.join(missing)}")
        try:
            data_class = DataClass(entry["dataClass"])
        except ValueError as exc:
            raise ValueError(f"{path}[{index}]: unknown dataClass {entry['dataClass']!r}") from exc

        if "text" in entry:
            text = str(entry["text"])
        elif "path" in entry:
            text = (path.parent / entry["path"]).read_text(encoding="utf-8")
        else:
            raise ValueError(f"{path}[{index}]: needs either 'text' or 'path'")

        documents.append(
            Document(
                doc_id=str(entry["docId"]),
                source=str(entry["source"]),
                data_class=data_class,
                text=text,
                uri=entry.get("uri"),
            )
        )
    return documents
