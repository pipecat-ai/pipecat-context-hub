"""Pre-compute K-means clusters from UMAP 3D coordinates for LOD visualization.

Produces two files:
  - clusters.json   (~50KB)  — centroids with labels, counts, type breakdown
  - cluster_members.json (~8MB) — full point data grouped by cluster ID
"""

import json
import os
import time
from collections import Counter

import numpy as np
from sklearn.cluster import MiniBatchKMeans

PUBLIC_DIR = os.path.join(os.path.dirname(__file__), "..", "public")
INPUT = os.path.join(PUBLIC_DIR, "embeddings_3d.json")
OUT_CLUSTERS = os.path.join(PUBLIC_DIR, "clusters.json")
OUT_MEMBERS = os.path.join(PUBLIC_DIR, "cluster_members.json")

N_CLUSTERS = 200


def top_n(counter: Counter, n: int = 3) -> list[str]:
    return [k for k, _ in counter.most_common(n) if k]


def label_cluster(members: list[dict]) -> str:
    """Generate a human-readable label for a cluster."""
    # Try class names first
    classes = Counter(m["class_name"] for m in members if m.get("class_name"))
    if classes:
        top = top_n(classes, 2)
        if top:
            return ", ".join(top)

    # Try module paths (last 2 segments)
    modules = Counter()
    for m in members:
        mp = m.get("module_path", "")
        if mp:
            parts = mp.split(".")
            modules[".".join(parts[-2:])] += 1
    if modules:
        top = top_n(modules, 2)
        if top:
            return ", ".join(top)

    # Try path-based labels
    paths = Counter()
    for m in members:
        p = m.get("path", "")
        if "/" in p:
            # Use parent directory name
            parts = p.strip("/").split("/")
            if len(parts) >= 2:
                paths[parts[-2]] += 1
            else:
                paths[parts[0]] += 1
    if paths:
        top = top_n(paths, 2)
        if top:
            return ", ".join(top)

    # Fallback: content type
    ct = Counter(m["content_type"] for m in members)
    return top_n(ct, 1)[0] if ct else "unknown"


def main() -> None:
    print("Loading embeddings_3d.json...")
    with open(INPUT) as f:
        data = json.load(f)
    points = data["points"]
    N = len(points)
    print(f"  {N:,} points loaded")

    # Extract 3D coords
    coords = np.array([[p["x"], p["y"], p["z"]] for p in points], dtype=np.float32)

    # K-means clustering
    print(f"Running MiniBatchKMeans (k={N_CLUSTERS})...")
    t0 = time.time()
    kmeans = MiniBatchKMeans(
        n_clusters=N_CLUSTERS,
        random_state=42,
        batch_size=2048,
        n_init=3,
    )
    labels = kmeans.fit_predict(coords)
    centroids = kmeans.cluster_centers_
    elapsed = time.time() - t0
    print(f"  Done in {elapsed:.1f}s")

    # Build cluster summaries
    cluster_map: dict[int, list[int]] = {}
    for i, lbl in enumerate(labels):
        cluster_map.setdefault(int(lbl), []).append(i)

    clusters = []
    all_members: dict[int, list[dict]] = {}

    for cid in range(N_CLUSTERS):
        member_indices = cluster_map.get(cid, [])
        if not member_indices:
            continue

        members = [points[i] for i in member_indices]

        # Content type breakdown
        ct_counts = Counter(m["content_type"] for m in members)

        # Chunk type breakdown (source only)
        st_counts = Counter(
            m["chunk_type"] for m in members
            if m.get("chunk_type")
        )

        # Repo breakdown
        repo_counts = Counter(m["repo"] for m in members if m.get("repo"))

        # Representative label
        lbl_text = label_cluster(members)

        # Compute spread (avg distance from centroid)
        member_coords = coords[member_indices]
        dists = np.linalg.norm(member_coords - centroids[cid], axis=1)
        spread = float(np.mean(dists))

        clusters.append({
            "id": cid,
            "x": float(centroids[cid, 0]),
            "y": float(centroids[cid, 1]),
            "z": float(centroids[cid, 2]),
            "count": len(members),
            "spread": round(spread, 4),
            "label": lbl_text,
            "content_types": dict(ct_counts),
            "chunk_types": dict(st_counts) if st_counts else {},
            "top_repos": dict(repo_counts.most_common(3)),
        })

        # Store full member data for expansion
        all_members[cid] = members

    # Sort by count descending
    clusters.sort(key=lambda c: c["count"], reverse=True)

    # Write clusters summary
    cluster_export = {
        "total_points": N,
        "n_clusters": len(clusters),
        "clusters": clusters,
    }
    with open(OUT_CLUSTERS, "w") as f:
        json.dump(cluster_export, f)
    size_kb = os.path.getsize(OUT_CLUSTERS) / 1024
    print(f"Wrote {OUT_CLUSTERS} ({size_kb:.0f} KB, {len(clusters)} clusters)")

    # Write members grouped by cluster
    # Convert member positions to use centroid-relative offsets for compression
    members_export: dict[str, list[dict]] = {}
    for cid, members in all_members.items():
        members_export[str(cid)] = members

    with open(OUT_MEMBERS, "w") as f:
        json.dump(members_export, f)
    size_mb = os.path.getsize(OUT_MEMBERS) / (1024 * 1024)
    print(f"Wrote {OUT_MEMBERS} ({size_mb:.1f} MB)")

    # Print top clusters
    print(f"\nTop 15 clusters:")
    for c in clusters[:15]:
        ct_str = ", ".join(f"{k}:{v}" for k, v in c["content_types"].items())
        print(f"  #{c['id']:3d}  {c['count']:5d} pts  [{ct_str}]  \"{c['label']}\"")


if __name__ == "__main__":
    main()
