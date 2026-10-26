"""SQLite manifest — the checkpoint/resume backbone of the pipeline."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    doc_type TEXT,
    size INTEGER,
    mtime REAL,
    sha256 TEXT,
    content_sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'discovered',
    error TEXT,
    canonical_of TEXT,
    note_path TEXT,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_sha ON files(content_sha256);
CREATE INDEX IF NOT EXISTS idx_files_doc_type ON files(doc_type);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    seq INTEGER NOT NULL,
    text TEXT NOT NULL,
    text_sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    tier TEXT,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_status ON chunks(status);

CREATE TABLE IF NOT EXISTS pii_map (
    real_value TEXT PRIMARY KEY,
    placeholder TEXT NOT NULL,
    kind TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    note TEXT
);
"""

STATUSES = [
    "discovered", "extracted", "deduped", "anonymised",
    "synthesised", "indexed", "failed", "quarantined",
]


class Manifest:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # Migration: add tier column if not exists
        cursor = self.conn.execute("PRAGMA table_info(chunks)")
        columns = [row[1] for row in cursor.fetchall()]
        if "tier" not in columns:
            self.conn.execute("ALTER TABLE chunks ADD COLUMN tier TEXT")
            self.conn.commit()
        fcur = self.conn.execute("PRAGMA table_info(files)")
        fcols = [row[1] for row in fcur.fetchall()]
        if "attempts" not in fcols:
            self.conn.execute("ALTER TABLE files ADD COLUMN attempts INTEGER DEFAULT 0")
            self.conn.commit()
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- files ------------------------------------------------------------
    def upsert_file(self, path: str, source: str, size: int, mtime: float) -> None:
        self.conn.execute(
            "INSERT INTO files(path, source, size, mtime, updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(path) DO NOTHING",
            (path, source, size, mtime, time.time()),
        )
        self.conn.commit()

    def set_status(self, path: str, status: str, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE files SET status=?, error=?, updated_at=? WHERE path=?",
            (status, error, time.time(), path),
        )
        self.conn.commit()

    def record_failure(self, path: str, error: str, max_attempts: int = 6) -> bool:
        """Increment attempt count; quarantine if exceeded. Returns True if quarantined.

        6 attempts because FileProvider hydration failures are often transient —
        a placeholder that fails today may download fine once OneDrive catches up.
        """
        self.conn.execute(
            "UPDATE files SET attempts=COALESCE(attempts,0)+1, error=?, updated_at=? WHERE path=?",
            (error[:400], time.time(), path),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT attempts FROM files WHERE path=?", (path,)).fetchone()
        if row and row["attempts"] >= max_attempts:
            self.conn.execute(
                "UPDATE files SET status='quarantined', updated_at=? WHERE path=?",
                (time.time(), path),
            )
            self.conn.commit()
            return True
        return False

    def set_field(self, path: str, field: str, value: str | None) -> None:
        assert field in ("sha256", "content_sha256", "canonical_of", "note_path", "doc_type")
        self.conn.execute(
            f"UPDATE files SET {field}=?, updated_at=? WHERE path=?",
            (value, time.time(), path),
        )
        self.conn.commit()

    def get(self, path: str) -> sqlite3.Row | None:
        cur = self.conn.execute("SELECT * FROM files WHERE path=?", (path,))
        return cur.fetchone()

    def files_with_status(self, status: str, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM files WHERE status=? ORDER BY path"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(self.conn.execute(sql, (status,)))

    def count_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM files GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # -- chunks -------------------------------------------------------------
    def add_chunks(self, file_path: str, chunks: list[str]) -> int:
        now = time.time()
        rows = [
            (file_path, i, text, hashlib.sha256(text.encode()).hexdigest(), "pending", now)
            for i, text in enumerate(chunks)
        ]
        self.conn.executemany(
            "INSERT INTO chunks(file_path, seq, text, text_sha256, status, updated_at) "
            "VALUES(?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def pending_chunks(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM chunks WHERE status='pending' ORDER BY chunk_id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(self.conn.execute(sql))

    def set_chunk_status(self, chunk_id: int, status: str, text: str | None = None, tier: str | None = None) -> None:
        if text is None:
            if tier is None:
                self.conn.execute(
                    "UPDATE chunks SET status=?, updated_at=? WHERE chunk_id=?",
                    (status, time.time(), chunk_id),
                )
            else:
                self.conn.execute(
                    "UPDATE chunks SET status=?, tier=?, updated_at=? WHERE chunk_id=?",
                    (status, tier, time.time(), chunk_id),
                )
        else:
            if tier is None:
                self.conn.execute(
                    "UPDATE chunks SET status=?, text=?, text_sha256=?, updated_at=? WHERE chunk_id=?",
                    (status, text, hashlib.sha256(text.encode()).hexdigest(), time.time(), chunk_id),
                )
            else:
                self.conn.execute(
                    "UPDATE chunks SET status=?, text=?, text_sha256=?, tier=?, updated_at=? WHERE chunk_id=?",
                    (status, text, hashlib.sha256(text.encode()).hexdigest(), tier, time.time(), chunk_id),
                )
        self.conn.commit()

    def chunk_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM chunks GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # -- pii map ------------------------------------------------------------
    def remember_pii(self, real: str, placeholder: str, kind: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO pii_map(real_value, placeholder, kind, created_at) "
            "VALUES(?,?,?,?)",
            (real, placeholder, kind, time.time()),
        )
        self.conn.commit()

    def known_pii(self) -> dict[str, tuple[str, str]]:
        rows = self.conn.execute("SELECT real_value, placeholder, kind FROM pii_map").fetchall()
        return {r["real_value"]: (r["placeholder"], r["kind"]) for r in rows}

    # -- runs -----------------------------------------------------------------
    def start_run(self, stage: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(stage, started_at) VALUES(?,?)", (stage, time.time())
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, note: str = "") -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, note=? WHERE run_id=?",
            (time.time(), note, run_id),
        )
        self.conn.commit()
