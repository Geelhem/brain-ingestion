"""Tests for bounded extraction retry and German OCR support (GS-564)."""

from pathlib import Path

import pytest

from mfl_brain.config import DEFAULT_CONFIG, load_config
from mfl_brain.crawler import retry_candidates
from mfl_brain.manifest import MAX_EXTRACTION_ATTEMPTS, Manifest
from mfl_brain.ocr import DEFAULT_OCR_LANGUAGES


@pytest.fixture
def manifest(tmp_path):
    m = Manifest(tmp_path / ".ingest-state" / "manifest.db")
    yield m
    m.close()


def test_ocr_defaults_include_german():
    assert "deu" in DEFAULT_OCR_LANGUAGES
    assert "deu" in DEFAULT_CONFIG["ocr"]["languages"]


def test_config_yaml_languages_include_german(tmp_path):
    cfg_path = tmp_path / "ingest.config.yaml"
    cfg_path.write_text(
        f"""
vault_path: {tmp_path / "vault"}
state_db: {tmp_path / "state.db"}
ocr:
  enabled: true
  languages: [eng, fra, spa, deu]
sources: []
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert "deu" in cfg.ocr["languages"]


def test_extraction_failure_increments_attempts(manifest):
    manifest.upsert_file("/tmp/a.pdf", "test", 10, 1.0)
    for expected in range(1, MAX_EXTRACTION_ATTEMPTS + 1):
        assert manifest.record_extraction_failure("/tmp/a.pdf", "too little text") == expected
    row = manifest.get("/tmp/a.pdf")
    assert row["status"] == "extraction_failed"
    assert row["attempts"] == MAX_EXTRACTION_ATTEMPTS


def test_terminal_extraction_failed_is_not_retried(manifest):
    manifest.upsert_file("/tmp/terminal.pdf", "test", 10, 1.0)
    for _ in range(MAX_EXTRACTION_ATTEMPTS):
        manifest.record_extraction_failure("/tmp/terminal.pdf", "too little text")

    manifest.upsert_file("/tmp/retryable.pdf", "test", 10, 1.0)
    manifest.record_extraction_failure("/tmp/retryable.pdf", "too little text")

    paths = {r["path"] for r in retry_candidates(manifest, "test")}
    assert "/tmp/retryable.pdf" in paths
    assert "/tmp/terminal.pdf" not in paths


def test_ext_filter_narrows_retry_batch(manifest):
    manifest.upsert_file("/tmp/a.pdf", "test", 10, 1.0)
    manifest.upsert_file("/tmp/b.pptx", "test", 10, 1.0)
    manifest.record_extraction_failure("/tmp/a.pdf", "too little text")
    manifest.record_extraction_failure("/tmp/b.pptx", "too little text")

    pdf_paths = {r["path"] for r in retry_candidates(manifest, "test", ext="pdf")}
    assert pdf_paths == {"/tmp/a.pdf"}


def test_requeued_extraction_failed_is_retried(manifest):
    manifest.upsert_file("/tmp/backlog.pdf", "test", 10, 1.0)
    for _ in range(MAX_EXTRACTION_ATTEMPTS):
        manifest.record_extraction_failure("/tmp/backlog.pdf", "too little text")
    assert "/tmp/backlog.pdf" not in {r["path"] for r in retry_candidates(manifest, "test")}

    manifest.conn.execute(
        "UPDATE files SET status='discovered', attempts=0 "
        "WHERE status='extraction_failed' AND path LIKE '%.pdf'"
    )
    manifest.conn.commit()
    assert "/tmp/backlog.pdf" in {r["path"] for r in retry_candidates(manifest, "test")}


def _tesseract_langs() -> set[str]:
    pytesseract = pytest.importorskip("pytesseract")
    try:
        return set(pytesseract.get_languages(config=""))
    except Exception:  # pragma: no cover - tesseract missing
        pytest.skip("tesseract not available")


def test_german_display_image_extracts_text(tmp_path):
    pytest.importorskip("PIL")
    if "deu" not in _tesseract_langs():
        pytest.skip("German tessdata (deu) not installed")

    from PIL import Image, ImageDraw, ImageFont

    font_path = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
    if not font_path.exists():
        pytest.skip("Arial font not available for test image")

    img = Image.new("RGB", (1500, 520), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(font_path), 32)
    lines = [
        "Deutschunterricht für die Oberstufe.",
        "Die Schülerinnen und Schüler lernen Grammatik,",
        "Literatur und Geschichte.",
        "Größe, Prüfung und Übung.",
    ]
    y = 30
    for line in lines:
        draw.text((20, y), line, fill="black", font=font)
        y += 70
    image_path = tmp_path / "german_display.png"
    img.save(image_path)

    from mfl_brain.ocr import ocr_image

    text = ocr_image(image_path, DEFAULT_OCR_LANGUAGES)
    assert "Schülerinnen" in text
    assert "Größe" in text
    assert "Prüfung" in text
