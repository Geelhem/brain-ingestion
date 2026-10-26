"""Anonymiser with tiered fallback: Apple PCC → Apple on-device → Ollama.

Apple's Foundation Models framework may route to Private Cloud Compute when the
session requires it; the SDK tiers are implemented as availability-ordered
backends so a failure at one tier falls cleanly to the next.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

REDACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "value": {"type": "string", "description": "exact PII text found", "x-order": 0},
                    "kind": {
                        "type": "string",
                        "enum": ["person", "email", "phone", "date_of_birth",
                                 "address", "assessment_record", "other"],
                        "x-order": 1,
                    },
                },
                "required": ["value", "kind"],
            },
        }
    },
    "required": ["items"],
}

SYSTEM_PROMPT = (
    "You find personally identifying information in text from a school teacher's "
    "documents. Return every PII occurrence exactly as it appears: student names, "
    "staff names, email addresses, phone numbers, dates of birth, home addresses, "
    "and individual student assessment/grade records. Do NOT include the teacher's "
    "own name. Do not invent entries. Return only the structured result."
)

KIND_PREFIX = {
    "person": "PER", "email": "EMA", "phone": "TEL", "date_of_birth": "DOB",
    "address": "ADR", "assessment_record": "ASR", "other": "MISC",
}

import dataclasses  # noqa: E402

import apple_fm_sdk as _fm  # noqa: E402


@_fm.generable()
@dataclasses.dataclass
class RedactionItem:
    value: str
    kind: str


@_fm.generable()
@dataclasses.dataclass
class RedactionList:
    items: list[RedactionItem]

fm_available_cache: bool | None = None


def _apple_fm_ok() -> bool:
    global fm_available_cache
    if fm_available_cache is None:
        try:
            import apple_fm_sdk as fm
            m = fm.SystemLanguageModel()
            avail = m.is_available()
            fm_available_cache = bool(avail[0] if isinstance(avail, tuple) else avail)
        except Exception:
            fm_available_cache = False
    return fm_available_cache


def _apple_fm_find_pii(text: str, use_pcc: bool) -> list[dict[str, str]]:
    import asyncio

    model = _fm.SystemLanguageModel()
    session = _fm.LanguageModelSession(model=model, instructions=SYSTEM_PROMPT)

    async def _run() -> RedactionList:
        result = await session.respond(text[:8000], generating=RedactionList)
        if isinstance(result, RedactionList):
            return result
        return RedactionList._from_generated_content(result)

    parsed = asyncio.run(_run())
    return [{"value": i.value, "kind": i.kind} for i in parsed.items]


def _ollama_find_pii(text: str, model: str) -> list[dict[str, str]]:
    import ollama

    prompt = (
        f"{SYSTEM_PROMPT}\n\nText:\n\"\"\"{text[:6000]}\"\"\"\n\n"
        'Reply with ONLY JSON: {"items":[{"value":"...","kind":"..."}], "items":[]}'
    )
    resp = ollama.chat(model=model, messages=[{"role": "user", "content": prompt}],
                       format="json")
    data = json.loads(resp["message"]["content"])
    return data.get("items", [])


def _placeholder(kind: str, counter: dict[str, int]) -> str:
    prefix = KIND_PREFIX.get(kind, "MISC")
    counter[prefix] = counter.get(prefix, 0) + 1
    return f"[[{prefix}-{counter[prefix]:04d}]]"


def _apply(text: str, items: list[dict[str, str]], manifest,
           counter: dict[str, int]) -> tuple[str, int]:
    replaced = 0
    for item in items:
        value = (item.get("value") or "").strip()
        kind = item.get("kind", "other")
        if not value or len(value) < 2:
            continue
        ph, _ = manifest.known_pii().get(value, (None, None))
        if ph is None:
            ph = _placeholder(kind, counter)
            manifest.remember_pii(value, ph, kind)
        if value in text:
            text = text.replace(value, ph)
            replaced += 1
    return text, replaced


def anonymise_chunk(chunk_text: str, manifest, cfg_anonymiser: dict) -> tuple[str, str, int]:
    """Returns (anonymised_text, tier_used, replacements). Tier: pcc/device/ollama/quarantine."""
    known = manifest.known_pii()
    text = chunk_text
    for real, (ph, _k) in known.items():
        if real in text:
            text = text.replace(real, ph)

    name_re = (r"\b(?:Mr|Mrs|Ms|Miss|Mme|Mlle|Monsieur|Madame|Dr)\.?\s+[A-ZÂÛÎ][\wÂÛÎôûî'’-]+"
               r"|\b[A-ZÂÛÎ][\wÂÛÎôûî'’-]+\s+[A-ZÂÛÎ][\wÂÛÎôûî'’-]+\s+[A-ZÂÛÎ][\wÂÛÎôûî'’-]+\b"
               r"|[\w.+-]+@[\w-]+\.[\w.]+"
               r"|(?<![\w])\+?\d[\d ]{7,}\d(?![\w])"
               r"|\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b")
    if re.search(name_re, text) is None:
        return text, "prefilter-clean", 0

    tiers = cfg_anonymiser.get("preference", ["apple_pcc", "apple_device", "ollama"])
    ollama_model = cfg_anonymiser.get("ollama_model", "qwen2.5:14b")
    counter: dict[str, int] = {}
    last_error: Exception | None = None
    for tier in tiers:
        try:
            if tier == "apple_pcc":
                if not _apple_fm_ok():
                    continue
                items = _apple_fm_find_pii(text, use_pcc=True)
            elif tier == "apple_device":
                if not _apple_fm_ok():
                    continue
                items = _apple_fm_find_pii(text, use_pcc=False)
            elif tier == "ollama":
                items = _ollama_find_pii(text, ollama_model)
            else:
                continue
            new_text, replaced = _apply(text, items, manifest, counter)
            return new_text, tier, replaced
        except Exception as exc:  # noqa: BLE001 — tier fallback is the point
            last_error = exc
            continue
    raise RuntimeError(f"all anonymisation tiers failed; last error: {last_error}")
