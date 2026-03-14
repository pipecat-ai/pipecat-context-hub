"""Extract dashboard statistics from ChromaDB and export as JSON for index.html."""

import json
import os
from collections import Counter
from statistics import mean

import chromadb


PUBLIC_DIR = os.path.join(os.path.dirname(__file__), "..", "public")
OUTPUT = os.path.join(PUBLIC_DIR, "dashboard_data.json")


def main() -> None:
    data_dir = os.path.expanduser("~/.pipecat-context-hub")
    print("Connecting to ChromaDB...")
    client = chromadb.PersistentClient(path=os.path.join(data_dir, "chroma"))
    col = client.get_collection("latest")
    total = col.count()
    print(f"Collection has {total:,} records")

    # Fetch all metadata + documents in batches
    batch_size = 5000
    all_meta: list[dict] = []
    all_docs: list[str] = []

    for offset in range(0, total, batch_size):
        print(f"  Fetching {offset}–{min(offset + batch_size, total)}...")
        result = col.get(
            limit=batch_size,
            offset=offset,
            include=["metadatas", "documents"],
        )
        all_meta.extend(result["metadatas"])
        all_docs.extend(result["documents"])

    print(f"Fetched {len(all_meta):,} records")

    # Content type counts
    ct_counts = Counter(m.get("content_type", "unknown") for m in all_meta)

    # Chunk type counts (source only)
    chunk_type_counts = Counter(
        m.get("chunk_type", "")
        for m in all_meta
        if m.get("content_type") == "source" and m.get("chunk_type")
    )

    # Distinct repos
    repos = set(m.get("repo", "") for m in all_meta if m.get("repo"))

    # Repo × content type breakdown (for treemap)
    repo_ct: dict[str, Counter] = {}
    for m in all_meta:
        repo = m.get("repo", "unknown")
        ctype = m.get("content_type", "unknown")
        repo_ct.setdefault(repo, Counter())[ctype] += 1

    treemap = []
    for repo, counts in repo_ct.items():
        for ctype, count in counts.most_common():
            # Map "unknown" repo (docs) to docs.pipecat.ai
            display_repo = "docs.pipecat.ai" if repo == "unknown" else repo
            treemap.append({"repo": display_repo, "type": ctype, "count": count})
    treemap.sort(key=lambda d: d["count"], reverse=True)

    # Method line distribution
    bins = [0] * 8  # 1-10, 11-20, 21-30, 31-40, 41-50, 51-75, 76-100, 100+
    bin_labels = ["1-10", "11-20", "21-30", "31-40", "41-50", "51-75", "76-100", "100+"]
    method_count = 0

    for i, m in enumerate(all_meta):
        if m.get("chunk_type") == "method":
            method_count += 1
            doc = all_docs[i] or ""
            lines = doc.count("\n") + 1
            if lines <= 10:
                bins[0] += 1
            elif lines <= 20:
                bins[1] += 1
            elif lines <= 30:
                bins[2] += 1
            elif lines <= 40:
                bins[3] += 1
            elif lines <= 50:
                bins[4] += 1
            elif lines <= 75:
                bins[5] += 1
            elif lines <= 100:
                bins[6] += 1
            else:
                bins[7] += 1

    cumulative = []
    s = 0
    for b in bins:
        s += b
        cumulative.append(s)

    pct_under_10 = round(bins[0] / method_count * 100, 1) if method_count else 0
    pct_under_100 = (
        round((method_count - bins[7]) / method_count * 100, 1) if method_count else 0
    )

    # Chunk size stats per content type
    sizes: dict[str, list[int]] = {"doc": [], "code": [], "source": []}
    for i, m in enumerate(all_meta):
        ct = m.get("content_type", "unknown")
        if ct in sizes:
            sizes[ct].append(len(all_docs[i] or ""))

    chunk_sizes = {}
    for ct in ["doc", "code", "source"]:
        vals = sizes[ct]
        if vals:
            chunk_sizes[ct] = {
                "min": min(vals),
                "avg": round(mean(vals)),
                "max": max(vals),
            }

    # Build export
    export = {
        "total": total,
        "repos": len(repos),
        "content_types": dict(ct_counts),
        "chunk_types": dict(chunk_type_counts),
        "methods": method_count,
        "docs": ct_counts.get("doc", 0),
        "source_chunks": ct_counts.get("source", 0),
        "treemap": treemap,
        "method_histogram": {
            "labels": bin_labels,
            "data": bins,
            "cumulative": cumulative,
            "pct_under_10": pct_under_10,
            "pct_under_100": pct_under_100,
        },
        "chunk_sizes": chunk_sizes,
    }

    with open(OUTPUT, "w") as f:
        json.dump(export, f, indent=2)

    size_kb = os.path.getsize(OUTPUT) / 1024
    print(f"Wrote {OUTPUT} ({size_kb:.1f} KB)")
    print(f"  {total:,} chunks across {len(repos)} repos")
    print(f"  Content types: {dict(ct_counts)}")
    print(f"  Methods: {method_count:,}")


if __name__ == "__main__":
    main()
