"""Extract, chunk, embed, and store PDF pages."""

from pathlib import Path
import re
from uuid import uuid4

import fitz

from database import insert_chunks
from embeddings import embed_texts


WORDS_PER_CHUNK = 800


def chunk_text(text: str, words_per_chunk: int = WORDS_PER_CHUNK) -> list[str]:
    """Split text into word-based blocks while discarding whitespace-only blocks."""
    words = re.findall(r"\S+", text)
    return [
        " ".join(words[start : start + words_per_chunk])
        for start in range(0, len(words), words_per_chunk)
        if words[start : start + words_per_chunk]
    ]


def _page_word_maps(page: fitz.Page) -> list[dict]:
    """Return words with their exact PDF-space bounding boxes."""
    words = page.get_text("words", sort=True)
    return [
        {
            "text": word[4],
            "bbox": [round(float(value), 2) for value in word[:4]],
        }
        for word in words
        if word[4].strip()
    ]


def build_rows(
    pdf_path: str | Path,
    document_name: str,
    owner_id: str,
    document_id: str,
) -> list[dict]:
    """Create database rows, retaining the source page for every chunk."""
    rows: list[dict] = []
    path = Path(pdf_path)
    with fitz.open(path) as pdf:
        for page_index, page in enumerate(pdf, start=1):
            page_words = _page_word_maps(page)
            page_chunks = [
                page_words[start : start + WORDS_PER_CHUNK]
                for start in range(0, len(page_words), WORDS_PER_CHUNK)
                if page_words[start : start + WORDS_PER_CHUNK]
            ]
            chunk_texts = [
                " ".join(word["text"] for word in chunk)
                for chunk in page_chunks
            ]
            embeddings = embed_texts(chunk_texts)
            rows.extend(
                {
                    "document_id": document_id,
                    "owner_id": owner_id,
                    "document_name": document_name,
                    "page_number": page_index,
                    "chunk_number": chunk_index,
                    "content": content,
                    "coordinates": coordinates,
                    "embedding": embedding,
                }
                for chunk_index, (content, coordinates, embedding) in enumerate(
                    zip(chunk_texts, page_chunks, embeddings), start=1
                )
            )
    return rows


def ingest_pdf(
    pdf_path: str | Path,
    document_name: str,
    owner_id: str,
    document_id: str | None = None,
) -> tuple[str, int]:
    """Index a PDF and return the number of stored chunks."""
    resolved_document_id = document_id or str(uuid4())
    rows = build_rows(pdf_path, document_name, owner_id, resolved_document_id)
    insert_chunks(rows)
    return resolved_document_id, len(rows)
