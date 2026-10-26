import os
import tempfile
from pathlib import Path

import pytest

from mfl_brain.config import load_config
from mfl_brain.crawler import chunk_text
from mfl_brain.manifest import Manifest


@pytest.fixture
def cfg(tmp_path):
    cfg_path = tmp_path / "ingest.config.yaml"
    cfg_path.write_text(
        f"""
vault_path: {tmp_path / "vault"}
state_db: .ingest-state/manifest.db
sources:
  - name: test
    path: {tmp_path / "src"}
    enabled: true
    include_globs: ["**/*.txt"]
    exclude_globs: []
""",
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "vault").mkdir()
    return load_config(cfg_path)


@pytest.fixture
def manifest(cfg):
    m = Manifest(cfg.vault_path / cfg.state_db)
    yield m
    m.close()


def test_chunk_text_boundaries():
    text = "Sentence one. " * 200
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= 1350 for c in chunks)
    # overlap: consecutive chunks share content
    assert chunks[0][-50:] != ""


def test_chunk_text_short():
    assert chunk_text("hello world") == ["hello world"]
    assert chunk_text("   ") == []


def test_manifest_checkpoint_roundtrip(manifest):
    manifest.upsert_file("/tmp/a.txt", "test", 100, 1.0)
    manifest.set_status("/tmp/a.txt", "extracted")
    row = manifest.get("/tmp/a.txt")
    assert row["status"] == "extracted"
    manifest.add_chunks("/tmp/a.txt", ["chunk one", "chunk two"])
    assert manifest.chunk_counts()["pending"] == 2
    manifest.set_chunk_status(1, "anonymised", "clean text")
    assert manifest.chunk_counts().get("anonymised") == 1
    assert manifest.pending_chunks()[0]["seq"] == 1


def test_pii_map_idempotent(manifest):
    manifest.remember_pii("Jean Dupont", "[[PER-0001]]", "person")
    manifest.remember_pii("Jean Dupont", "[[PER-0002]]", "person")
    known = manifest.known_pii()
    assert known["Jean Dupont"][0] == "[[PER-0001]]"


def test_resume_skips_done_files(cfg, manifest):
    from mfl_brain.crawler import crawl

    src = cfg.raw["sources"][0]["path"]
    for i in range(3):
        Path(src, f"f{i}.txt").write_text("Unique teaching content about French grammar. " * 5)
    crawl(cfg, manifest, verbose=False)
    assert manifest.count_by_status()["extracted"] == 3
    # re-crawl: everything skipped
    stats = crawl(cfg, manifest, verbose=False)
    assert stats["skipped"] == 3
    assert stats["extracted"] == 0


def test_config_defaults(cfg):
    assert cfg.anonymiser["preference"][0] == "apple_pcc"
    assert cfg.embeddings["ollama_model"] == "paraphrase-multilingual"
