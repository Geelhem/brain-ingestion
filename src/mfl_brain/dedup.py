"""Dedup: exact content hash + near-duplicate detection via embeddings."""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

from .manifest import Manifest


def mark_exact_duplicates(manifest: Manifest) -> int:
    """Content-hash exact duplicates: first path wins, rest point to it."""
    rows = manifest.conn.execute(
        "SELECT path, content_sha256 FROM files "
        "WHERE content_sha256 IS NOT NULL AND canonical_of IS NULL "
        "ORDER BY path"
    ).fetchall()
    seen: dict[str, str] = {}
    marked = 0
    for r in rows:
        h = r["content_sha256"]
        if h in seen:
            manifest.set_field(r["path"], "canonical_of", seen[h])
            manifest.set_status(r["path"], "deduped")
            marked += 1
        else:
            seen[h] = r["path"]
    return marked


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(x * x for x in b)) or 1e-9
    return dot / (na * nb)


def mark_near_duplicates(manifest: Manifest, ollama_model: str,
                         threshold: float = 0.93, max_pairs: int = 5000) -> int:
    """Embed canonical file first-chunks; near-dupes get canonical_of set."""
    import ollama

    rows = manifest.conn.execute(
        "SELECT path, content_sha256 FROM files WHERE status='extracted' AND canonical_of IS NULL "
        "AND path IN (SELECT DISTINCT file_path FROM chunks WHERE seq=0) "
        "ORDER BY path LIMIT ?", (max_pairs,)
    ).fetchall()
    if not rows:
        return 0
    vecs: dict[str, list[float]] = {}
    paths = [r["path"] for r in rows]
    for p in paths:
        chunk = manifest.conn.execute(
            "SELECT text FROM chunks WHERE file_path=? AND seq=0", (p,)
        ).fetchone()
        if chunk is None:
            continue
        resp = ollama.embed(model=ollama_model, input=chunk["text"][:2000])
        vecs[p] = resp["embeddings"][0]
    marked = 0
    plist = list(vecs)
    for i, p1 in enumerate(plist):
        if marked >= max_pairs:
            break
        for p2 in plist[i + 1:]:
            sim = _cosine(vecs[p1], vecs[p2])
            if sim >= threshold:
                manifest.set_field(p2, "canonical_of", p1)
                manifest.set_status(p2, "deduped")
                marked += 1
    return marked


def skip_noncanonical(manifest: Manifest) -> int:
    """Files whose canonical_of is set but status not yet deduped."""
    rows = manifest.conn.execute(
        "SELECT path FROM files WHERE canonical_of IS NOT NULL AND status='extracted'"
    ).fetchall()
    for r in rows:
        manifest.set_status(r["path"], "deduped")
    return len(rows)
