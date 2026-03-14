"""Extract embeddings from ChromaDB, reduce to 3D with UMAP, export JSON for visualization."""

import json
import os
import sys
import time

import chromadb
import numpy as np
import umap


def main() -> None:
    data_dir = os.path.expanduser("~/.pipecat-context-hub")
    out_path = os.path.join(os.path.dirname(__file__), "..", "public", "embeddings_3d.json")

    print("Connecting to ChromaDB...")
    client = chromadb.PersistentClient(path=os.path.join(data_dir, "chroma"))
    col = client.get_collection("latest")
    total = col.count()
    print(f"Collection has {total:,} records")

    # Fetch all embeddings + metadata in batches
    batch_size = 5000
    all_ids: list[str] = []
    all_embeddings: list[list[float]] = []
    all_meta: list[dict] = []
    all_docs: list[str] = []

    for offset in range(0, total, batch_size):
        print(f"  Fetching {offset}–{min(offset + batch_size, total)}...")
        result = col.get(
            limit=batch_size,
            offset=offset,
            include=["embeddings", "metadatas", "documents"],
        )
        all_ids.extend(result["ids"])
        all_embeddings.extend(result["embeddings"])
        all_meta.extend(result["metadatas"])
        all_docs.extend(result["documents"])

    print(f"Fetched {len(all_ids):,} records")

    # Run UMAP 384D → 3D
    print("Running UMAP (384D → 3D)... this may take a minute")
    embeddings_array = np.array(all_embeddings, dtype=np.float32)
    t0 = time.time()
    reducer = umap.UMAP(
        n_components=3,
        n_neighbors=30,
        min_dist=0.1,
        metric="cosine",
        random_state=42,
        verbose=True,
    )
    coords_3d = reducer.fit_transform(embeddings_array)
    elapsed = time.time() - t0
    print(f"UMAP done in {elapsed:.1f}s")

    # Build export data
    points = []
    for i in range(len(all_ids)):
        meta = all_meta[i] or {}
        doc = all_docs[i] or ""
        # Truncate document preview to 200 chars
        preview = doc[:200].replace("\n", " ").strip()
        if len(doc) > 200:
            preview += "..."

        # Extract chunk_type from the document content heuristic or metadata
        chunk_type = meta.get("chunk_type", "")
        content_type = meta.get("content_type", "unknown")

        # For source chunks, try to get chunk_type from the content header
        if content_type == "source" and not chunk_type:
            if doc.startswith("# Module:"):
                chunk_type = "module_overview"
            elif doc.startswith("# Class:"):
                chunk_type = "class_overview"
            elif "." in (doc.split("\n")[0] if doc else ""):
                chunk_type = "method"
            else:
                chunk_type = "function"

        points.append({
            "id": all_ids[i],
            "x": float(coords_3d[i, 0]),
            "y": float(coords_3d[i, 1]),
            "z": float(coords_3d[i, 2]),
            "content_type": content_type,
            "chunk_type": chunk_type,
            "repo": meta.get("repo", ""),
            "path": meta.get("path", ""),
            "class_name": meta.get("class_name", ""),
            "method_name": meta.get("method_name", ""),
            "module_path": meta.get("module_path", ""),
            "preview": preview,
        })

    export = {
        "total": len(points),
        "umap_params": {
            "n_neighbors": 30,
            "min_dist": 0.1,
            "metric": "cosine",
            "n_components": 3,
        },
        "points": points,
    }

    with open(out_path, "w") as f:
        json.dump(export, f)

    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"Wrote {out_path} ({size_mb:.1f} MB, {len(points):,} points)")


if __name__ == "__main__":
    main()
