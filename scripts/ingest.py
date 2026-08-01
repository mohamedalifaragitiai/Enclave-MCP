"""Ingest data/raw_docs into the ChromaDB store at data/chroma_db (Phase 2).

Chunks every text document in the corpus, embeds the chunks with
BAAI/bge-small-en-v1.5 via fastembed, and writes them to a persistent Chroma
collection that servers/rag_server/server.py then queries.

Run:  .venv\\Scripts\\python.exe scripts\\ingest.py [--reset]

--reset drops the existing collection first. Without it the collection is
upserted, so re-running after editing a document refreshes those chunks in
place; note that chunks deleted by an edit are only pruned with --reset.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# retrieval.py is the single source of truth for chunking and IDs, but the repo
# is a flat layout (no package), so make the server directory importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "servers" / "rag_server"))

import retrieval  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="drop the existing collection before ingesting",
    )
    args = parser.parse_args()

    print(f"corpus     : {retrieval.RAW_DOCS_DIR}")
    print(f"vector store: {retrieval.CHROMA_DIR}")

    documents, skipped = retrieval.iter_documents()
    if skipped:
        print(
            f"skipping {len(skipped)} non-text file(s) - PDF/image extraction "
            f"arrives with docs_server in Phase 4"
        )
    if not documents:
        print(f"ERROR: no text documents found in {retrieval.RAW_DOCS_DIR}", file=sys.stderr)
        return 1

    if args.reset:
        import chromadb
        from chromadb.config import Settings

        client = chromadb.PersistentClient(
            path=str(retrieval.CHROMA_DIR),
            settings=Settings(anonymized_telemetry=False),
        )
        try:
            client.delete_collection(retrieval.COLLECTION_NAME)
            print(f"dropped existing collection '{retrieval.COLLECTION_NAME}'")
        except Exception:
            pass  # collection did not exist; nothing to drop

    collection = retrieval.get_collection(create=True)

    ids: list[str] = []
    texts: list[str] = []
    metadatas: list[dict] = []

    for path, text in documents:
        chunks = retrieval.chunk_text(text)
        print(f"  {path.relative_to(retrieval.RAW_DOCS_DIR)}: {len(chunks)} chunk(s)")
        for index, chunk in enumerate(chunks):
            ids.append(retrieval.make_chunk_id(path, index))
            texts.append(chunk)
            metadatas.append(
                {
                    "source": path.relative_to(retrieval.RAW_DOCS_DIR).as_posix(),
                    "chunk_index": index,
                    "chars": len(chunk),
                }
            )

    print(f"embedding {len(texts)} chunk(s) with {retrieval.EMBED_MODEL} ...")
    embeddings = retrieval.embed(texts)
    print(f"embedding dimension: {len(embeddings[0])}")

    collection.upsert(
        ids=ids,
        documents=texts,
        embeddings=embeddings,
        metadatas=metadatas,
    )

    print(f"\ningested {len(ids)} chunk(s) from {len(documents)} document(s)")
    print(f"collection '{retrieval.COLLECTION_NAME}' now holds {collection.count()} chunk(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
