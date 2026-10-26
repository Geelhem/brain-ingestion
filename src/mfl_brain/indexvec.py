"""sqlite-vec embedding index over anonymised chunks — future website API layer."""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

from .manifest import Manifest


def vec_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def build_index(manifest: Manifest, cfg, limit: int | None = None) -> int:
    import ollama
    import sqlite_vec

    vault = cfg.vault_path
    db_path = vault / ".ingest-state" / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    model = cfg.embeddings.get("ollama_model", "paraphrase-multilingual")

    idx = sqlite3.connect(str(db_path))
    idx.enable_load_extension(True)
    sqlite_vec.load(idx)
    idx.enable_load_extension(False)
    dims = int(cfg.embeddings.get("dimensions", 768))
    idx.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0("
        f"  chunk_id INTEGER PRIMARY KEY, embedding float[{dims}]"
        f")"
    )
    idx.execute(
        "CREATE TABLE IF NOT EXISTS chunk_meta ("
        " chunk_id INTEGER PRIMARY KEY, file_path TEXT, seq INTEGER,"
        " text TEXT, note_path TEXT)"
    )

    rows = manifest.conn.execute(
        "SELECT chunk_id, file_path, seq, text FROM chunks WHERE status='anonymised' "
        "ORDER BY chunk_id" + (f" LIMIT {int(limit)}" if limit else "")
    ).fetchall()
    done = manifest.conn.execute("SELECT chunk_id FROM chunks WHERE status='indexed'").fetchall()
    done_ids = {r[0] for r in done}
    count = 0
    for r in rows:
        if r["chunk_id"] in done_ids:
            continue
        file_row = manifest.get(r["file_path"])
        note_path = file_row["note_path"] if file_row else None
        if file_row and file_row["canonical_of"]:
            continue  # dedup: index canonical only
        resp = ollama.embed(model=model, input=r["text"][:2000])
        vec = resp["embeddings"][0]
        idx.execute(
            "INSERT INTO chunks_vec(chunk_id, embedding) VALUES(?,?)",
            (r["chunk_id"], vec_to_blob(vec)),
        )
        idx.execute(
            "INSERT OR REPLACE INTO chunk_meta(chunk_id, file_path, seq, text, note_path) "
            "VALUES(?,?,?,?,?)",
            (r["chunk_id"], r["file_path"], r["seq"], r["text"], note_path),
        )
        manifest.set_chunk_status(r["chunk_id"], "indexed")
        count += 1
    idx.commit()
    idx.close()
    return count


def query(cfg, manifest: Manifest, question: str, k: int = 5) -> list[dict]:
    import ollama
    import sqlite_vec

    vault = cfg.vault_path
    db_path = vault / ".ingest-state" / "index.db"
    if not db_path.exists():
        return []
    model = cfg.embeddings.get("ollama_model", "paraphrase-multilingual")
    resp = ollama.embed(model=model, input=question)
    qvec = vec_to_blob(resp["embeddings"][0])
    idx = sqlite3.connect(str(db_path))
    idx.enable_load_extension(True)
    sqlite_vec.load(idx)
    idx.enable_load_extension(False)
    rows = idx.execute(
        "SELECT chunk_id, distance FROM chunks_vec WHERE embedding MATCH ? AND k=?",
        (qvec, k),
    ).fetchall()
    out = []
    for chunk_id, dist in rows:
        meta = idx.execute(
            "SELECT file_path, seq, text, note_path FROM chunk_meta WHERE chunk_id=?",
            (chunk_id,),
        ).fetchone()
        if meta:
            out.append({"distance": dist, "file": meta[0], "seq": meta[1],
                        "text": meta[2], "note": meta[3]})
    idx.close()
    return out
