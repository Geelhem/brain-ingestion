"""Crawler: walks configured sources, extracts text, chunks, checkpoints."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import sys
import time
from pathlib import Path

from .config import Config, Source
from .extract import ExtractionError, extract_text
from .manifest import Manifest

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150


def sha256_file(path: Path, bufsize: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunk = text[start:end]
        if end < len(text):
            for sep in ("\n\n", ". ", "\n", " "):
                idx = chunk.rfind(sep)
                if idx > size // 2:
                    end = start + idx + len(sep)
                    chunk = text[start:end]
                    break
        chunks.append(chunk.strip())
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def _included(path: Path, src: Source) -> bool:
    p = str(path)
    if src.include_globs and not any(
        fnmatch.fnmatch(p, str(Path(src.path) / g)) or fnmatch.fnmatch(p, g)
        for g in src.include_globs
    ):
        return False
    for g in src.exclude_globs:
        if fnmatch.fnmatch(p, str(Path(src.path) / g)) or fnmatch.fnmatch(p, g):
            return False
    return True


def _detect_doc_type(path: Path, routing: dict) -> str:
    """Detect document type from path keywords."""
    p = str(path).lower()
    for dtype, rule in routing.items():
        for kw in rule.path_keywords:
            if kw.lower() in p:
                return dtype
    return "resource"


def _extract_with_retry(fpath: Path, ocr_config: dict | None, attempts: int = 3,
                        base_delay: float = 2.0, quick: bool = False) -> str:
    """Hydrate FileProvider placeholders (streaming download), then extract.

    quick=True (walk pass): short hydration budget — leave the patient work
    to the retry pass so discovery stays fast.
    """
    from .hydrate import ensure_hydrated, hydrate, is_dataless

    if is_dataless(fpath):
        if quick:
            # brief single attempt; if not instantly available, defer to retry pass
            import errno as _errno
            raise OSError(_errno.EAGAIN, "hydration deferred (placeholder, walk pass)")
        ensure_hydrated(fpath)

    last: Exception | None = None
    for i in range(attempts):
        try:
            return extract_text(fpath, ocr_config)
        except (OSError, ExtractionError) as exc:
            msg = str(exc)
            transient = "Resource deadlock" in msg or "deadlock avoided" in msg.lower() or errno_eq(exc)
            last = exc
            if not transient or i == attempts - 1:
                raise
            time.sleep(base_delay * (2 ** i))
    raise last  # pragma: no cover


def errno_eq(exc: Exception) -> bool:
    return getattr(exc, "errno", None) in (11, 35, 36)  # EAGAIN, EAGAIN(darwin), ERANGE-ish


def crawl(cfg: Config, manifest: Manifest, only_source: str | None = None,
          limit: int | None = None, verbose: bool = True,
          walk: bool = True, delay_ms: int = 100) -> dict[str, int]:
    stats = {"discovered": 0, "extracted": 0, "failed": 0, "skipped": 0, "deferred": 0}
    sources = [s for s in cfg.sources if s.enabled and (only_source is None or s.name == only_source)]
    ocr_config = cfg.ocr
    routing = cfg.document_type_routing
    for src in sources:
        root = Path(src.path)
        eff_delay = src.delay_ms if src.delay_ms is not None else delay_ms
        last_heartbeat = time.time()
        if walk:
            if not root.exists():
                if verbose:
                    print(f"[crawl] source not found (placeholder or unmounted): {root}", file=sys.stderr)
                continue
            try:
                next(root.iterdir())
            except PermissionError:
                print(f"[crawl] PERMISSION DENIED: {root}\n"
                      f"  → Grant Full Disk Access to your terminal app "
                      f"(System Settings → Privacy & Security → Full Disk Access), then re-run.",
                      file=sys.stderr)
                stats["failed"] += 1
                continue
            except OSError as exc:
                print(f"[crawl] source unreadable ({exc}): {root}", file=sys.stderr)
                stats["failed"] += 1
                continue
            last_heartbeat = time.time()
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fname in filenames:
                    if time.time() - last_heartbeat >= 60:
                        print(f"[crawl] {src.name}: walking... {stats['discovered']} new, "
                              f"{stats['extracted']} ok, {stats['failed']} failed, "
                              f"{stats['skipped']} already-known — dir: {dirpath[-60:]}",
                              file=sys.stderr)
                        last_heartbeat = time.time()
                    fpath = Path(dirpath) / fname
                    if not _included(fpath, src):
                        continue
                    if limit and stats["discovered"] >= limit:
                        return stats
                    row = manifest.get(str(fpath))
                    if row is not None:
                        # Walk = discovery only. Retrying failed files is the
                        # job of no-walk mode (paced, ordered by updated_at).
                        stats["skipped"] += 1
                        continue
                    stats["discovered"] += 1
                    try:
                        st = fpath.stat()
                        doc_type = _detect_doc_type(fpath, cfg.document_type_routing)
                        manifest.upsert_file(str(fpath), src.name, st.st_size, st.st_mtime)
                        manifest.set_field(str(fpath), "doc_type", doc_type)
                        manifest.set_status(str(fpath), "discovered")
                        text = _extract_with_retry(fpath, cfg.ocr, attempts=1, quick=True)
                        fhash = sha256_file(fpath)
                        manifest.set_field(str(fpath), "sha256", fhash)
                        if not text or len(text.strip()) < 40:
                            manifest.set_status(str(fpath), "extracted")
                            continue
                        manifest.add_chunks(str(fpath), chunk_text(text))
                        manifest.set_field(str(fpath), "content_sha256",
                                           hashlib.sha256(text.encode()).hexdigest())
                        manifest.set_status(str(fpath), "extracted")
                        stats["extracted"] += 1
                    except PermissionError as exc:
                        manifest.set_status(str(fpath), "failed", error=f"permission: {exc}"[:400])
                        stats["failed"] += 1
                    except (OSError, ExtractionError) as exc:
                        msg = str(exc)
                        transient = errno_eq(exc) or "deadlock" in msg.lower() or "hydration" in msg.lower()
                        if transient:
                            # stays 'discovered' — the patient retry pass will hydrate it
                            stats["deferred"] += 1
                        else:
                            manifest.set_status(str(fpath), "failed", error=msg[:400])
                            stats["failed"] += 1
                    except Exception as exc:  # noqa: BLE001 — corrupt files, odd formats
                        manifest.set_status(str(fpath), "failed", error=str(exc)[:400])
                        stats["failed"] += 1
                    if eff_delay:
                        time.sleep(eff_delay / 1000.0)
        else:
            rows = manifest.conn.execute(
                "SELECT path FROM files WHERE source=? AND status IN ('failed','discovered') "
                "ORDER BY updated_at" + (f" LIMIT {int(limit)}" if limit else ""),
                (src.name,),
            ).fetchall()
            for r in rows:
                fpath = Path(r["path"])
                if limit and stats["discovered"] >= limit:
                    return stats
                stats["discovered"] += 1
                if time.time() - last_heartbeat >= 60:
                    print(f"[crawl] {src.name}: retrying... {stats['discovered']} tried, "
                          f"{stats['extracted']} ok, {stats['failed']} still-failing — "
                          f"current: {fpath.name[:60]}", file=sys.stderr)
                    last_heartbeat = time.time()
                try:
                    manifest.set_status(str(fpath), "discovered")
                    text = _extract_with_retry(fpath, cfg.ocr, attempts=cfg.crawl.get("extract_attempts", 3))
                    manifest.set_field(str(fpath), "sha256", sha256_file(fpath))
                    if not text or len(text.strip()) < 40:
                        manifest.set_status(str(fpath), "extracted")
                        continue
                    old = manifest.conn.execute(
                        "SELECT COUNT(*) FROM chunks WHERE file_path=?", (str(fpath),)
                    ).fetchone()[0]
                    if not old:
                        manifest.add_chunks(str(fpath), chunk_text(text))
                    manifest.set_field(str(fpath), "content_sha256",
                                       hashlib.sha256(text.encode()).hexdigest())
                    manifest.set_status(str(fpath), "extracted")
                    stats["extracted"] += 1
                except PermissionError as exc:
                    manifest.record_failure(str(fpath), f"permission: {exc}")
                    stats["failed"] += 1
                except Exception as exc:  # noqa: BLE001
                    manifest.record_failure(str(fpath), str(exc))
                    stats["failed"] += 1
                if eff_delay:
                    time.sleep(eff_delay / 1000.0)
    return stats