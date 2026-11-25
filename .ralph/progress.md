# Progress Log

> Updated by the agent after significant work.

## Summary

- Iterations completed: 12
- Current status: Anonymise running (sustained ~5.9 chunks/s, ETA ~7 days)

## Session History

### Session 2026-11-19 — Apple FM investigation + performance recovery

**Context:** Anonymise stage had corrupted 68.9% of chunk text (mid-word
placeholder splicing from unguarded `str.replace()` pre-pass + junk PII
values from Apple FM). Corpus repaired from source; pipeline restarted.

**Findings (Apple FM):**
- Apple FM runs fully on-device; `asyncio.wait_for` DOES cancel hung calls
  correctly (native task cancelled via FMTaskCancel) — earlier "hangs" were
  mostly the 5s timeout being too aggressive on real 1200-char chunks
  (real calls take 2.8–5.7s; test text took 1–3s)
- timeout=10s → 0/6 timeouts vs 2/4 at timeout=5s
- Parallel scaling on real chunks: 4w=0.7/s, 8w=1.0/s, 12w=1.2/s (58% ok),
  16w=1.3/s (33% ok — overloaded). **8 workers = sweet spot**
- Ollama fallback (qwen2.5:7b) serialises (~13s/call); OLLAMA_NUM_PARALLEL
  would help but requires Ollama restart (disruptive to other sessions)

**Fixes shipped (commit b54cd81, pushed to GitHub):**
- [x] Repair script restored 17,607 corrupted chunks from source (deterministic
      re-extract; 802 chunk-count mismatches skipped)
- [x] pii_map cleared (was 97% invalid-kind junk, 921 short corruption vectors)
- [x] Word-boundary guard on known-PII pre-pass (was bare str.replace)
- [x] Kind validation (invalid → "other"), junk rejection (len<3, len>120,
      contains "[[")
- [x] Multi-language common-words prefilter (EN/FR/DE/ES ~400 words):
      LLM trigger rate 87% → ~30% on mixed content, 2.4% on English-heavy
- [x] German-noun heuristic: chunks with 3+ German function words skip
      2-word capitalized phrases (German capitalises all nouns); Frau/Herr
      added to title patterns
- [x] Phone digit guard (9+ digits) rejects year sequences
- [x] Parallel anonymise: 8-worker ThreadPool, per-thread SQLite manifests,
      PII batch-written outside hot path
- [x] add-path / remove-path CLI commands (--purge option)
- [x] Visual progress monitor TUI (bar, ETA, spinner, ingestion paths,
      a/d add/remove-path controls) on Desktop
- [x] Supervisor with stall detection (600s) + auto-restart

**Measured performance:**
| Config | Rate |
|---|---|
| Original (sequential, 87% trigger) | ~0.2 chunks/s |
| Final (8 workers, 30% trigger, FM 10s) | **~5.9 chunks/s sustained** |

**Current state:**
- chunks: 177,542 anonymised / 3,786,286 pending / 47 quarantined
- files: 951 anonymised, 17,157 extracted (awaiting), 8,732 deduped
- Supervisor PID varies (auto-restarts); logs: /tmp/anonymise.log,
  /tmp/supervisor.log

### Session 2026-11-20 — Ollama hang root-caused and fixed (commit 49e3502)

**Symptom:** pipeline froze at 270,342 chunks, 0% CPU, 10+ min no progress.

**Diagnosis (macOS `sample`):** worker thread blocked in `sock_recv` →
`__recvfrom` (Ollama HTTP read, no timeout); main thread blocked in
`lock_PyThread_acquire_lock` waiting on that future via as_completed().

**Root cause:** `_ollama_find_pii` called `ollama.chat` directly — the
timeout wrapper had been lost in an earlier revert. Default ollama Client
uses httpx `timeout=None` → infinite socket read on a stalled connection.

**Fix:**
- [x] Shared `ollama.Client(timeout=60)` — stalled sockets raise at HTTP level
- [x] Daemon-thread hard guard (45s) abandons stuck calls, tier fallback proceeds
- [x] Verified: 2.9s/call after fix; pipeline resumed at ~6.7 chunks/s, 98.7% prefilter-clean

### Session 2026-11-21 — Pipeline restart after reboot

- [x] Found anonymise stopped at 423,049 chunks (last DB write 12:23:38 HKT); machine rebooted ~14:38 HKT and `/tmp` logs were lost.
- [x] Changed supervisor log path from `/tmp/anonymise.log` to durable `logs/anonymise.log` under project root.
- [x] Restarted supervisor (PID 85999) with stdout to `logs/supervisor.log`; worker PID 86066 at ~326% CPU.
- [x] Verified progress: 423,049 -> 423,492 chunks in 45s (~9.8 chunks/s), batch tiers 100% prefilter-clean.

### Session 2026-11-22 — Coordinated restart after GS-564 extraction retry

- [x] Diagnosed apparent stall: supervisor was intentionally stopped (`[supervisor] stopping`, no `STALLED` marker) so Paperclip CTO could run GS-564 extraction retry.
- [x] Confirmed PDF retry complete: `logs/gs564_pdf_crawl3.log` ends `crawl: {'discovered': 150, 'extracted': 60, 'failed': 4, 'skipped': 0, 'deferred': 0}`; no active crawl/requeue processes; backup `.ingest-state/manifest.db.bak-20261122-131034`.
- [x] Verified CTO work: `tests/test_retry_and_ocr.py` → 7 passed; `git diff --check` clean.
- [x] Committed CTO changes (bounded extraction retry + German OCR) before restart to preserve audit trail.
- [x] Remaining `extraction_failed` by ext: docx 1,789; jpg 910; pptx 766; txt 135; gif 100; pdf 86; png 63; jpeg 20; md 10. Coordinate further retries before any future stop.

### Session 2026-11-25 — A1/A2 branch split, PII purge, STATUS scan fix

- [x] A1 moved off master onto `experimental/a1-local-optimisation` (now `0f9f2ee`); master force-pushed back to `fabdab1`; local backup ref `backup/master-before-experimental` kept.
- [x] A2 note committed in PAI repo: `DOCS_DIR/MFL_Brain_CloudAFM_MCP_RAG_Notes.md`.
- [x] PII junk purge applied: 3,627 rows deleted from 26,016; backup `.ingest-state/manifest.pii-junk-backup.20261125-172522.db` (4.0 GB).
- [x] Root cause of post-restart stall found via `sample`: main thread stuck in `sqlite3_step` → `pread` doing `SELECT status, COUNT(*) FROM chunks GROUP BY status` from `render_status()`, called every 50 chunks. Fixed in `0f9f2ee` — STATUS.md now written once at stage end.
- [x] Full test suite: 45 passed.
- [ ] Verify rate after supervisor integrity guard completes and worker restarts.

**Next steps (pending user go-ahead):**
- [ ] Monitor anonymise to completion (~7 days)
- [ ] Run synthesise (gemma4:12b-mxfp8, per-doc-type routing + MOCs)
- [ ] Run index (paraphrase-multilingual, 768-dim, sqlite-vec)
- [ ] Verify student-name redaction quality on STMS exam-paper sample
- [ ] Optional: OLLAMA_NUM_PARALLEL=4 restart to speed Ollama fallbacks

## How This Works

Progress is tracked in THIS FILE, not in LLM context.
When context is rotated (fresh agent), the new agent reads this file.
This is how Ralph maintains continuity across iterations.
