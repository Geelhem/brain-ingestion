"""Vault writing: source notes, synthesis notes, MOCs, tags, wikilinks."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .manifest import Manifest


def safe_note_name(path: str, vault: Path, subfolder: str = "10-Resources") -> tuple[str, Path]:
    src = Path(path)
    stem = src.stem.replace(" ", "_")
    folder = src.parent.name.replace(" ", "_") or "root"
    name = f"{folder}--{stem}"
    if len(name) > 110:
        name = name[-110:]
    import hashlib
    h = hashlib.sha256(path.encode()).hexdigest()[:6]
    name = f"{name}-{h}"
    target = vault / subfolder / f"{name}.md"
    i = 2
    while target.exists() and not target.name.startswith(name):
        target = vault / subfolder / f"{name}-{i}.md"
        i += 1
    return name, target


def frontmatter(kv: dict[str, str | int | list[str]]) -> str:
    lines = ["---"]
    for k, v in kv.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            lines.extend(f"  - {item}" for item in v)
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def write_source_note(manifest: Manifest, file_path: str, cfg, doc_type: str = "resource") -> Path | None:
    vault = cfg.vault_path
    row = manifest.get(file_path)
    if row is None:
        return None
    canonical = row["canonical_of"]
    if canonical:
        c_row = manifest.get(canonical)
        name, _ = safe_note_name(canonical or file_path, vault)
        note = vault / "10-Resources" / f"{name}.md"
        if note.exists():
            return note
        return None
    chunks = manifest.conn.execute(
        "SELECT text FROM chunks WHERE file_path=? ORDER BY seq", (file_path,)
    ).fetchall()
    if not chunks:
        return None
    name, target = safe_note_name(file_path, vault)
    body = "\n\n".join(c["text"] for c in chunks[:40])
    fm = frontmatter({
        "source_path": file_path,
        "origin": row["source"],
        "type": doc_type,
        "tags": [f"#ingest/{doc_type}"],
        "ingested": True,
    })
    target.write_text(
        f"{fm}\n\n# {name}\n\n{body}\n\n> [!info] Original: `{file_path}`\n",
        encoding="utf-8",
    )
    manifest.set_field(file_path, "note_path", str(target))
    return target


def get_synthesis_prompt(doc_type: str, sample: str, cfg) -> str:
    """Generate synthesis prompt based on document type."""
    model = cfg.synthesis.get("ollama_model", "qwen2.5:14b")
    base = f"This is from a Modern Foreign Languages teacher's documents (French/Spanish teaching, IB/IGCSE/GCSE). Document type: {doc_type}.\n"

    prompts = {
        "student_work": (
            base + "This is student work (French/Spanish). Analyze for language errors.\n"
            "Extract every language error and categorize. Output JSON:\n"
            '{"errors": [{"text": "error text", "category": "grammar|vocab|syntax|register|cohesion|pronunciation|spelling", '
            '"subcategory": "specific type (e.g., subjunctive-formation, false-friends, tense-sequencing)", '
            '"target_structure": "grammar point", "severity": "minor|major", "correction": "corrected version"}]}'
        ),
        "report": (
            base + "This is a school report comment. Extract reusable phrases and patterns.\n"
            "Output JSON:\n"
            '{"voice_patterns": [{"phrase": "exact phrase", "context": "praise|correction|neutral", '
            '"proficiency": "HL|SL|AbInitio|Foundation|Higher", "language": "French|Spanish", '
            '"category": "achievement|effort|behavior|oral|writing|reading|listening"}]}'
        ),
        "scheme": (
            base + "This is a scheme of work / unit plan. Map to standards.\n"
            "Output JSON:\n"
            '{"programme": "IB-DP|IGCSE|GCSE-2026|KS3|MYP", "language": "French|Spanish", '
            '"year": "7-13|KS3|KS4|KS5|IB1|IB2", "theme": "topic", "skills": ["Listening","Reading","Writing","Speaking"], '
            '"assessment_objectives": ["AO1","AO2","AO3","AO4"], "key_grammar": ["point1"], '
            '"key_vocab_topics": ["topic1"], "pedagogy": "EPI|Conti|TBLT|CLIL|Traditional"}'
        ),
        "assessment": (
            base + "This is an assessment / markscheme / rubric / past paper. Extract criteria.\n"
            "Output JSON:\n"
            '{"programme": "IB-DP|IGCSE|GCSE-2026|KS3", "language": "French|Spanish", '
            '"paper": "Paper 1|Paper 2|Paper 3|IA|Oral|Coursework", "criteria": [{"code": "A", "descriptor": "text", "marks": 10}], '
            '"grade_boundaries": {"7": 85, "6": 70}, "command_terms": ["analyse", "evaluate"]}'
        ),
        "cpd": (
            base + "This is CPD / training material. Extract actionable takeaways.\n"
            "Output JSON:\n"
            '{"takeaways": [{"concept": "key concept", "application": "how to apply in classroom", '
            '"relevant_programmes": ["IB-DP", "IGCSE"], "relevant_languages": ["French", "Spanish"], '
            '"pedagogy_tags": ["EPI", "Conti", "TBLT"]}]}'
        ),
        "knowledge": (
            base + "This is a knowledge note from a personal knowledge base. Create atomic summary.\n"
            "Output JSON:\n"
            '{"key_ideas": ["idea1", "idea2"], "concepts": ["concept1"], '
            '"relevant_to": ["IB-DP", "IGCSE"], "languages": ["French", "Spanish"], "tags": ["tag1"]}'
        ),
        "admin": (
            base + "This is an administrative document. Summarize key points.\n"
            "Output JSON:\n"
            '{"summary": "text", "key_dates": [], "action_items": [], "people": [], "tags": []}'
        ),
        "resource": (
            base + "This is a teaching resource. Write a 3-sentence summary and topic tags.\n"
            "Respond in exactly this format:\nSUMMARY: <text>\nTAGS:\nTAG: <topic>\nTAG: <topic>"
        ),
    }

    return prompts.get(doc_type, prompts["resource"])


def summarise_source(manifest: Manifest, file_path: str, cfg, doc_type: str = "resource") -> tuple[str, list[str]] | None:
    import ollama

    model = cfg.synthesis.get("ollama_model", "qwen2.5:14b")
    chunks = manifest.conn.execute(
        "SELECT text FROM chunks WHERE file_path=? AND seq<=4 ORDER BY seq", (file_path,)
    ).fetchall()
    if not chunks:
        return None
    sample = "\n---\n".join(c["text"] for c in chunks)[:6000]

    if doc_type in ("student_work", "report", "scheme", "assessment", "cpd", "knowledge", "admin"):
        prompt = get_synthesis_prompt(doc_type, sample, cfg)
        resp = ollama.chat(model=model, messages=[{"role": "user", "content": prompt}], format="json")
        out = resp["message"]["content"]
        try:
            data = json.loads(out)
            if doc_type == "student_work":
                errors = data.get("errors", [])
                if not errors:
                    return None  # no real errors — write nothing, not a stub
                summary = f"Error taxonomy: {len(errors)} errors categorized."
                tags = ["#error-taxonomy", "#student-work"]
            elif doc_type == "report":
                patterns = data.get("voice_patterns", [])
                if not patterns:
                    return None  # no real voice patterns — write nothing, not a stub
                summary = f"Voice profile: {len(patterns)} patterns extracted."
                tags = ["#voice-profile", "#reports"]
            elif doc_type == "scheme":
                if not data.get("programme") and not data.get("theme"):
                    return None
                summary = f"Scheme mapped to standards: {data.get('programme', '')} {data.get('language', '')} {data.get('year', '')}."
                tags = [f"#programme/{data.get('programme','').lower()}", f"#language/{data.get('language','').lower()}"]
            elif doc_type == "assessment":
                if not data.get("criteria"):
                    return None
                summary = f"Assessment criteria mapped: {data.get('programme','')} {data.get('paper','')}."
                tags = [f"#assessment/{data.get('paper','').lower().replace(' ', '-')}"]
            elif doc_type == "cpd":
                takeaways = data.get("takeaways", [])
                if not takeaways:
                    return None
                summary = f"CPD takeaways: {len(takeaways)} concepts."
                tags = ["#cpd"]
            elif doc_type == "knowledge":
                ideas = data.get("key_ideas", [])
                if not ideas:
                    return None
                summary = f"Knowledge note: {', '.join(ideas[:3])}."
                tags = ["#knowledge"]
            else:
                summary = data.get("summary", "Admin document processed.")
                if not summary or summary == "Admin document processed.":
                    return None
                tags = ["#admin"]
            return summary, ["#" + t for t in tags]
        except Exception:
            pass

    # Fallback to standard prompt
    prompt = (
        "This is from a Modern Foreign Languages teacher's documents (French/Spanish teaching). "
        "Write a 3-sentence summary of what this document is about and its usefulness.\n"
        "Then list up to 5 topic tags in this exact format, one per line: TAG: <kebab-case-topic>\n"
        f"Allowed topics include: pedagogy, grammar, vocabulary, assessment, scheme-of-work, "
        f"culture, admin, exam-igcse, exam-alevel, ks3, ks4, ks5, french, spanish.\n\n"
        f"Document:\n\"\"\"{sample}\"\"\"\n\n"
        "Respond in exactly this format:\nSUMMARY: <text>\nTAGS:\nTAG: <topic>\nTAG: <topic>"
    )
    resp = ollama.chat(model=model, messages=[{"role": "user", "content": prompt}])
    out = resp["message"]["content"]
    m = re.search(r"SUMMARY:\s*(.*?)(?:\nTAGS:|\Z)", out, re.S)
    summary = m.group(1).strip() if m else out.strip()[:400]
    tags = re.findall(r"TAG:\s*([\w-]+)", out)
    return summary, ["#" + t for t in tags[:5]]


def write_synthesis_note(manifest: Manifest, file_path: str, summary: str,
                         tags: list[str], cfg, output_folder: str = "20-Synthesis") -> Path | None:
    if not summary:
        return None  # no real content — write nothing, not a stub
    vault = cfg.vault_path
    row = manifest.get(file_path)
    src_note = row["note_path"] if row and row["note_path"] else None
    import hashlib
    h = hashlib.sha256(file_path.encode()).hexdigest()[:6]
    name = f"S--{Path(file_path).stem.replace(' ', '_')[-80:]}-{h}"
    target = vault / output_folder / f"{name}.md"
    fm = frontmatter({
        "source_path": file_path,
        "type": "synthesis",
        "tags": ["#ingest/synthesis"] + tags,
        "related": ([f"[[{Path(src_note).stem}]]"] if src_note else []),
    })
    link_line = f"\n\nSource: [[{Path(src_note).stem}]]\n" if src_note else "\n"
    target.write_text(f"{fm}\n\n# {name}\n\n{summary}{link_line}", encoding="utf-8")
    return target


CURATED_MOCS = {
    "german": "# Map of Content: German\n\nGerman teaching resources, schemes, and assessment materials. Links to notes appear here as they are ingested.\n\n#moc #german #language/german",
    "french": "# Map of Content: French\n\nFrench teaching resources, schemes, and assessment materials. Links to notes appear here as they are ingested.\n\n#moc #french #language/french",
    "spanish": "# Map of Content: Spanish\n\nSpanish teaching resources, schemes, and assessment materials. Links to notes appear here as they are ingested.\n\n#moc #spanish #language/spanish",
    "assessment": "# Map of Content: Assessment\n\nAssessment criteria, mark schemes, and rubrics for IB/IGCSE/GCSE French and Spanish.\n\n#moc #assessment",
    "scheme-of-work": "# Map of Content: Schemes of Work\n\nUnit plans and schemes of work across KS3, KS4, KS5, IB-DP.\n\n#moc #scheme-of-work",
    "grammar": "# Map of Content: Grammar\n\nGrammar points, explanations, and practice materials for French and Spanish.\n\n#moc #grammar",
    "cpd": "# Map of Content: CPD\n\nContinuing professional development notes, training takeaways, and reflections.\n\n#moc #cpd",
    "student-work": "# Map of Content: Student Work\n\nAnonymised student work samples with error taxonomies.\n\n#moc #student-work",
    "error-taxonomy": "# Map of Content: Error Taxonomy\n\nCategorised language errors from anonymised student work: grammar, vocabulary, syntax, register, cohesion, pronunciation, spelling.\n\n#moc #error-taxonomy",
    "voice-profile": "# Map of Content: Voice Profile\n\nReusable report comment patterns extracted from the teacher's own reports.\n\n#moc #voice-profile #reports",
}


def build_mocs(cfg, manifest: Manifest) -> list[Path]:
    """Create only the 10 curated MOCs as honest gap notes. No auto-tag generation."""
    vault = cfg.vault_path
    synth_dir = vault / "20-Synthesis"
    synth_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for tag, body in CURATED_MOCS.items():
        moc = synth_dir / f"MOC--{tag}.md"
        if not moc.exists():
            moc.write_text(body + "\n", encoding="utf-8")
            created.append(moc)
    return created


def build_voice_profile_moc(cfg, manifest: Manifest) -> Path | None:
    """Build a dedicated MOC for voice profile / report templates.
    Only writes if there are real comment patterns — never a stub."""
    vault = cfg.vault_path
    voice_dir = vault / "40-Reports" / "templates"
    voice_dir.mkdir(parents=True, exist_ok=True)

    # Collect all voice patterns from reports - join with files table
    patterns = manifest.conn.execute(
        "SELECT c.text FROM chunks c "
        "JOIN files f ON c.file_path = f.path "
        "WHERE c.status='anonymised' AND f.doc_type='report'"
    ).fetchall()

    if not patterns:
        return None  # no real patterns — write nothing, not a stub

    # Filter out stubs: skip chunks that are just "N patterns extracted" placeholders
    real_patterns = [p for p in patterns if len(p["text"].strip()) > 50
                     and "patterns extracted" not in p["text"].lower()]

    if not real_patterns:
        return None  # only stubs found — write nothing

    lines = ["# Voice Profile & Report Templates", "", "## Extracted Comment Patterns", ""]
    for i, p in enumerate(real_patterns[:50], 1):
        lines.append(f"{i}. {p['text'][:200]}")
    lines += ["", "## Usage", "Copy and adapt these phrases for new reports."]

    moc = voice_dir / "voice-profile.md"
    moc.write_text("\n".join(lines), encoding="utf-8")
    return moc


def build_error_taxonomy_moc(cfg, manifest: Manifest) -> Path | None:
    """Build MOC for student error taxonomy.
    Only writes if there are real anonymised student work chunks — never a stub."""
    vault = cfg.vault_path
    error_dir = vault / "30-Student-Work" / "error-taxonomy"
    error_dir.mkdir(parents=True, exist_ok=True)

    # Check for real anonymised student work chunks
    count = manifest.conn.execute(
        "SELECT COUNT(*) FROM chunks c "
        "JOIN files f ON c.file_path = f.path "
        "WHERE c.status='anonymised' AND f.doc_type='student_work'"
    ).fetchone()[0]

    if count == 0:
        return None  # no real student work — write nothing, not a stub

    lines = ["# Student Error Taxonomy", "", "Generated from anonymized student work.", ""]
    lines += ["Categories: Grammar, Vocabulary, Syntax, Register, Cohesion, Pronunciation, Spelling", ""]
    lines += [f"Student work chunks indexed: {count}", ""]

    moc = error_dir / "error-taxonomy-index.md"
    moc.write_text("\n".join(lines), encoding="utf-8")
    return moc