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
                summary = f"Error taxonomy extracted: {len(data.get('errors', []))} errors categorized."
                tags = ["#error-taxonomy", "#student-work"]
            elif doc_type == "report":
                summary = f"Voice profile: {len(data.get('voice_patterns', []))} patterns extracted."
                tags = ["#voice-profile", "#reports"]
            elif doc_type == "scheme":
                summary = f"Scheme mapped to standards: {data.get('programme', '')} {data.get('language', '')} {data.get('year', '')}."
                tags = [f"#programme/{data.get('programme','').lower()}", f"#language/{data.get('language','').lower()}"]
            elif doc_type == "assessment":
                summary = f"Assessment criteria mapped: {data.get('programme','')} {data.get('paper','')}."
                tags = [f"#assessment/{data.get('paper','').lower().replace(' ', '-')}"]
            elif doc_type == "cpd":
                summary = f"CPD takeaways: {len(data.get('takeaways', []))} concepts."
                tags = ["#cpd"]
            elif doc_type == "knowledge":
                summary = f"Knowledge note: {', '.join(data.get('key_ideas', [])[:3])}."
                tags = ["#knowledge"]
            else:
                summary = data.get("summary", "Admin document processed.")
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


def build_mocs(cfg, manifest: Manifest) -> list[Path]:
    vault = cfg.vault_path
    tag_index: dict[str, list[str]] = {}
    for folder in ["20-Synthesis", "10-Resources", "30-Student-Work", "40-Reports", "60-CPD"]:
        for f in (vault / folder).glob("*.md"):
            head = f.read_text(encoding="utf-8", errors="replace")[:1200]
            for tag in re.findall(r"#([\w-]+)", head):
                tag_index.setdefault(tag, []).append(f.stem)
    mocs: list[Path] = []
    for tag, notes in sorted(tag_index.items()):
        if len(notes) < 3:
            continue
        moc = vault / "20-Synthesis" / f"MOC--{tag}.md"
        lines = [f"# Map of Content: {tag}", ""]
        lines += [f"- [[{n}]]" for n in sorted(notes)]
        lines += ["", f"#moc #{tag}"]
        moc.write_text("\n".join(lines), encoding="utf-8")
        mocs.append(moc)
    return mocs


def build_voice_profile_moc(cfg, manifest: Manifest) -> Path | None:
    """Build a dedicated MOC for voice profile / report templates."""
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
        return None

    lines = ["# Voice Profile & Report Templates", "", "## Extracted Comment Patterns", ""]
    for i, p in enumerate(patterns[:50], 1):
        lines.append(f"{i}. {p['text'][:200]}")
    lines += ["", "## Usage", "Copy and adapt these phrases for new reports."]

    moc = voice_dir / "voice-profile.md"
    moc.write_text("\n".join(lines), encoding="utf-8")
    return moc


def build_error_taxonomy_moc(cfg, manifest: Manifest) -> Path | None:
    """Build MOC for student error taxonomy."""
    vault = cfg.vault_path
    error_dir = vault / "30-Student-Work" / "error-taxonomy"
    error_dir.mkdir(parents=True, exist_ok=True)

    # This would be populated by the error_taxonomy synthesis
    lines = ["# Student Error Taxonomy", "", "Generated from anonymized student work.", ""]
    lines += ["Categories: Grammar, Vocabulary, Syntax, Register, Cohesion, Pronunciation, Spelling", ""]

    moc = error_dir / "error-taxonomy-index.md"
    moc.write_text("\n".join(lines), encoding="utf-8")
    return moc