"""Week 2 schema metadata retrieval.

This module builds simple sparse embeddings from schema metadata and performs
cosine top-k retrieval over table and column text. It is dependency-free so it
can run in the current repository without installing a vector database.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def normalize_vector(vector: Counter[str]) -> dict[str, float]:
    norm = math.sqrt(sum(weight * weight for weight in vector.values()))
    if norm == 0:
        return {}
    return {token: weight / norm for token, weight in vector.items()}


def cosine_similarity(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(weight * right.get(token, 0.0) for token, weight in left.items())


@dataclass(frozen=True)
class SchemaDocument:
    name: str
    text: str
    metadata: dict[str, object]


class SchemaRetrievalEngine:
    def __init__(self, documents: Sequence[SchemaDocument]):
        self.documents = list(documents)
        self.embeddings = [normalize_vector(Counter(tokenize(document.text))) for document in self.documents]

    @classmethod
    def from_metadata(cls, metadata_path: Path) -> "SchemaRetrievalEngine":
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        documents: list[SchemaDocument] = []

        for table in metadata.get("tables", []):
            table_name = str(table["name"])
            table_text_parts = [
                table_name,
                str(table.get("description", "")),
                " ".join(table.get("keywords", [])),
                " ".join(table.get("primary_key", [])),
            ]

            for foreign_key in table.get("foreign_keys", []):
                table_text_parts.append(" ".join(foreign_key.get("columns", [])))
                table_text_parts.append(str(foreign_key.get("references", "")))

            for column in table.get("columns", []):
                table_text_parts.append(str(column.get("name", "")))
                table_text_parts.append(str(column.get("description", "")))
                table_text_parts.append(" ".join(column.get("keywords", [])))

            documents.append(
                SchemaDocument(
                    name=table_name,
                    text=" ".join(table_text_parts),
                    metadata=table,
                )
            )

        return cls(documents)

    def query(self, text: str, top_k: int = 3) -> list[dict[str, object]]:
        query_embedding = normalize_vector(Counter(tokenize(text)))
        scored = []
        for document, embedding in zip(self.documents, self.embeddings, strict=False):
            scored.append(
                {
                    "name": document.name,
                    "score": round(cosine_similarity(query_embedding, embedding), 4),
                    "metadata": document.metadata,
                }
            )
        scored.sort(key=lambda item: (-item["score"], item["name"]))
        return scored[:top_k]


def build_engine(metadata_path: Path | None = None) -> SchemaRetrievalEngine:
    if metadata_path is None:
        metadata_path = Path(__file__).with_name("schema_metadata.json")
    return SchemaRetrievalEngine.from_metadata(metadata_path)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Schema retrieval demo for Week 2")
    parser.add_argument("query", help="Natural-language schema search query")
    parser.add_argument("--top-k", type=int, default=3, help="Number of results to return")
    parser.add_argument(
        "--metadata",
        type=Path,
        default=Path(__file__).with_name("schema_metadata.json"),
        help="Path to schema metadata JSON",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    engine = build_engine(args.metadata)
    results = engine.query(args.query, top_k=args.top_k)
    print(json.dumps({"query": args.query, "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())