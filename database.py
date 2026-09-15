"""Supabase client setup and database helpers."""

import os
from functools import lru_cache

from supabase import Client, create_client


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    """Create one Supabase client from environment configuration."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be configured.")
    return create_client(url, key)


def insert_chunks(rows: list[dict]) -> None:
    """Insert indexed chunks in batches to avoid oversized requests."""
    if not rows:
        return

    client = get_supabase()
    batch_size = 100
    for start in range(0, len(rows), batch_size):
        client.table("documents").insert(rows[start : start + batch_size]).execute()


def list_documents(owner_id: str, is_admin: bool = False) -> list[dict]:
    """Return distinct documents visible to the current principal."""
    query = get_supabase().table("documents").select(
        "document_id,document_name,owner_id"
    )
    if not is_admin:
        query = query.eq("owner_id", owner_id)
    rows = query.execute().data or []
    documents: dict[str, dict] = {}
    for row in rows:
        document_id = row.get("document_id")
        if document_id and document_id not in documents:
            documents[document_id] = {
                "document_id": document_id,
                "document_name": row.get("document_name", "Untitled PDF"),
            }
    return sorted(documents.values(), key=lambda document: document["document_name"].lower())


def delete_document(document_id: str, owner_id: str, is_admin: bool = False) -> int:
    """Delete all chunks for a document when the principal has access."""
    query = get_supabase().table("documents").delete().eq("document_id", document_id)
    if not is_admin:
        query = query.eq("owner_id", owner_id)
    response = query.execute()
    return len(response.data or [])
