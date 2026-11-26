"""Tests for PII junk validation, purge dry-run, and prefilter parity."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from mfl_brain.manifest import (
    PII_JUNK_RULES,
    Manifest,
    pii_junk_reason,
    pii_junk_rules,
)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import purge_pii_junk  # noqa: E402


@pytest.fixture
def manifest(tmp_path):
    m = Manifest(tmp_path / ".ingest-state" / "manifest.db")
    yield m
    m.close()


# --- validation -----------------------------------------------------------

@pytest.mark.parametrize(
    "value,kind,expected",
    [
        ("Jean Dupont", "person", None),
        ("jean.dupont@school.fr", "email", None),
        ("+33 1 23 45 67 89", "phone", None),
        ("12/03/1998", "date_of_birth", None),
        ("12 Rue de la Paix", "address", None),
        ("", "other", "empty"),
        ("   ", "other", "empty"),
        ("ab", "other", "too_short"),
        ("[[PER-0001]]", "other", "placeholder"),
        ("value with ]] token", "other", "placeholder"),
        ("x" * 61, "other", "other_too_long"),
        ("x" * 61, "person", None),  # long values allowed for non-other kinds
        ("The student completed the worksheet.", "other", "sentence_like"),
        ("This is a rather long sentence about school.", "other", "sentence_like"),
        ("one two three four five six seven eight", "other", "sentence_like"),
        ("word  with  double spaces", "other", "whitespace_heavy"),
        ("line one\nline two", "other", "whitespace_heavy"),
    ],
)
def test_pii_junk_reason(value, kind, expected):
    assert pii_junk_reason(value, kind) == expected


def test_pii_junk_rules_can_match_multiple():
    value = "The student " + "x" * 70 + "  [[PER-0001]]"
    rules = pii_junk_rules(value, "other")
    assert "other_too_long" in rules
    assert "placeholder" in rules
    assert "sentence_like" in rules
    assert set(rules) <= set(PII_JUNK_RULES)


def test_remember_pii_skips_junk(manifest):
    assert manifest.remember_pii("The student completed the worksheet.", "[[MISC-1]]", "other") == 0
    assert manifest.remember_pii("[[PER-0001]]", "[[PER-2]]", "person") == 0
    assert manifest.remember_pii("  ", "[[PER-3]]", "person") == 0
    assert manifest.known_pii() == {}

    assert manifest.remember_pii("Jean Dupont", "[[PER-0001]]", "person") == 1
    assert "Jean Dupont" in manifest.known_pii()


def test_placeholder_collision_is_resolved(manifest):
    manifest.remember_pii("Jean Dupont", "[[PER-0001]]", "person")
    manifest.remember_pii("Marie Curie", "[[PER-0001]]", "person")
    known = manifest.known_pii()
    assert known["Jean Dupont"][0] == "[[PER-0001]]"
    assert known["Marie Curie"][0] != "[[PER-0001]]"
    assert len({ph for ph, _kind in known.values()}) == len(known)


def test_placeholder_collision_within_one_batch(manifest):
    inserted = manifest.remember_pii_batch([
        ("Jean Dupont", "[[PER-0001]]", "person"),
        ("Marie Curie", "[[PER-0001]]", "person"),
        ("Hans Müller", "[[PER-0001]]", "person"),
    ])
    assert inserted == 3
    known = manifest.known_pii()
    placeholders = [ph for ph, _kind in known.values()]
    assert len(set(placeholders)) == 3


def test_remember_pii_batch_single_commit(manifest):
    manifest.remember_pii_batch([
        ("Jean Dupont", "[[PER-0001]]", "person"),
        ("Marie Curie", "[[PER-0002]]", "person"),
        ("junk sentence that should be skipped entirely", "[[MISC-1]]", "other"),
    ])
    assert len(manifest.known_pii()) == 2


# --- purge dry-run --------------------------------------------------------

def _seed_legacy_db(path: Path) -> None:
    """Seed a DB with legacy junk rows that remember_pii would now reject."""
    m = Manifest(path)
    m.conn.executemany(
        "INSERT INTO pii_map(real_value, placeholder, kind, created_at) VALUES(?,?,?,?)",
        [
            ("Jean Dupont", "[[PER-0001]]", "person", 1.0),
            ("The student completed the worksheet.", "[[MISC-1]]", "other", 1.0),
            ("[[PER-0002]]", "[[PER-0002]]", "person", 1.0),
            ("word  with  double spaces", "[[MISC-2]]", "other", 1.0),
            ("x" * 70, "[[MISC-3]]", "other", 1.0),
        ],
    )
    m.conn.commit()
    m.close()


def test_purge_scan_reports_counts_by_rule(tmp_path):
    db = tmp_path / ".ingest-state" / "manifest.db"
    _seed_legacy_db(db)

    junk, counts, total = purge_pii_junk._scan(db)
    assert total == 5
    assert len(junk) == 4
    assert counts["sentence_like"] == 1
    assert counts["placeholder"] == 1
    assert counts["whitespace_heavy"] == 1
    assert counts["other_too_long"] == 1
    assert "Jean Dupont" not in {real for real, _ph, _kind in junk}


def test_purge_dry_run_does_not_delete_or_backup(tmp_path, capsys):
    db = tmp_path / ".ingest-state" / "manifest.db"
    _seed_legacy_db(db)

    rc = purge_pii_junk.main(["--db", str(db)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY RUN" in out
    assert "counts by rule" in out
    assert not list(tmp_path.glob("*.pii-junk-backup.*"))
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM pii_map").fetchone()[0] == 5
    conn.close()


def test_purge_apply_backs_up_and_deletes(tmp_path):
    db = tmp_path / ".ingest-state" / "manifest.db"
    _seed_legacy_db(db)

    rc = purge_pii_junk.main(["--db", str(db), "--apply"])
    assert rc == 0
    backups = list(db.parent.glob("*.pii-junk-backup.*"))
    assert len(backups) == 1
    # Backup still contains all 5 rows.
    bconn = sqlite3.connect(backups[0])
    assert bconn.execute("SELECT COUNT(*) FROM pii_map").fetchone()[0] == 5
    bconn.close()
    # Live DB now contains only the valid row.
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT real_value FROM pii_map").fetchall()
    conn.close()
    assert rows == [("Jean Dupont",)]


# --- prefilter parity -----------------------------------------------------

@pytest.fixture
def no_llm(monkeypatch):
    """Make any LLM call fail loudly so prefilter-clean paths are observable."""
    import mfl_brain.anonymise as a

    def _boom(*args, **kwargs):
        raise AssertionError("LLM tier should not have been called")

    monkeypatch.setattr(a, "_apple_fm_ok", lambda: True)
    monkeypatch.setattr(a, "_apple_fm_find_pii", _boom)
    monkeypatch.setattr(a, "_ollama_find_pii", _boom)


@pytest.mark.parametrize(
    "text",
    [
        "Die Schülerinnen lernen Grammatik und Literatur.",
        "The School Lesson Plan is ready.",
        "Ins Kino geht es am Wochenende.",
        "Room 12 34 56 78 is available.",
    ],
)
def test_prefilter_rejects_noise_without_llm(text, no_llm):
    from mfl_brain.anonymise import anonymise_chunk

    out, tier, replaced, entries = anonymise_chunk(
        text, known={}, cfg_anonymiser={"preference": ["ollama"]}
    )
    assert tier == "prefilter-clean"
    assert replaced == 0
    assert entries == []
    assert out == text


def test_prefilter_allows_real_name_to_llm(monkeypatch):
    import mfl_brain.anonymise as a

    calls = []

    def _fake_ollama(text, model, timeout=45):
        calls.append(text)
        return [{"value": "Jean Dupont", "kind": "person"}]

    monkeypatch.setattr(a, "_ollama_find_pii", _fake_ollama)
    out, tier, _replaced, entries = a.anonymise_chunk(
        "Jean Dupont attended the meeting.",
        known={},
        cfg_anonymiser={"preference": ["ollama"], "ollama_model": "test"},
    )
    assert calls
    assert tier == "ollama"
    assert "Jean Dupont" not in out
    assert entries == [("Jean Dupont", entries[0][1], "person")]


def test_malformed_llm_items_do_not_crash_anonymise(monkeypatch):
    import mfl_brain.anonymise as a

    def _fake_ollama(text, model, timeout=45):
        # Model returned junk alongside one valid item (GS-568 crash-loop).
        return ["Jean Dupont", {"value": None, "kind": "person"},
                {"value": "Marie Curie", "kind": "person"}, 42]

    monkeypatch.setattr(a, "_ollama_find_pii", _fake_ollama)
    out, tier, _replaced, entries = a.anonymise_chunk(
        "Marie Curie and Jean Dupont attended the meeting.",
        known={},
        cfg_anonymiser={"preference": ["ollama"], "ollama_model": "test"},
    )
    assert tier == "ollama"
    assert "Marie Curie" not in out
    assert len(entries) == 1
    assert entries[0][0] == "Marie Curie"


def test_apple_tier_attempted_once_and_use_pcc_honoured(monkeypatch):
    import mfl_brain.anonymise as a

    calls = []

    def _fake_apple(text, use_pcc, timeout=10):
        calls.append(use_pcc)
        return [{"value": "Jean Dupont", "kind": "person"}]

    monkeypatch.setattr(a, "_apple_fm_ok", lambda: True)
    monkeypatch.setattr(a, "_apple_fm_find_pii", _fake_apple)
    out, tier, _replaced, _entries = a.anonymise_chunk(
        "Jean Dupont attended the meeting.",
        known={},
        cfg_anonymiser={"preference": ["apple_device", "apple_pcc", "ollama"]},
    )
    assert calls == [False]  # one Apple call, first tier's use_pcc hint
    assert tier == "apple_device"
    assert "Jean Dupont" not in out


def test_cli_delegates_to_anonymise_chunk(monkeypatch, tmp_path):
    """cmd_anonymise must use the shared helper, not its own prefilter."""
    import argparse

    import mfl_brain.cli as cli
    from mfl_brain.config import load_config

    cfg_path = tmp_path / "ingest.config.yaml"
    cfg_path.write_text(
        f"""
vault_path: {tmp_path / "vault"}
state_db: {tmp_path / "state.db"}
sources: []
""",
        encoding="utf-8",
    )
    (tmp_path / "vault").mkdir()
    cfg = load_config(cfg_path)
    m = Manifest(cfg.state_db)
    m.upsert_file("/tmp/a.txt", "test", 10, 1.0)
    m.add_chunks("/tmp/a.txt", ["Jean Dupont attended the meeting."])
    m.close()

    seen = []

    def _fake(text, manifest=None, cfg_anonymiser=None, *, known=None):
        seen.append((text, dict(known or {})))
        return (text, "prefilter-clean", 0, [])

    monkeypatch.setattr(cli, "anonymise_chunk", _fake)
    rc = cli.cmd_anonymise(argparse.Namespace(
        config=str(cfg_path), limit=None, quiet=True
    ))
    assert rc == 0
    assert len(seen) == 1
    assert seen[0][0] == "Jean Dupont attended the meeting."
