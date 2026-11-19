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
