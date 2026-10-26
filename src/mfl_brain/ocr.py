"""Conditional OCR for scanned/image-based PDFs."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import pytesseract
    from pdf2image import convert_from_path
    from PIL import Image
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False
    pytesseract = None
    convert_from_path = None
    Image = None


def ocr_pdf(path: Path, languages: list[str], min_text_length: int = 100) -> str:
    """Run OCR on a PDF file. Returns extracted text."""
    if not OCR_AVAILABLE:
        logger.warning("OCR dependencies not available (pytesseract, pdf2image)")
        return ""

    lang_str = "+".join(languages)
    try:
        images = convert_from_path(str(path), dpi=300)
        texts = []
        for i, img in enumerate(images):
            text = pytesseract.image_to_string(img, lang=lang_str)
            if text.strip():
                texts.append(f"--- Page {i+1} ---\n{text}")
        return "\n\n".join(texts)
    except Exception as e:
        logger.error(f"OCR failed for {path}: {e}")
        return ""


def ocr_image(path: Path, languages: list[str]) -> str:
    """Run OCR on an image file."""
    if not OCR_AVAILABLE:
        return ""
    if Image is None:
        return ""
    try:
        img = Image.open(path)
        lang_str = "+".join(languages)
        return pytesseract.image_to_string(img, lang=lang_str)
    except Exception as e:
        logger.error(f"OCR failed for {path}: {e}")
        return ""


def needs_ocr(text: str, min_length: int = 100) -> bool:
    """Check if extracted text is too short/sparse, suggesting OCR needed."""
    if not text:
        return True
    # Count alphanumeric characters
    alnum = sum(1 for c in text if c.isalnum())
    return alnum < min_length


def extract_with_ocr_fallback(
    path: Path,
    primary_extractor,
    ocr_config: dict
) -> str:
    """Try primary extractor first; if result is too short, run OCR."""
    text = primary_extractor(path)
    if ocr_config.get("enabled", False) and needs_ocr(text, ocr_config.get("min_text_length", 100)):
        logger.info(f"Primary extraction yielded little text for {path.name}, running OCR...")
        ocr_text = ocr_pdf(
            path,
            languages=ocr_config.get("languages", ["eng"]),
            min_text_length=ocr_config.get("min_text_length", 100)
        )
        if ocr_text and len(ocr_text) > len(text):
            return ocr_text
    return text