"""Atomic note splitting and wikilink conversion for Obsidian knowledge bases."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class AtomicNote:
    title: str
    content: str
    tags: list[str]
    links: list[str]  # wikilinks found in content
    source_file: str
    frontmatter: dict[str, Any]


WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
TAG_RE = re.compile(r"#([\w/-]+)")


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter from markdown content."""
    match = FRONTMATTER_RE.match(content)
    if match:
        try:
            fm = yaml.safe_load(match.group(1))
            if fm is None:
                fm = {}
            remaining = content[match.end():].lstrip()
            return fm, remaining
        except Exception:
            pass
    return {}, content


def extract_wikilinks(content: str) -> list[str]:
    """Extract all [[wikilinks]] from content."""
    return WIKILINK_RE.findall(content)


def extract_tags(content: str) -> list[str]:
    """Extract #tags from content."""
    return TAG_RE.findall(content)


def extract_headings(content: str) -> list[tuple[int, str]]:
    """Extract headings with level."""
    return [(len(m.group(1)), m.group(2).strip()) for m in HEADING_RE.finditer(content)]


def split_into_atomic_notes(content: str, source_file: str, base_tags: list[str] | None = None) -> list[AtomicNote]:
    """Split markdown content into atomic notes by headings."""
    fm, body = parse_frontmatter(content)
    base_tags = base_tags or []
    file_tags = extract_tags(body)
    all_tags = list(set(base_tags + file_tags + (fm.get("tags", []) if isinstance(fm.get("tags"), list) else [])))

    headings = extract_headings(body)
    if not headings:
        # No headings - treat whole file as one atomic note
        title = fm.get("title") or Path(source_file).stem
        return [AtomicNote(
            title=title,
            content=body.strip(),
            tags=all_tags,
            links=extract_wikilinks(body),
            source_file=source_file,
            frontmatter=fm,
        )]

    notes = []
    # Split by top-level headings (level 1-2)
    sections = []
    current_level = 0
    current_title = ""
    current_content = []

    lines = body.split("\n")
    for line in lines:
        heading_match = HEADING_RE.match(line)
        if heading_match:
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            if level <= 2 and current_title:
                sections.append((current_title, "\n".join(current_content).strip()))
            current_title = title
            current_content = [line]
            current_level = level
        else:
            current_content.append(line)
    if current_title:
        sections.append((current_title, "\n".join(current_content).strip()))

    for title, section_body in sections:
        if not section_body.strip():
            continue
        section_tags = extract_tags(section_body)
        combined_tags = list(set(all_tags + section_tags))
        notes.append(AtomicNote(
            title=title,
            content=section_body.strip(),
            tags=combined_tags,
            links=extract_wikilinks(section_body),
            source_file=source_file,
            frontmatter={**fm, "parent_file": source_file, "section": title},
        ))

    return notes


def convert_wikilinks_to_vault(content: str, vault_root: Path, source_file: Path) -> str:
    """Convert [[wikilinks]] to vault-relative links."""
    def replace_link(match):
        link = match.group(1)
        # Handle alias syntax: [[link|alias]]
        if "|" in link:
            target, alias = link.split("|", 1)
        else:
            target = link
            alias = link

        # Remove .md extension if present
        target = target.replace(".md", "").replace(".MD", "")

        # Convert to vault-relative wikilink (Obsidian handles this natively)
        # Just ensure the target exists or will exist in vault
        return f"[[{target}]]"

    return WIKILINK_RE.sub(replace_link, content)


def process_obsidian_file(source_file: Path, vault_root: Path, output_dir: Path) -> list[Path]:
    """Process a single Obsidian markdown file into atomic notes with converted links."""
    content = source_file.read_text(encoding="utf-8", errors="replace")
    atomic_notes = split_into_atomic_notes(content, str(source_file))

    written = []
    for note in atomic_notes:
        # Convert wikilinks
        converted_content = convert_wikilinks_to_vault(note.content, vault_root, Path(note.source_file))

        # Build frontmatter
        fm = {
            "source_file": note.source_file,
            "section": note.frontmatter.get("section", ""),
            "parent_file": note.frontmatter.get("parent_file", ""),
            "tags": note.tags,
            "links": note.links,
            "type": "atomic",
            "ingested": True,
        }
        fm = {k: v for k, v in fm.items() if v}

        # Create safe filename
        safe_title = re.sub(r"[^\w\s-]", "", note.title).strip().replace(" ", "_")
        if len(safe_title) > 80:
            safe_title = safe_title[:80]
        import hashlib
        h = hashlib.sha256(note.source_file.encode()).hexdigest()[:6]
        fname = f"{safe_title}-{h}.md"

        output_path = output_dir / fname
        i = 1
        while output_path.exists():
            output_path = output_dir / f"{safe_title}-{h}-{i}.md"
            i += 1

        # Write frontmatter + content
        fm_lines = ["---"]
        for k, v in fm.items():
            if isinstance(v, list):
                fm_lines.append(f"{k}:")
                fm_lines.extend(f"  - {item}" for item in v)
            else:
                fm_lines.append(f"{k}: {v}")
        fm_lines.append("---")

        output_path.write_text(
            "\n".join(fm_lines) + "\n\n" + f"# {note.title}\n\n" + converted_content,
            encoding="utf-8"
        )
        written.append(output_path)

    return written


def batch_process_obsidian_folder(
    source_dir: Path,
    vault_root: Path,
    output_base: Path,
    file_pattern: str = "**/*.md"
) -> dict[str, int]:
    """Process all markdown files in an Obsidian folder."""
    stats = {"files": 0, "atomic_notes": 0, "errors": 0}
    for md_file in source_dir.glob(file_pattern):
        if md_file.name.startswith("."):
            continue
        if not md_file.is_file():
            continue
        try:
            # Determine output subdirectory based on relative path
            rel = md_file.relative_to(source_dir)
            subdir = rel.parent if rel.parent != Path(".") else Path("")
            output_dir = output_base / subdir
            output_dir.mkdir(parents=True, exist_ok=True)

            written = process_obsidian_file(md_file, vault_root, output_dir)
            stats["files"] += 1
            stats["atomic_notes"] += len(written)
        except Exception as e:
            print(f"Error processing {md_file}: {e}")
            stats["errors"] += 1
    return stats