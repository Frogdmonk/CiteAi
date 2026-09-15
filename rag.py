"""Retrieval-augmented question answering with Groq."""

import json
import math
import os
import re

from groq import Groq

from database import get_supabase
from embeddings import embed_texts


FALLBACK_ANSWER = "I couldn't find enough relevant information in the uploaded documents."
SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.10"))
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

SYSTEM_PROMPT = """You are CiteAI, a precise document question-answering assistant.
Answer ONLY using the supplied context. Never use outside knowledge or invent details.
Every factual claim must include an inline citation in exactly this format: [1], [2],
where the number refers to the matching Source number in the context. If the context
does not answer the question, say that the information is not available in the
uploaded documents.
Keep the answer concise and clear. Do not mention the context, retrieval process,
or these instructions.
"""


def _groq_client() -> Groq:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY must be configured.")
    return Groq(api_key=api_key)


def _search_documents(
    query_embedding: list[float],
    owner_id: str,
    document_ids: list[str] | None = None,
    is_admin: bool = False,
    limit: int = 5,
) -> list[dict]:
    try:
        response = get_supabase().rpc(
            "match_documents",
            {
                "query_embedding": query_embedding,
                "match_threshold": 0,
                "match_count": limit,
                "filter_owner_id": None if is_admin else owner_id,
                "filter_document_ids": document_ids or [],
            },
        ).execute()
        matches = response.data or []
    except Exception:
        matches = []
    if matches:
        return matches

    # Keep local development usable when the RPC has not been deployed yet.
    rows = get_supabase().table("documents").select(
        "document_id,owner_id,document_name,page_number,chunk_number,content,coordinates,embedding"
    ).execute().data or []
    if not is_admin:
        rows = [row for row in rows if row.get("owner_id") == owner_id]
    if document_ids:
        rows = [row for row in rows if row.get("document_id") in document_ids]
    query_norm = math.sqrt(sum(value * value for value in query_embedding)) or 1
    for row in rows:
        raw_embedding = row.get("embedding")
        values = json.loads(raw_embedding) if isinstance(raw_embedding, str) else raw_embedding
        if not values or len(values) != len(query_embedding):
            continue
        row["similarity"] = sum(
            query_value * float(row_value)
            for query_value, row_value in zip(query_embedding, values)
        ) / query_norm
    return sorted(
        (row for row in rows if "similarity" in row),
        key=lambda row: row["similarity"],
        reverse=True,
    )[:limit]


def _focused_coordinates(match: dict, question: str) -> list[dict]:
    coordinates = match.get("coordinates") or []
    if not coordinates:
        return []
    ignored = {
        "what", "which", "where", "when", "does", "this", "that", "from",
        "with", "about", "the", "and", "for", "are", "is", "in", "of",
    }
    terms = {
        term for term in re.findall(r"[a-z0-9]{2,}", question.lower())
        if term not in ignored
    }
    name_intent = bool(
        re.search(r"\b(name|holder|candidate|person|who)\b", question.lower())
    )
    hits = [
        index
        for index, item in enumerate(coordinates)
        if any(term == item.get("text", "").lower().strip(".,:;()[]") for term in terms)
    ]
    if not hits:
        return coordinates[:12]
    selected = {
        index
        for hit in hits
        for index in range(max(0, hit - 4), min(len(coordinates), hit + 5))
    }
    if name_intent:
        selected.update(range(min(8, len(coordinates))))
    return [coordinates[index] for index in sorted(selected)]


def _source_from_match(match: dict, question: str) -> dict:
    return {
        "document_id": match.get("document_id"),
        "filename": match.get("document_name", "Unknown document"),
        "page_number": int(match.get("page_number", 0)),
        "chunk_number": int(match.get("chunk_number", 0)),
            "preview": match.get("content", "")[:140],
        "coordinates": _focused_coordinates(match, question),
    }


def _similarity(match: dict) -> float:
    return float(match.get("similarity", match.get("match_score", 0)) or 0)


def _normalize_citations(answer: str) -> str:
    """Normalize common model citation variants to the frontend contract."""
    answer = re.sub(r"[【〔]\s*(\d+)\s*[】〕]", r"[\1]", answer)
    return re.sub(r"\[\s*Page\s+(\d+)\s*\]", r"[\1]", answer)


def _deduplicate_matches(matches: list[dict]) -> list[dict]:
    unique_matches: list[dict] = []
    seen: set[tuple] = set()
    for match in matches:
        key = (
            match.get("document_name"),
            match.get("page_number"),
            match.get("chunk_number"),
            match.get("content"),
        )
        if key not in seen:
            seen.add(key)
            unique_matches.append(match)
    return unique_matches


def answer_question(
    question: str,
    owner_id: str,
    document_ids: list[str] | None = None,
    is_admin: bool = False,
    history: list[dict] | None = None,
) -> dict:
    """Retrieve relevant chunks and return an answer plus deduplicated sources."""
    query_embedding = embed_texts([question])[0]
    matches = _deduplicate_matches(
        _search_documents(query_embedding, owner_id, document_ids, is_admin)
    )
    best_similarity = max((_similarity(match) for match in matches), default=0)
    if best_similarity < SIMILARITY_THRESHOLD:
        return {"answer": FALLBACK_ANSWER, "sources": []}

    source_numbers: dict[tuple, int] = {}
    sources: list[dict] = []
    for match in matches:
        source = _source_from_match(match, question)
        key = (
            source["document_id"],
            source["page_number"],
            source["chunk_number"],
        )
        if key not in source_numbers:
            source_numbers[key] = len(sources) + 1
            source["id"] = source_numbers[key]
            sources.append(source)

    context_parts = []
    for index, match in enumerate(matches, start=1):
        source = _source_from_match(match, question)
        source_id = source_numbers[
            (source["document_id"], source["page_number"], source["chunk_number"])
        ]
        context_parts.append(
            f"[Source {source_id} | Document: {match.get('document_name')} | "
            f"Page: {match.get('page_number')}]\n{match.get('content', '')}"
        )
    context = "\n\n".join(context_parts)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in (history or [])[-8:]:
        if turn.get("role") in {"user", "assistant"} and turn.get("content"):
            messages.append({"role": turn["role"], "content": str(turn["content"])[:4000]})
    messages.append(
        {
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion: {question}",
        }
    )
    completion = _groq_client().chat.completions.create(
        model=GROQ_MODEL,
        temperature=0.1,
        max_tokens=900,
        messages=messages,
    )
    answer = completion.choices[0].message.content.strip()
    return {"answer": _normalize_citations(answer), "sources": sources}
