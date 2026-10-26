"""Configuration loading for the MFL second brain pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG = {
    "sources": [
        {
            "name": "onedrive-nae",
            "path": "/Users/Gee/Library/CloudStorage/OneDrive-NordAngliaEducation",
            "enabled": True,
            "include_globs": [
                "**/*.docx", "**/*.doc", "**/*.pptx", "**/*.ppt",
                "**/*.pdf", "**/*.xlsx", "**/*.msg", "**/*.txt", "**/*.md",
            ],
            "exclude_globs": ["**/~$*", "**/.~*", "**/node_modules/**"],
        },
        {
            "name": "onedrive-ladies-college",
            "path": "/Users/Gee/Library/CloudStorage/OneDrive-TheLadies'College",
            "enabled": True,
            "include_globs": ["**/*.docx", "**/*.pptx", "**/*.pdf", "**/*.txt", "**/*.md"],
            "exclude_globs": ["**/~$*"],
        },
        {
            "name": "onedrive-grammar-school",
            "path": "/Users/Gee/Library/CloudStorage/OneDrive-NordAngliaEducation/Grammar School Drive",
            "enabled": True,
            "include_globs": ["**/*.docx", "**/*.pptx", "**/*.pdf", "**/*.txt", "**/*.md"],
            "exclude_globs": ["**/~$*"],
        },
        {
            "name": "obsidian-education-gch",
            "path": "/Users/Gee/Documents/Obsidian Vault/Obsidian GCH/04 - Permanent/Education GCH",
            "enabled": True,
            "include_globs": ["**/*.md", "**/*.pdf", "**/*.docx"],
            "exclude_globs": [],
        },
        {
            "name": "obsidian-ib-exams",
            "path": "/Users/Gee/Documents/Obsidian Vault/Obsidian GCH/04 - Permanent/IB Examinations",
            "enabled": True,
            "include_globs": ["**/*.md", "**/*.pdf", "**/*.docx"],
            "exclude_globs": [],
        },
        {
            "name": "obsidian-teaching",
            "path": "/Users/Gee/Documents/Obsidian Vault/Obsidian GCH/04 - Permanent/Teaching",
            "enabled": True,
            "include_globs": ["**/*.md", "**/*.pdf", "**/*.docx"],
            "exclude_globs": [],
        },
        {
            "name": "obsidian-primary-sources",
            "path": "/Users/Gee/Documents/Obsidian Vault/Obsidian GCH/03 - Sources/Primary Sources",
            "enabled": True,
            "include_globs": ["**/*.md", "**/*.pdf", "**/*.docx"],
            "exclude_globs": [],
        },
        {
            "name": "onenote-mfl-staff",
            "path": "/Volumes/GCH/Second Brain GCH Work /OneNote Exports",
            "enabled": True,
            "include_globs": ["**/*.onepkg", "**/*.pdf"],
            "exclude_globs": [],
        },
    ],
    "vault_path": "/Volumes/GCH/Second Brain GCH Work /AI_Work-Vault",
    "state_db": "/Volumes/GCH/Second Brain GCH Work /.ingest-state/manifest.db",
    "index_db": "/Volumes/GCH/Second Brain GCH Work /.ingest-state/index.db",
    "log_dir": "/Volumes/GCH/Second Brain GCH Work /.ingest-state/logs",
    "anonymiser": {
        "preference": ["apple_pcc", "apple_device", "ollama"],
        "ollama_model": "qwen2.5:14b",
        "chunk_chars": 1200,
        "chunk_overlap": 150,
    },
    "embeddings": {
        "ollama_model": "paraphrase-multilingual",
        "dimensions": 768,
    },
    "synthesis": {
        "ollama_model": "qwen2.5:14b",
        "max_summary_chars": 1200,
    },
    "ocr": {
        "enabled": True,
        "engine": "tesseract",
        "languages": ["eng", "fra", "spa"],
        "only_if_no_text": True,
        "min_text_length": 100,
    },
    "document_type_routing": {
        "student_work": {
            "path_keywords": ["student", "Student", "exemplars", "Exemplars", "student work"],
            "anonymise_mode": "names_only",
            "synthesize": "error_taxonomy",
            "index_namespace": "student_work",
            "output_folder": "30-Student-Work",
        },
        "report": {
            "path_keywords": ["report", "Report", "reports", "Reports", "school report"],
            "anonymise_mode": "names_only",
            "synthesize": "voice_profile",
            "index_namespace": "reports",
            "output_folder": "40-Reports",
        },
        "scheme": {
            "path_keywords": ["scheme", "Scheme", "unit plan", "Unit Plan", "SOW", "sow", "curriculum", "Curriculum"],
            "anonymise_mode": "none",
            "synthesize": "standards_map",
            "index_namespace": "schemes",
            "output_folder": "20-Synthesis/unit-plans",
        },
        "assessment": {
            "path_keywords": ["exam", "Exam", "assessment", "Assessment", "markscheme", "Markscheme", "rubric", "Rubric", "past paper"],
            "anonymise_mode": "names_in_exemplars",
            "synthesize": "criteria_map",
            "index_namespace": "assessments",
            "output_folder": "20-Synthesis/assessment-blueprints",
        },
        "cpd": {
            "path_keywords": ["cpd", "CPD", "training", "Training", "workshop", "Workshop", "inset", "INSET"],
            "anonymise_mode": "none",
            "synthesize": "takeaways",
            "index_namespace": "cpd",
            "output_folder": "60-CPD",
        },
        "knowledge": {
            "path_keywords": ["primary", "Primary", "zettelkasten", "literature", "permanent", "Permanent"],
            "anonymise_mode": "none",
            "synthesize": "atomic_notes",
            "index_namespace": "knowledge",
            "output_folder": "10-Resources/by-theme",
        },
        "admin": {
            "path_keywords": ["admin", "Admin", "policy", "Policy", "handbook", "Handbook", "meeting", "Meeting", "handover", "Handover"],
            "anonymise_mode": "names",
            "synthesize": "standard",
            "index_namespace": "admin",
            "output_folder": "50-Admin",
        },
        "resource": {
            "path_keywords": ["lesson", "Lesson", "resource", "Resource", "worksheet", "Worksheet", "sentence builder", "activity", "Activity"],
            "anonymise_mode": "none",
            "synthesize": "standard",
            "index_namespace": "resources",
            "output_folder": "10-Resources",
        },
    },
    "monitor": {
        "status_file": "90-System/STATUS.md",
        "update_every_files": 25,
    },
    "crawl": {
        "delay_ms": 300,
        "extract_attempts": 3,
    },
    "tag_taxonomy_path": "90-System/tag-taxonomy.yaml",
}


@dataclass
class Source:
    name: str
    path: str
    enabled: bool
    include_globs: list[str] = field(default_factory=list)
    exclude_globs: list[str] = field(default_factory=list)
    doc_type_hint: str | None = None
    delay_ms: int | None = None  # per-source pacing; None = use global crawl delay


@dataclass
class DocumentTypeRule:
    name: str
    path_keywords: list[str]
    anonymise_mode: str
    synthesize: str
    index_namespace: str
    output_folder: str


@dataclass
class Config:
    raw: dict[str, Any]
    sources: list[Source]
    vault_path: Path
    state_db: Path
    index_db: Path
    log_dir: Path
    anonymiser: dict[str, Any]
    embeddings: dict[str, Any]
    synthesis: dict[str, Any]
    ocr: dict[str, Any]
    document_type_routing: dict[str, DocumentTypeRule]
    monitor: dict[str, Any]
    crawl: dict[str, Any]
    tag_taxonomy_path: Path

    @property
    def vault(self) -> Path:
        return self.vault_path


def load_config(path: str | Path) -> Config:
    cfg_path = Path(path)
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        if loaded:
            raw = loaded
    else:
        import sys
        print(f"[config] WARNING: config file not found: {cfg_path.resolve()} — using defaults",
              file=sys.stderr)
    merged = {**DEFAULT_CONFIG, **raw}
    sources = [
        Source(
            name=s["name"],
            path=s["path"],
            enabled=bool(s.get("enabled", True)),
            include_globs=list(s.get("include_globs", [])),
            exclude_globs=list(s.get("exclude_globs", [])),
            doc_type_hint=s.get("doc_type_hint"),
            delay_ms=s.get("delay_ms"),
        )
        for s in merged.get("sources", [])
    ]
    routing_raw = merged.get("document_type_routing", DEFAULT_CONFIG["document_type_routing"])
    document_type_routing = {
        k: DocumentTypeRule(
            name=k,
            path_keywords=v["path_keywords"],
            anonymise_mode=v["anonymise_mode"],
            synthesize=v["synthesize"],
            index_namespace=v["index_namespace"],
            output_folder=v["output_folder"],
        )
        for k, v in routing_raw.items()
    }
    return Config(
        raw=merged,
        sources=sources,
        vault_path=Path(merged["vault_path"]),
        state_db=Path(merged["state_db"]),
        index_db=Path(merged["index_db"]),
        log_dir=Path(merged["log_dir"]),
        anonymiser=merged.get("anonymiser", DEFAULT_CONFIG["anonymiser"]),
        embeddings=merged.get("embeddings", DEFAULT_CONFIG["embeddings"]),
        synthesis=merged.get("synthesis", DEFAULT_CONFIG["synthesis"]),
        ocr=merged.get("ocr", DEFAULT_CONFIG["ocr"]),
        document_type_routing=document_type_routing,
        monitor=merged.get("monitor", DEFAULT_CONFIG["monitor"]),
        crawl=merged.get("crawl", DEFAULT_CONFIG["crawl"]),
        tag_taxonomy_path=Path(merged.get("tag_taxonomy_path", DEFAULT_CONFIG["tag_taxonomy_path"])),
    )