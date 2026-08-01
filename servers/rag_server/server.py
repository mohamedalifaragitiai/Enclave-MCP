"""
rag_server - Phase 2 of the Sovereign MCP Platform.

Exposes the three MCP primitives over stdio:

  tool     search_documents(query)  - vector search over the ChromaDB store
  resource chunk://{chunk_id}       - full text of one retrieved chunk
  prompt   rag_answer(question)     - grounded-answer template for the host

Phase 1's naive keyword scoring has been replaced by dense retrieval
(BAAI/bge-small-en-v1.5 via fastembed + ChromaDB), but the tool name and
signature are unchanged, so the agent-facing contract from Phase 1 still holds.

Chunking, IDs, and store locations live in retrieval.py, shared with
scripts/ingest.py so the two cannot disagree.

Transport: stdio.

IMPORTANT: on stdio transport, stdout *is* the MCP wire. Never print() to stdout
from this process - a single stray line corrupts the JSON-RPC stream and the
client drops the connection. All diagnostics go to stderr via retrieval.log().
"""

from __future__ import annotations

from mcp.server import MCPServer

import retrieval
from retrieval import log

TOP_K = 5
PREVIEW_CHARS = 400

# Dense retrieval always returns the nearest neighbours, however far away they
# are - an off-topic query still yields its "best" match. Without a floor the
# model receives irrelevant context and is invited to hallucinate from it.
# 0.50 cleanly separates genuine paraphrase matches (~0.60-0.80 observed) from
# unrelated queries (~0.48) on the current corpus, but it is calibrated against
# a handful of fixtures - retune it once the real PDF corpus is ingested.
MIN_SIMILARITY = 0.50

server = MCPServer(
    name="rag_server",
    version="0.2.0",
    instructions=(
        "Semantic search over the local document corpus. Call search_documents "
        "to find relevant passages, then read the chunk://<id> resource for any "
        "result whose full text you need. Answer only from retrieved content."
    ),
)


def _collection_or_none():
    """Open the collection, or return None if the corpus was never ingested."""
    try:
        return retrieval.get_collection()
    except Exception as exc:  # chromadb raises different types across versions
        log(f"collection unavailable: {exc}")
        return None


@server.tool(
    name="search_documents",
    description=(
        "Semantic search over the local document corpus. Returns up to 5 "
        "passages ranked by embedding similarity, each tagged with a chunk_id "
        "that can be read in full via the chunk://<chunk_id> resource. Unlike "
        "keyword search this matches on meaning, so paraphrased queries work."
    ),
)
def search_documents(query: str) -> list[str]:
    """Vector search over the ingested corpus.

    Args:
        query: Free-text question or topic. Embedded with the same model used
            at ingest time, then matched by cosine similarity.

    Returns:
        Up to TOP_K formatted passages, closest match first. On an empty or
        missing index a single explanatory string is returned rather than an
        empty list, so the calling model gets a usable signal instead of
        silence.
    """
    if not query or not query.strip():
        return ["Empty query - provide a question or topic to search for."]

    collection = _collection_or_none()
    if collection is None:
        return [
            "The vector store has not been built yet. Run scripts/ingest.py to "
            "chunk and embed data/raw_docs into data/chroma_db."
        ]

    count = collection.count()
    if count == 0:
        return ["The vector store is empty. Run scripts/ingest.py to populate it."]

    query_embedding = retrieval.embed([query])[0]
    response = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(TOP_K, count),
        include=["documents", "metadatas", "distances"],
    )

    ids = response["ids"][0]
    documents = response["documents"][0]
    metadatas = response["metadatas"][0]
    distances = response["distances"][0]

    log(f"query={query!r} matched {len(ids)} of {count} chunk(s)")

    results: list[str] = []
    best_rejected = 0.0
    for chunk_id, document, metadata, distance in zip(ids, documents, metadatas, distances):
        # Chroma returns cosine *distance*; similarity reads better in context.
        similarity = 1.0 - float(distance)
        if similarity < MIN_SIMILARITY:
            best_rejected = max(best_rejected, similarity)
            continue
        preview = " ".join(document.split())
        if len(preview) > PREVIEW_CHARS:
            preview = preview[:PREVIEW_CHARS] + "..."
        results.append(
            f"[similarity {similarity:.3f}] {metadata.get('source', '?')} "
            f"(chunk_id: {chunk_id})\n{preview}"
        )

    if not results:
        log(f"all matches below floor {MIN_SIMILARITY} (best {best_rejected:.3f})")
        return [
            f"No passage in the corpus is relevant to {query!r} "
            f"(best similarity {best_rejected:.3f}, below the {MIN_SIMILARITY} "
            "relevance floor). Answer that the corpus does not cover this."
        ]

    return results


@server.resource(
    "chunk://{chunk_id}",
    name="corpus_chunk",
    description=(
        "Full untruncated text of a single ingested chunk, addressed by the "
        "chunk_id returned from search_documents."
    ),
    mime_type="text/plain",
)
def read_chunk(chunk_id: str) -> str:
    """Return one chunk verbatim, so the host can expand a search preview."""
    collection = _collection_or_none()
    if collection is None:
        return "The vector store has not been built yet. Run scripts/ingest.py."

    result = collection.get(ids=[chunk_id], include=["documents", "metadatas"])
    documents = result.get("documents") or []
    if not documents:
        log(f"chunk not found: {chunk_id}")
        return f"No chunk with id {chunk_id!r}."

    metadata = (result.get("metadatas") or [{}])[0] or {}
    source = metadata.get("source", "unknown")
    index = metadata.get("chunk_index", "?")
    log(f"served chunk {chunk_id}")
    return f"source: {source}\nchunk_index: {index}\n\n{documents[0]}"


@server.prompt(
    name="rag_answer",
    description=(
        "Template instructing the model to answer a question strictly from "
        "passages retrieved via search_documents, and to say so when the "
        "corpus does not support an answer."
    ),
)
def rag_answer(question: str) -> str:
    """Grounded-answer prompt.

    Kept deliberately strict about abstaining: with a 4B model and a small
    corpus, an ungrounded confident answer is the most likely failure mode
    (HLD.md section 8).
    """
    return (
        "You are answering from a local document corpus.\n\n"
        f"Question: {question}\n\n"
        "Instructions:\n"
        "1. Call search_documents with a focused query drawn from the question.\n"
        "2. If a result looks relevant but is truncated, read its "
        "chunk://<chunk_id> resource for the full text.\n"
        "3. Answer using only the retrieved passages. Cite the source filename "
        "for each claim.\n"
        "4. If the passages do not contain the answer, say so plainly instead "
        "of guessing. Do not use outside knowledge."
    )


if __name__ == "__main__":
    log(f"starting on stdio; corpus={retrieval.RAW_DOCS_DIR} store={retrieval.CHROMA_DIR}")
    server.run("stdio")
