"""SQLite manifest — the checkpoint/resume backbone of the pipeline."""

from __future__ import annotations

import hashlib
import re
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

# Total extraction attempts allowed for a file that yields too little text:
# the first attempt plus two retries. Once attempts reaches this bound the
# file stays in `extraction_failed` and is no longer selected for retry.
MAX_EXTRACTION_ATTEMPTS = 3

# --- PII value validation -------------------------------------------------
# The LLM tiers occasionally return prose, placeholders or whitespace blobs
# instead of a real identifier.  These rules keep that junk out of pii_map so
# the known-PII pre-pass cannot "redact" half a sentence on later runs.
MAX_OTHER_PII_LEN = 60
_MIN_PII_LEN = 3

_SENTENCE_START_RE = re.compile(
    r"^(the|this|that|these|those|it|we|they|he|she|i|there|here|a|an)\s+[a-zäöüßéèêàçñ]",
    re.IGNORECASE,
)
_TERMINATOR_RE = re.compile(r"[.!?](?:\s|$)")


def _is_sentence_like(value: str) -> bool:
    """Heuristic: prose rather than a name/email/phone/address."""
    words = value.split()
    if len(words) >= 8:
        return True
    if len(words) >= 4 and _TERMINATOR_RE.search(value):
        return True
    if len(words) >= 3 and _SENTENCE_START_RE.match(value):
        return True
    # Names and addresses are capitalised; all-lowercase multi-word prose is
    # almost always a sentence fragment from a hallucinated LLM result.
    # Exclude digit-heavy values (phone numbers) which are all-lowercase too.
    if (len(words) >= 5 and value == value.lower()
            and any(c.isalpha() for c in value)
            and sum(c.isdigit() for c in value) <= 2):
        return True
    return False


def _is_whitespace_heavy(value: str) -> bool:
    """Reject embedded newlines/tabs, doubled spaces or mostly-whitespace blobs."""
    if "\n" in value or "\t" in value or "  " in value:
        return True
    ws = sum(c.isspace() for c in value)
    return bool(value) and ws / len(value) > 0.3


def _rule_empty(value: str, kind: str) -> bool:
    return not value


def _rule_too_short(value: str, kind: str) -> bool:
    return len(value) < _MIN_PII_LEN


def _rule_placeholder(value: str, kind: str) -> bool:
    return "[[" in value or "]]" in value


def _rule_other_too_long(value: str, kind: str) -> bool:
    return kind == "other" and len(value) > MAX_OTHER_PII_LEN


def _rule_sentence_like(value: str, kind: str) -> bool:
    return _is_sentence_like(value)


def _rule_whitespace_heavy(value: str, kind: str) -> bool:
    return _is_whitespace_heavy(value)


# Ordered: the first matching rule is the "primary" reason.  Kept as a single
# source of truth so remember_pii() and scripts/purge_pii_junk.py cannot drift.
PII_JUNK_RULES = {
    "empty": _rule_empty,
    "too_short": _rule_too_short,
    "placeholder": _rule_placeholder,
    "other_too_long": _rule_other_too_long,
    "sentence_like": _rule_sentence_like,
    "whitespace_heavy": _rule_whitespace_heavy,
}


def pii_junk_rules(real: str | None, kind: str) -> list[str]:
    """Return every junk rule a value matches (a row may match several)."""
    value = (real or "").strip()
    return [name for name, fn in PII_JUNK_RULES.items() if fn(value, kind or "other")]


def pii_junk_reason(real: str | None, kind: str) -> str | None:
    """Return the primary junk rule for a value, or None when it is valid."""
    rules = pii_junk_rules(real, kind)
    return rules[0] if rules else None


class Manifest:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), timeout=60)
        self.conn.row_factory = sqlite3.Row
        # Durability + concurrency. WAL keeps readers live during writes and is
        # persistent in the file; busy_timeout prevents spurious "database is
        # locked" when crawl and anonymise write concurrently.
        self.conn.execute("PRAGMA busy_timeout=60000")
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
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

    def set_status(self, path: str, status: str, error: str | None = None, commit: bool = True) -> None:
        self.conn.execute(
            "UPDATE files SET status=?, error=?, updated_at=? WHERE path=?",
            (status, error, time.time(), path),
        )
        if commit:
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

    def record_extraction_failure(self, path: str, error: str) -> int:
        """Mark a file `extraction_failed` and increment its attempt count.

        Returns the new attempt count. Files are retried only while
        attempts < MAX_EXTRACTION_ATTEMPTS (see crawler no-walk selection).
        """
        self.conn.execute(
            "UPDATE files SET status='extraction_failed', "
            "attempts=COALESCE(attempts,0)+1, error=?, updated_at=? WHERE path=?",
            (error[:400], time.time(), path),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT attempts FROM files WHERE path=?", (path,)).fetchone()
        return int(row["attempts"]) if row and row["attempts"] is not None else 0

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

    def set_chunk_status(self, chunk_id: int, status: str, text: str | None = None, tier: str | None = None, commit: bool = True) -> None:
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
        if commit:
            self.conn.commit()

    def chunk_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM chunks GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # -- pii map ------------------------------------------------------------
    def remember_pii(self, real: str, placeholder: str, kind: str) -> int:
        """Backward-compatible single-entry wrapper around remember_pii_batch."""
        return self.remember_pii_batch([(real, placeholder, kind)])

    def remember_pii_batch(
        self,
        entries: list[tuple[str, str, str]],
        commit: bool = True,
    ) -> int:
        """Validate and persist PII mappings with one executemany + one commit.

        Junk values are skipped (see PII_JUNK_RULES).  A placeholder already
        owned by a *different* real value is replaced with a fresh unique one
        so distinct identifiers never share a placeholder.  Returns the number
        of rows actually inserted.
        """
        now = time.time()
        rows: list[tuple[str, str, str, float]] = []
        claimed: dict[str, str] = {}  # placeholder -> real_value (this batch)
        for real, placeholder, kind in entries:
            if pii_junk_reason(real, kind) is not None:
                continue
            real = real.strip()
            kind = kind or "other"
            placeholder = self._resolve_placeholder(real, placeholder, kind, claimed)
            claimed[placeholder] = real
            rows.append((real, placeholder, kind, now))
        if not rows:
            return 0
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO pii_map(real_value, placeholder, kind, created_at) "
            "VALUES(?,?,?,?)",
            rows,
        )
        if commit:
            self.conn.commit()
        return self.conn.total_changes - before

    def _resolve_placeholder(
        self,
        real: str,
        placeholder: str,
        kind: str,
        claimed: dict[str, str],
    ) -> str:
        """Return a placeholder not already owned by a different real value."""
        owner = claimed.get(placeholder)
        if owner is None:
            row = self.conn.execute(
                "SELECT real_value FROM pii_map WHERE placeholder=?", (placeholder,)
            ).fetchone()
            owner = row["real_value"] if row else None
        if owner is not None and owner != real:
            return self._unique_placeholder(placeholder, claimed)
        return placeholder

    def _unique_placeholder(self, placeholder: str, claimed: dict[str, str]) -> str:
        """Allocate the next free [[PREFIX-NNNN]] for a colliding placeholder."""
        m = re.match(r"\[\[([A-Za-z]+)-", placeholder or "")
        prefix = m.group(1) if m else "MISC"
        used = set(claimed)
        for row in self.conn.execute(
            "SELECT placeholder FROM pii_map WHERE placeholder LIKE ?", (f"[[{prefix}-%",)
        ):
            used.add(row["placeholder"])
        n = 1
        while f"[[{prefix}-{n:04d}]]" in used:
            n += 1
        return f"[[{prefix}-{n:04d}]]"

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
