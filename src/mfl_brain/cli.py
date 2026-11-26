"""MFL second brain CLI — resumable, monitored ingestion pipeline.

Usage:
  .venv/bin/python -m mfl_brain init
  .venv/bin/python -m mfl_brain add-path /path/to/folder [--name NAME] [--crawl]
  .venv/bin/python -m mfl_brain remove-path --name NAME [--purge]
  .venv/bin/python -m mfl_brain crawl --config ingest.config.yaml [--source NAME] [--limit N]
  .venv/bin/python -m mfl_brain dedup [--near/--exact-only]
  .venv/bin/python -m mfl_brain anonymise [--limit N]
  .venv/bin/python -m mfl_brain synthesise [--limit N]
  .venv/bin/python -m mfl_brain index [--limit N]
  .venv/bin/python -m mfl_brain atomic-split [--source NAME] [--limit N]
  .venv/bin/python -m mfl_brain run [--limit N] [--source NAME]
  .venv/bin/python -m mfl_brain status
  .venv/bin/python -m mfl_brain ask "question"
  .venv/bin/python -m mfl_brain monitor --state-dir .ingest-state
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

from .anonymise import anonymise_chunk, build_known_pattern
from .atomic_splitter import batch_process_obsidian_folder
from .config import DEFAULT_CONFIG, load_config
from .crawler import crawl
from .dedup import mark_exact_duplicates, mark_near_duplicates, skip_noncanonical
from .indexvec import build_index, query
from .manifest import Manifest
from .monitor import print_summary, write_status
from .monitor_tui import PipelineMonitor
from .synthesise import (build_error_taxonomy_moc, build_mocs, build_voice_profile_moc,
                         summarise_source, write_source_note, write_synthesis_note)


def _cfg_and_manifest(args) -> tuple:
    cfg = load_config(args.config)
    manifest = Manifest(cfg.state_db)
    return cfg, manifest


def _maybe_status(cfg, manifest, force: bool = False) -> None:
    if force:
        write_status(cfg, manifest)


def cmd_init(args) -> int:
    path = Path("ingest.config.yaml")
    if path.exists():
        print("ingest.config.yaml already exists")
        return 0
    path.write_text(yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    print(f"wrote {path.resolve()}")
    return 0


def cmd_add_path(args) -> int:
    """Add a new source path to the config and optionally crawl it immediately."""
    raw = args.path
    # Preserve trailing space if present (macOS folders often have them)
    path = Path(raw)
    if not path.exists():
        # try with trailing space
        if Path(raw + " ").exists():
            path = Path(raw + " ")
        else:
            print(f"error: path does not exist: {raw!r}")
            return 1
    if not path.is_dir():
        print(f"error: path is not a directory: {path}")
        return 1

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"error: config not found: {cfg_path} (run `mfl_brain init` first)")
        return 1

    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    sources = cfg.setdefault("sources", [])
    path_str = str(path)

    # Duplicate check (compare resolved paths to catch trailing-space variants)
    for s in sources:
        if str(Path(s["path"]).resolve()) == str(path.resolve()):
            print(f"already present: source {s['name']!r} -> {s['path']}")
            return 0

    name = args.name or _slugify(path.name)
    # ensure unique name
    existing_names = {s["name"] for s in sources}
    base = name
    i = 2
    while name in existing_names:
        name = f"{base}-{i}"
        i += 1

    new_source = {
        "name": name,
        "path": path_str,
        "enabled": True,
    }
    if args.doc_type_hint:
        new_source["doc_type_hint"] = args.doc_type_hint
    if args.delay_ms is not None:
        new_source["delay_ms"] = args.delay_ms
    if args.include:
        new_source["include_globs"] = args.include
    if args.exclude:
        new_source["exclude_globs"] = args.exclude

    sources.append(new_source)

    with cfg_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True, width=4096)

    print(f"added source {name!r} -> {path_str}")
    if args.delay_ms is not None:
        print(f"  delay_ms: {args.delay_ms}")
    if args.doc_type_hint:
        print(f"  doc_type_hint: {args.doc_type_hint}")

    if args.crawl:
        print(f"\nCrawling {name} (limit={args.limit or 'none'})...")
        cfg_obj = load_config(str(cfg_path))
        manifest = Manifest(cfg_obj.state_db)
        run_id = manifest.start_run("crawl")
        stats = crawl(cfg_obj, manifest, only_source=name,
                      limit=args.limit, verbose=True,
                      walk=True, delay_ms=args.delay_ms or 50)
        manifest.finish_run(run_id, str(stats))
        write_status(cfg_obj, manifest)
        print("crawl:", stats)
    return 0


def cmd_remove_path(args) -> int:
    """Remove a source path from the config (and optionally its manifest rows)."""
    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"error: config not found: {cfg_path}")
        return 1

    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    sources = cfg.get("sources", [])
    target = args.name or args.path
    if target is None:
        print("error: provide --name NAME or a path")
        return 1

    to_remove = []
    for i, src in enumerate(sources):
        if src.get("name") == target:
            to_remove.append(i)
        elif args.path and str(Path(src.get("path", "")).resolve()) == str(Path(args.path).resolve()):
            to_remove.append(i)

    if not to_remove:
        print(f"error: no source matching {target!r}")
        print("available sources:")
        for src in sources:
            print(f"  {src.get('name')!r} -> {src.get('path')}")
        return 1

    removed = []
    for i in sorted(to_remove, reverse=True):
        removed.append(sources.pop(i))

    with cfg_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True, width=4096)

    for r in removed:
        print(f"removed source {r.get('name')!r} -> {r.get('path')}")

    if args.purge:
        cfg_obj = load_config(str(cfg_path))
        manifest = Manifest(cfg_obj.state_db)
        for r in removed:
            pth = r.get("path")
            n = manifest.conn.execute("DELETE FROM chunks WHERE file_path LIKE ?", (pth + "%",)).rowcount
            m = manifest.conn.execute("DELETE FROM files WHERE path LIKE ?", (pth + "%",)).rowcount
            manifest.conn.commit()
            print(f"  purged {m} files and {n} chunks for {r.get('name')!r}")
        manifest.close()

    return 0


def _slugify(s: str) -> str:
    import re
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"[^A-Za-z0-9._-]", "", s)
    return s.lower() or "source"


def cmd_crawl(args) -> int:
    cfg, manifest = _cfg_and_manifest(args)
    run_id = manifest.start_run("crawl")
    stats = crawl(cfg, manifest, only_source=args.source,
                  limit=args.limit, verbose=not args.quiet,
                  walk=not args.no_walk, delay_ms=args.delay,
                  ext=args.ext, order=args.order)
    manifest.finish_run(run_id, str(stats))
    _maybe_status(cfg, manifest, force=True)
    print("crawl:", stats)
    return 0


def cmd_dedup(args) -> int:
    cfg, manifest = _cfg_and_manifest(args)
    run_id = manifest.start_run("dedup")
    exact = mark_exact_duplicates(manifest)
    skipped = skip_noncanonical(manifest)
    near = 0
    if not args.exact_only:
        near = mark_near_duplicates(manifest, cfg.embeddings["ollama_model"])
    manifest.finish_run(run_id, f"exact={exact} near={near}")
    _maybe_status(cfg, manifest, force=True)
    print(f"dedup: exact={exact} near={near} skipped={skipped}")
    return 0


def cmd_anonymise(args) -> int:
    import concurrent.futures
    from types import MappingProxyType

    cfg, manifest = _cfg_and_manifest(args)
    run_id = manifest.start_run("anonymise")
    tiers_seen: dict[str, int] = {}
    errors = 0
    total_done = 0
    batch_size = 50
    limit = args.limit
    workers = 8
    all_pii: list[tuple[str, str, str]] = []

    # ONE read-only snapshot shared by all workers.  Workers never touch the
    # DB (no per-chunk connection / PRAGMA / schema / migration setup); the
    # snapshot is refreshed at batch boundaries once new PII is durable.
    known = MappingProxyType(dict(manifest.known_pii()))
    known_pattern = build_known_pattern(known)

    def _process_chunk(r):
        """Process one chunk using the shared snapshot; returns a result tuple."""
        try:
            text, tier, _replaced, pii_entries = anonymise_chunk(
                r["text"], known=known, known_pattern=known_pattern,
                cfg_anonymiser=cfg.anonymiser
            )
            return (r["chunk_id"], r["file_path"], text, tier, pii_entries, None)
        except RuntimeError as exc:
            return (r["chunk_id"], r["file_path"], None, "quarantined", [], str(exc))

    while True:
        if limit and total_done >= limit:
            break
        remaining = (limit - total_done) if limit else batch_size
        fetch = min(batch_size, remaining) if limit else batch_size
        rows = manifest.conn.execute(
            "SELECT c.* FROM chunks c LEFT JOIN files f ON c.file_path=f.path "
            "WHERE c.status='pending' AND (f.canonical_of IS NULL) ORDER BY c.chunk_id LIMIT ?",
            (fetch,),
        ).fetchall()
        if not rows:
            break

        # Process chunks in parallel — all read the same immutable snapshot.
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_process_chunk, rows))

        # Write results to DB sequentially on the single manifest connection.
        for chunk_id, file_path, text, tier, pii_entries, err in results:
            if err is not None or tier == "quarantined":
                manifest.set_chunk_status(chunk_id, "quarantined", tier="quarantined", commit=False)
                errors += 1
                manifest.set_status(file_path, "quarantined",
                                    error="chunk failed all anonymisation tiers", commit=False)
            else:
                manifest.set_chunk_status(chunk_id, "anonymised", text, tier, commit=False)
                tiers_seen[tier] = tiers_seen.get(tier, 0) + 1
                manifest.set_status(file_path, "anonymised", commit=False)
                all_pii.extend(pii_entries)
            total_done += 1

        # Persist PII and all status changes with ONE commit (no per-entry
        # remember_pii commits, no duplicate end-of-batch commit).
        manifest.remember_pii_batch(all_pii, commit=False)
        all_pii.clear()
        manifest.conn.commit()
        # Refresh the read-only snapshot now that new mappings are durable.
        known = MappingProxyType(dict(manifest.known_pii()))
        known_pattern = build_known_pattern(known)

        if not args.quiet and total_done % 50 == 0:
            # NOTE: do not call write_status() here. render_status() runs
            # SELECT status, COUNT(*) FROM chunks GROUP BY status — a full
            # scan of the multi-million-row chunks table. Doing that every
            # 50 chunks dominated runtime. STATUS.md is written once at
            # stage end via _maybe_status(force=True) below.
            print(f"[anonymise] {total_done} tiers={tiers_seen} quarantined={errors}", flush=True)
        if not args.quiet:
            print(f"[anonymise] batch done: {total_done} total, tiers={tiers_seen}", flush=True)
        if limit and total_done >= limit:
            break

    manifest.finish_run(run_id, f"tiers={tiers_seen} quarantined={errors}")
    _maybe_status(cfg, manifest, force=True)
    print(f"anonymise: {total_done} chunks, tiers={tiers_seen} quarantined={errors}")
    return 0


def _detect_doc_type(cfg, file_path: str) -> str:
    """Detect document type from path using routing rules."""
    p = file_path.lower()
    for dtype, rule in cfg.document_type_routing.items():
        for kw in rule.path_keywords:
            if kw.lower() in p:
                return dtype
    return "resource"


def cmd_synthesise(args) -> int:
    cfg, manifest = _cfg_and_manifest(args)
    run_id = manifest.start_run("synthesise")
    rows = manifest.files_with_status("anonymised", limit=args.limit)
    done = 0
    for r in rows:
        path = r["path"]
        try:
            doc_type = _detect_doc_type(cfg, path)
            rule = cfg.document_type_routing.get(doc_type, cfg.document_type_routing["resource"])
            output_folder = rule.output_folder
            
            note = write_source_note(manifest, path, cfg, doc_type=doc_type)
            result = summarise_source(manifest, path, cfg, doc_type=doc_type)
            if result is None:
                manifest.set_status(path, "synthesised")
                continue
            summary, tags = result
            write_synthesis_note(manifest, path, summary, tags, cfg, output_folder=output_folder)
            manifest.set_status(path, "synthesised")
            done += 1
        except Exception as exc:  # noqa: BLE001
            manifest.set_status(path, "failed", error=f"synth: {exc}"[:400])
        if not args.quiet and done % 25 == 0 and done > 0:
            write_status(cfg, manifest)
    build_mocs(cfg, manifest)
    build_voice_profile_moc(cfg, manifest)
    build_error_taxonomy_moc(cfg, manifest)
    manifest.finish_run(run_id, f"synthesised={done}")
    _maybe_status(cfg, manifest, force=True)
    print(f"synthesise: {done} notes, MOCs rebuilt")
    return 0


def cmd_index(args) -> int:
    cfg, manifest = _cfg_and_manifest(args)
    run_id = manifest.start_run("index")
    n = build_index(manifest, cfg, limit=args.limit)
    manifest.finish_run(run_id, f"indexed={n}")
    _maybe_status(cfg, manifest, force=True)
    print(f"index: {n} chunks embedded")
    return 0


def cmd_atomic_split(args) -> int:
    """Split Obsidian knowledge base files into atomic notes."""
    cfg, manifest = _cfg_and_manifest(args)
    run_id = manifest.start_run("atomic_split")
    
    # Only process sources with doc_type_hint == "knowledge"
    knowledge_sources = [s for s in cfg.sources if s.enabled and s.doc_type_hint == "knowledge"]
    
    total_files = 0
    total_notes = 0
    for src in knowledge_sources:
        if args.source and src.name != args.source:
            continue
        print(f"[atomic_split] Processing {src.name}...")
        stats = batch_process_obsidian_folder(
            source_dir=Path(src.path),
            vault_root=cfg.vault_path,
            output_base=cfg.vault_path / "10-Resources/by-theme",
        )
        total_files += stats["files"]
        total_notes += stats["atomic_notes"]
        print(f"  {stats['files']} files → {stats['atomic_notes']} atomic notes ({stats['errors']} errors)")
    
    manifest.finish_run(run_id, f"files={total_files} notes={total_notes}")
    print(f"atomic_split: {total_files} files → {total_notes} atomic notes")
    return 0


def cmd_run(args) -> int:
    cfg, manifest = _cfg_and_manifest(args)
    while True:
        crawl(cfg, manifest, only_source=args.source, limit=args.limit, verbose=False)
        cmd_dedup(argparse.Namespace(config=args.config, exact_only=True))
        cmd_anonymise(argparse.Namespace(config=args.config, limit=args.limit, quiet=False))
        cmd_synthesise(argparse.Namespace(config=args.config, limit=args.limit, quiet=False))
        cmd_index(argparse.Namespace(config=args.config, limit=args.limit))
        more = manifest.pending_chunks()
        if not more or args.limit:
            break
    print_summary(cfg, manifest)
    return 0


def cmd_run_full(args) -> int:
    """Run full pipeline including atomic splitting for knowledge sources."""
    cfg, manifest = _cfg_and_manifest(args)
    
    # First run atomic split for knowledge sources
    atomic_args = argparse.Namespace(config=args.config, source=args.source, limit=args.limit, quiet=args.quiet)
    cmd_atomic_split(atomic_args)
    
    # Then run standard pipeline
    return cmd_run(args)


def cmd_status(args) -> int:
    cfg, manifest = _cfg_and_manifest(args)
    print_summary(cfg, manifest)
    path = write_status(cfg, manifest)
    print(f"status written: {path}")
    return 0


def cmd_ask(args) -> int:
    cfg, manifest = _cfg_and_manifest(args)
    hits = query(cfg, manifest, args.question, k=args.k)
    if not hits:
        print("index empty — run: python -m mfl_brain index")
        return 1
    for h in hits:
        print(f"\n[{h['distance']:.3f}] {h['file']} (chunk {h['seq']})")
        print(h["text"][:300].replace("\n", " "))
    return 0


def cmd_monitor(args) -> int:
    monitor = PipelineMonitor(args.config, Path(args.state_dir))
    try:
        monitor.run_monitor()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_incremental(args) -> int:
    """Run incremental update (called from monitor 'u' key)."""
    cfg, manifest = _cfg_and_manifest(args)
    from .crawler import crawl
    from .dedup import mark_exact_duplicates, mark_near_duplicates, skip_noncanonical
    from .anonymise import anonymise_chunk, build_known_pattern
    from .indexvec import build_index
    from .monitor import write_status
    
    print("[incremental] Starting incremental update...")
    
    # Crawl new/changed files
    crawl(cfg, manifest, verbose=True, walk=True, delay_ms=200, limit=None)
    
    # Run rest of pipeline
    mark_exact_duplicates(manifest)
    skip_noncanonical(manifest)
    mark_near_duplicates(manifest, cfg.embeddings["ollama_model"])
    
    rows = manifest.pending_chunks()
    for r in rows:
        try:
            text, tier, _, _ = anonymise_chunk(r["text"], manifest, cfg.anonymiser)
            manifest.set_chunk_status(r["chunk_id"], "anonymised", text)
        except RuntimeError:
            manifest.set_chunk_status(r["chunk_id"], "quarantined")
    
    files = manifest.files_with_status("anonymised")
    for f in files:
        doc_type = _detect_doc_type(cfg, f["path"])
        rule = cfg.document_type_routing.get(doc_type, cfg.document_type_routing["resource"])
        output_folder = rule.output_folder
        
        write_source_note(manifest, f["path"], cfg, doc_type=doc_type)
        res = summarise_source(manifest, f["path"], cfg, doc_type=doc_type)
        if res:
            summary, tags = res
            write_synthesis_note(manifest, f["path"], summary, tags, cfg, output_folder=output_folder)
            manifest.set_status(f["path"], "synthesised")
    
    build_mocs(cfg, manifest)
    build_voice_profile_moc(cfg, manifest)
    build_error_taxonomy_moc(cfg, manifest)
    build_index(manifest, cfg)
    write_status(cfg, manifest)
    print("[incremental] Complete")
    return 0


def _detect_doc_type(cfg, file_path: str) -> str:
    p = file_path.lower()
    for dtype, rule in cfg.document_type_routing.items():
        for kw in rule.path_keywords:
            if kw.lower() in p:
                return dtype
    return "resource"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mfl_brain")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--config", default="ingest.config.yaml")
        sp.add_argument("--quiet", action="store_true")

    sp = sub.add_parser("init"); common(sp); sp.set_defaults(fn=cmd_init)
    sp = sub.add_parser("add-path"); common(sp)
    sp.add_argument("path", help="Directory to add as a source (trailing space preserved)")
    sp.add_argument("--name", help="Source name (default: slugified folder name)")
    sp.add_argument("--doc-type-hint", choices=["resource", "knowledge", "report", "student_work", "scheme", "assessment", "cpd", "admin"])
    sp.add_argument("--delay-ms", type=int, help="Per-source pacing (default: global crawl delay)")
    sp.add_argument("--include", nargs="+", help="Include globs (e.g. '**/*.docx' '**/*.pdf')")
    sp.add_argument("--exclude", nargs="+", help="Exclude globs")
    sp.add_argument("--crawl", action="store_true", help="Crawl this source immediately after adding")
    sp.add_argument("--limit", type=int, help="Limit files when crawling (with --crawl)")
    sp.set_defaults(fn=cmd_add_path)
    sp = sub.add_parser("remove-path"); common(sp)
    sp.add_argument("path", nargs="?", help="Directory path to remove")
    sp.add_argument("--name", help="Source name to remove")
    sp.add_argument("--purge", action="store_true",
                    help="Also delete manifest rows (files + chunks) for this source")
    sp.set_defaults(fn=cmd_remove_path)
    sp = sub.add_parser("crawl"); common(sp)
    sp.add_argument("--source"); sp.add_argument("--limit", type=int)
    sp.add_argument("--no-walk", action="store_true",
                    help="retry known failed/pending files without re-walking sources")
    sp.add_argument("--ext", help="only retry this extension in no-walk mode (e.g. pdf)")
    sp.add_argument("--order", default="updated_at", choices=["updated_at", "size", "path"],
                    help="retry order in no-walk mode (size = smallest first)")
    sp.add_argument("--delay", type=int, default=100, help="ms pause between files (OneDrive pacing)")
    sp.set_defaults(fn=cmd_crawl)
    sp = sub.add_parser("dedup"); common(sp)
    sp.add_argument("--exact-only", action="store_true")
    sp.set_defaults(fn=cmd_dedup)
    sp = sub.add_parser("anonymise"); common(sp)
    sp.add_argument("--limit", type=int)
    sp.set_defaults(fn=cmd_anonymise)
    sp = sub.add_parser("synthesise"); common(sp)
    sp.add_argument("--limit", type=int)
    sp.set_defaults(fn=cmd_synthesise)
    sp = sub.add_parser("index"); common(sp)
    sp.add_argument("--limit", type=int)
    sp.set_defaults(fn=cmd_index)
    sp = sub.add_parser("atomic-split"); common(sp)
    sp.add_argument("--source"); sp.add_argument("--limit", type=int)
    sp.set_defaults(fn=cmd_atomic_split)
    sp = sub.add_parser("run"); common(sp)
    sp.add_argument("--source"); sp.add_argument("--limit", type=int)
    sp.add_argument("--delay", type=int, default=100)
    sp.set_defaults(fn=cmd_run)
    sp = sub.add_parser("run-full"); common(sp)
    sp.add_argument("--source"); sp.add_argument("--limit", type=int)
    sp.set_defaults(fn=cmd_run_full)
    sp = sub.add_parser("status"); common(sp); sp.set_defaults(fn=cmd_status)
    sp = sub.add_parser("ask"); common(sp)
    sp.add_argument("question"); sp.add_argument("--k", type=int, default=5)
    sp.set_defaults(fn=cmd_ask)
    sp = sub.add_parser("monitor"); common(sp)
    sp.add_argument("--state-dir", default=".ingest-state")
    sp.set_defaults(fn=cmd_monitor)
    sp = sub.add_parser("incremental"); common(sp)
    sp.set_defaults(fn=cmd_incremental)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())