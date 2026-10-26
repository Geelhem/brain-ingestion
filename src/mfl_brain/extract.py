"""Text extraction from common document formats with conditional OCR fallback."""

from __future__ import annotations

import email
import email.policy
from pathlib import Path

from .ocr import extract_with_ocr_fallback, ocr_pdf, needs_ocr

MAX_BYTES = 200 * 1024 * 1024


class ExtractionError(Exception):
    pass


def extract_text(path: Path, ocr_config: dict | None = None) -> str:
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise ExtractionError(f"file too large: {size} bytes")
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return _txt(path)
    if suffix == ".md":
        return _txt(path)
    if suffix == ".docx":
        return _docx(path)
    if suffix == ".pptx":
        return _pptx(path)
    if suffix == ".pdf":
        return _pdf_with_ocr(path, ocr_config)
    if suffix == ".xlsx":
        return _xlsx(path)
    if suffix == ".msg":
        return _msg(path)
    if suffix == ".doc":
        return _legacy_binary(path)
    if suffix == ".ppt":
        return _legacy_binary(path)
    if suffix == ".onepkg":
        return _onepkg(path)
    raise ExtractionError(f"unsupported type: {suffix}")


def _pdf_with_ocr(path: Path, ocr_config: dict | None) -> str:
    """Extract PDF text with conditional OCR fallback."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)

    if ocr_config and ocr_config.get("enabled", False):
        min_len = ocr_config.get("min_text_length", 100)
        if needs_ocr(text, min_len):
            from .ocr import ocr_pdf
            ocr_text = ocr_pdf(
                path,
                languages=ocr_config.get("languages", ["eng"]),
                min_text_length=ocr_config.get("min_text_length", 100)
            )
            if ocr_text and len(ocr_text.strip()) > len(text.strip()):
                return ocr_text
    return text


def _txt(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _pptx(path: Path) -> str:
    from pptx import Presentation

    prs = Presentation(str(path))
    parts: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        texts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    texts.append(t)
        if texts:
            parts.append(f"## Slide {i}\n" + "\n".join(texts))
    return "\n\n".join(parts)


def _xlsx(path: Path) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    parts: list[str] = []
    for sheet in wb.worksheets:
        parts.append(f"## Sheet: {sheet.title}")
        for row in sheet.iter_rows(values_only=True):
            vals = [str(v) for v in row if v is not None]
            if vals:
                parts.append(" | ".join(vals))
    wb.close()
    return "\n".join(parts)


def _msg(path: Path) -> str:
    msg = email.message_from_binary_file(
        open(path, "rb"), policy=email.policy.default  # noqa: SIM115
    )
    headers = [f"From: {msg.get('From')}", f"Subject: {msg.get('Subject')}"]
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                body = part.get_content()
                break
    else:
        body = msg.get_content()
    return "\n".join(headers) + "\n\n" + body


def _legacy_binary(path: Path) -> str:
    """Best-effort text scrape of legacy .doc/.ppt (OLE) files."""
    data = path.read_bytes()
    chunks: list[str] = []
    for encoding in ("utf-16-le", "cp1252"):
        try:
            text = data.decode(encoding, errors="ignore")
        except Exception:
            continue
        runs = "".join(c if c.isprintable() or c in "\n\t" else " " for c in text)
        words = [w for w in runs.split() if len(w) > 2 and w.isascii() or len(w) > 2]
        if len(words) > 40:
            chunks.append(" ".join(words[:5000]))
            break
    return "\n".join(chunks)


def _onepkg(path: Path) -> str:
    """Extract text from OneNote Package (.onepkg) - ZIP-based format."""
    import zipfile
    import xml.etree.ElementTree as ET
    from io import BytesIO

    text_parts = []
    try:
        with zipfile.ZipFile(path, 'r') as z:
            for name in z.namelist():
                if name.endswith('.one') or name.endswith('.xml'):
                    try:
                        data = z.read(name)
                        # Try to parse as XML for text content
                        if name.endswith('.xml'):
                            root = ET.fromstring(data)
                            for elem in root.iter():
                                if elem.text and elem.text.strip():
                                    text_parts.append(elem.text.strip())
                    except Exception:
                        continue
    except Exception as e:
        return f"[OneNote package extraction failed: {e}]"
    return "\n\n".join(text_parts) if text_parts else f"[OneNote package: {path.name} - no extractable text]"