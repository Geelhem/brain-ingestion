"""Anonymiser with tiered fallback: Apple PCC → Apple on-device → Ollama.

Apple's Foundation Models framework may route to Private Cloud Compute when the
session requires it; the SDK tiers are implemented as availability-ordered
backends so a failure at one tier falls cleanly to the next.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
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
    "own name. Do NOT include organization names, school names, place names, "
    "common words, or job titles. Do not invent entries. Return only the "
    "structured result."
)

KIND_PREFIX = {
    "person": "PER", "email": "EMA", "phone": "TEL", "date_of_birth": "DOB",
    "address": "ADR", "assessment_record": "ASR", "other": "MISC",
}

_COMMON_WORDS = {
    # --- English function words ---
    "the", "and", "for", "are", "but", "not", "you", "all", "can", "had",
    "her", "was", "one", "our", "out", "day", "get", "has", "him", "his",
    "how", "man", "new", "now", "old", "see", "two", "way", "who", "boy",
    "did", "its", "let", "put", "say", "she", "too", "use", "that", "this",
    "with", "from", "they", "them", "what", "when", "where", "will",
    "would", "could", "should", "may", "might", "must", "shall", "need",
    "about", "after", "again", "against", "been", "before", "being",
    "below", "between", "both", "during", "each", "few", "further",
    "here", "into", "more", "most", "other", "over", "same", "some",
    "such", "than", "then", "there", "through", "under", "until", "very",
    "while", "have", "were", "does", "done", "also", "just", "only",
    # --- English school/topic vocabulary (MFL corpus) ---
    "school", "staff", "student", "students", "room", "lesson", "unit",
    "plan", "past", "paper", "mark", "scheme", "grade", "record",
    "assessment", "report", "exam", "test", "class", "form", "level",
    "year", "term", "half", "course", "french", "german", "spanish",
    "english", "maths", "science", "reading", "writing", "listening",
    "speaking", "grammar", "vocabulary", "worksheet", "activity",
    "resources", "audio", "video", "homework", "revision", "plenary",
    "starter", "objectives", "outcomes", "differentiation", "curriculum",
    "education", "pearson", "aqa", "edexcel", "igcse", "gcse", "ib",
    "baccalaureate", "diploma", "examinations", "examiner", "candidate",
    "centre", "board", "publisher", "ltd", "limited", "copyright",
    "artificial", "intelligence", "google", "forms", "assistant",
    "chatbot", "methodology", "information", "studies", "library",
    "health", "psychology", "czech", "republic", "watches", "youth",
    "hostel", "museum", "montag", "zeit", "grammatical", "covert",
    "generative", "helsinki", "guidelines", "peer", "review", "tamayo",
    "ibm", "adamov", "square", "guernsey", "jersey", "london", "paris",
    "madrid", "berlin", "college", "ladies", "language", "commission",
    "kingdom", "united", "states", "description", "frequency", "tag",
    # --- French function words ---
    "le", "la", "les", "un", "une", "des", "du", "de", "et", "ou", "est",
    "sont", "dans", "sur", "pour", "avec", "sans", "chez", "comme",
    "mais", "donc", "car", "ni", "que", "qui", "quoi", "dont", "où",
    "ce", "cet", "cette", "ces", "mon", "ton", "son", "notre", "votre",
    "leur", "mes", "tes", "ses", "nos", "vos", "leurs", "je", "tu", "il",
    "elle", "nous", "vous", "ils", "elles", "on", "en", "y", "se", "sa",
    "si", "ne", "pas", "plus", "moins", "très", "bien", "tout", "tous",
    "toute", "toutes", "autre", "autres", "même", "aussi", "encore",
    "déjà", "toujours", "souvent", "parfois", "jamais", "rien",
    "quelque", "chose", "personne", "quand", "comment", "pourquoi",
    "parce", "alors", "ensuite", "puis", "avant", "après", "pendant",
    "depuis", "vers", "chez", "entre", "parmi", "selon", "malgré",
    "afin", "ainsi", "c'est", "qu'il", "qu'elle", "d'un", "d'une",
    "l'école", "l'année", "j'ai", "c'était", "n'est", "dans",
    # --- French school/topic vocabulary ---
    "français", "allemand", "espagnol", "matin", "soir", "matinée",
    "journée", "semaine", "mois", "janvier", "février", "mars", "avril",
    "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre",
    "décembre", "lundi", "mardi", "mercredi", "jeudi", "vendredi",
    "samedi", "dimanche", "école", "classe", "professeur", "élève",
    "élèves", "devoir", "devoirs", "leçon", "leçons", "exercice",
    "exercices", "examen", "contrôle", "note", "notes", "cahier",
    "cahiers", "livre", "livres", "page", "pages", "chapitre",
    "vocabulaire", "grammaire", "conjugaison", "verbe", "verbes",
    "phrase", "phrases", "mot", "mots", "texte", "textes", "question",
    "questions", "réponse", "réponses", "activités", "fiche", "fiches",
    # --- German function words (nouns are capitalized in German!) ---
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen",
    "einem", "einer", "eines", "und", "oder", "aber", "denn", "sondern",
    "ist", "sind", "war", "waren", "hat", "haben", "hatte", "wird",
    "werden", "wurde", "kann", "können", "soll", "sollen", "muss",
    "müssen", "nicht", "kein", "keine", "auch", "noch", "schon", "nur",
    "sehr", "gut", "hier", "dort", "da", "dann", "wenn", "weil", "als",
    "wie", "was", "wer", "wem", "wen", "wessen", "warum", "wieso",
    "ich", "du", "er", "sie", "es", "wir", "ihr", "mich", "dir", "ihm",
    "uns", "euch", "sich", "mein", "dein", "sein", "ihr", "unser",
    "euer", "meine", "deine", "seine", "ihre", "unsere", "eure",
    "dieser", "diese", "dieses", "jeder", "jede", "jedes", "alle",
    "manche", "etwas", "nichts", "alles", "jemand", "niemand", "man",
    "zum", "zur", "im", "in", "an", "am", "auf", "aus", "bei", "mit",
    "nach", "von", "vor", "zu", "über", "unter", "durch", "für", "um",
    "gegen", "ohne", "um", "bis", "seit", "während", "trotz", "wegen",
    "heute", "morgen", "gestern", "jetzt", "später", "früher", "immer",
    "nie", "oft", "manchmal", "selten", "gerade", "gleich", "dort",
    # --- German school/topic vocabulary ---
    "deutsch", "französisch", "spanisch", "englisch", "schule",
    "schüler", "schülerin", "lehrer", "lehrerin", "klasse", "kurs",
    "stunde", "stunden", "tag", "tage", "woche", "monat", "januar",
    "februar", "märz", "april", "mai", "juni", "juli", "august",
    "september", "oktober", "november", "dezember", "montag",
    "dienstag", "mittwoch", "donnerstag", "freitag", "samstag",
    "sonntag", "hausaufgaben", "übung", "übungen", "prüfung",
    "noten", "heft", "hefte", "buch", "bücher", "seite", "seiten",
    "kapitel", "wortschatz", "grammatik", "konjugation", "verb",
    "verben", "satz", "sätze", "wort", "wörter", "text", "texte",
    "frage", "fragen", "antwort", "antworten", "ausflug", "bild",
    "bilder", "straßenschild", "essen", "trinken", "uhr", "viertel",
    "museum", "eltern", "tagesablauf", "dialoge", "regel", "regeln",
    "audioscript", "track", "gv2", "l3", "l4", "l5", "m1", "m2", "m3",
    "m4", "m5", "ks3", "ks4", "yr", "y7", "y8", "y9", "y10", "y11",
    "lc3", "lc4", "lc5", "gv2", "gv3", "gv4", "stms", "sow", "mfl",
    "kino", "dank", "vielen", "kabine", "tasse", "tee", "glas",
    "gv1", "präsens", "vergangenheit", "futur", "wacken", "world",
    "cup", "final", "notizen", "mach", "leute", "einige", "letzten",
    "sommer", "schweizer", "bands", "finde", "informationen",
    "basler", "fasnacht", "open", "air", "tenses",
    "kinderpunsch", "cologne", "carnival", "conversation",
    "expressing", "developing", "ins", "im", "zum", "zur",
    # --- Spanish function words ---
    "el", "los", "las", "un", "una", "unos", "unas", "del", "al", "y",
    "o", "pero", "es", "son", "está", "están", "era", "eran", "fue",
    "fueron", "tiene", "tienen", "hay", "había", "ser", "estar", "en",
    "con", "sin", "por", "para", "como", "que", "qué", "quién",
    "quienes", "cuándo", "dónde", "cómo", "cuál", "cuáles", "porque",
    "si", "no", "más", "menos", "muy", "mucho", "poco", "todo", "toda",
    "todos", "todas", "otro", "otra", "otros", "otras", "mismo",
    "misma", "yo", "tú", "él", "ella", "usted", "nosotros", "vosotros",
    "ellos", "ellas", "ustedes", "me", "te", "se", "le", "lo", "la",
    "nos", "os", "les", "los", "las", "mi", "tu", "su", "nuestro",
    "vuestro", "mis", "tus", "sus", "cuando", "donde", "ya", "todavía",
    "también", "sólo", "solo", "entonces", "después", "antes",
    "durante", "desde", "hasta", "hacia", "entre", "según", "aunque",
    # --- Spanish school/topic vocabulary ---
    "español", "francés", "alemán", "inglés", "escuela", "colegio",
    "instituto", "alumno", "alumna", "alumnos", "profesor", "profesora",
    "clase", "curso", "hora", "horas", "día", "días", "semana", "mes",
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    "lunes", "martes", "miércoles", "jueves", "viernes", "sábado",
    "domingo", "deberes", "ejercicio", "ejercicios", "examen",
    "prueba", "nota", "notas", "cuaderno", "cuadernos", "libro",
    "libros", "página", "páginas", "capítulo", "vocabulario",
    "gramática", "conjugación", "verbo", "verbos", "frase", "frases",
    "palabra", "palabras", "texto", "textos", "pregunta", "preguntas",
    "respuesta", "respuestas", "actividad", "actividades", "ficha",
    "fichas", "inteligencia", "artificial",
}

_GERMAN_MARKERS = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen",
    "einem", "einer", "eines", "und", "oder", "aber", "ist", "sind",
    "war", "waren", "hat", "haben", "hatte", "wird", "werden", "wurde",
    "kann", "können", "soll", "sollen", "muss", "müssen", "nicht",
    "kein", "keine", "auch", "noch", "schon", "nur", "sehr", "hier",
    "dort", "dann", "wenn", "weil", "als", "wie", "ich", "du", "er",
    "wir", "ihr", "mich", "dir", "ihm", "uns", "euch", "sich", "mein",
    "dein", "sein", "ihre", "unsere", "dieser", "diese", "dieses",
    "alle", "etwas", "nichts", "man", "zum", "zur", "im", "an", "am",
    "auf", "aus", "bei", "mit", "nach", "von", "vor", "zu", "über",
    "unter", "durch", "für", "um", "gegen", "ohne", "bis", "seit",
}

_NON_NAME_PHRASES = {
    "ins kino", "vielen dank", "tasse tee", "glas kinderpunsch",
    "cologne carnival", "lc3 conversation", "lc4 expressing",
    "gv3 developing", "gv2 grammatical",
    "pearson education", "pearson education ltd", "reading l3", "reading l4",
    "writing l3", "writing l4", "listening l4", "mein tagesablauf",
    "guernsey language", "guernsey language commission", "language commission",
    "ladies college", "ladies college resources", "new york",
    "chinese room", "google assistant", "google forms", "health psychol",
    "methodology the", "information studies", "chatbot francis",
    "ibm watson", "adamov square", "library sc", "library science",
    "for peer review", "the helsinki guidelines", "beyond tamayo",
    "václav havel", "josef weizenbaum", "czech republic", "the snachbot",
    "feel like", "feel drawn", "tag description", "description frequency",
    "artificial intelligence", "inteligencia artificial", "covert ai",
    "generative ai", "root entry data", "default paragraph",
}

MIN_PII_LEN = 3

NAME_RE = (r"\b(?:Mr|Mrs|Ms|Miss|Mme|Mlle|Monsieur|Madame|Dr|Frau|Herr)\.?\s+[A-ZÂÛÎ][\wÂÛÎôûî'’-]+"
           r"|\b[A-ZÂÛÎ][\wÂÛÎôûî'’-]+[ \t]+[A-ZÂÛÎ][\wÂÛÎôûî'’-]+[ \t]+[A-ZÂÛÎ][\wÂÛÎôûî'’-]+\b"
           r"|\b[A-ZÂÛÎ][\wÂÛÎôûî'’-]+[ \t]+[A-ZÂÛÎ][\wÂÛÎôûî'’-]+\b"
           r"|[\w.+-]+@[\w-]+\.[\w.]+"
           r"|(?<![\w])\+?\d[\d ]{7,}\d(?![\w])"
           r"|\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b")
MAX_PII_LEN = 120
PII_KINDS = ("person", "email", "phone", "date_of_birth",
             "address", "assessment_record", "other")

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


def _apple_fm_find_pii(text: str, use_pcc: bool, timeout: int = 10) -> list[dict[str, str]]:
    """Run one Apple Foundation Models redaction call.

    `use_pcc` is an explicit routing hint: True permits Private Cloud Compute,
    False requests on-device only.  apple-fm-sdk does not expose a PCC toggle
    (the system routes automatically), so both values reach the same backend —
    callers must therefore attempt the Apple tier only ONCE (see
    anonymise_chunk), rather than retrying an identical pcc/device call.

    Hard guard: the SDK call can block inside native code, where asyncio's
    wait_for() cannot interrupt it.  Run it in a daemon thread and abandon the
    thread after `timeout` seconds so one stuck call cannot freeze the worker.
    """
    import asyncio

    if not _apple_fm_ok():
        raise RuntimeError("Apple Foundation Model unavailable")

    out: dict = {}

    def _worker() -> None:
        try:
            model = _fm.SystemLanguageModel()
            session = _fm.LanguageModelSession(model=model, instructions=SYSTEM_PROMPT)

            async def _run() -> RedactionList:
                result = await session.respond(text[:8000], generating=RedactionList)
                if isinstance(result, RedactionList):
                    return result
                return RedactionList._from_generated_content(result)

            parsed = asyncio.run(asyncio.wait_for(_run(), timeout=timeout))
            out["items"] = [{"value": i.value, "kind": i.kind} for i in parsed.items]
        except Exception as exc:  # noqa: BLE001 — surfaced to tier fallback
            out["error"] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout + 2)
    if t.is_alive():
        raise TimeoutError(f"apple fm call exceeded {timeout}s")
    if "error" in out:
        raise out["error"]
    return out.get("items", [])


# HTTP-level timeout for the Ollama client. The default client uses
# timeout=None, so a stalled connection blocks the worker thread forever
# in sock_recv (observed via stack sample). 60s read timeout makes the
# socket itself raise instead of hanging.
_OLLAMA_HTTP_TIMEOUT = 60.0
_ollama_client = None


def _get_ollama_client():
    """Lazily create a shared Ollama client with an HTTP timeout."""
    global _ollama_client
    if _ollama_client is None:
        import ollama
        _ollama_client = ollama.Client(timeout=_OLLAMA_HTTP_TIMEOUT)
    return _ollama_client


def _ollama_find_pii(text: str, model: str, timeout: int = 45) -> list[dict[str, str]]:
    """Find PII with Ollama.

    Two-layer protection:
      1. HTTP-level timeout on the client (stalled socket raises)
      2. Daemon-thread hard guard (abandons any call exceeding `timeout`)
    """
    import threading

    client = _get_ollama_client()
    prompt = (
        f"{SYSTEM_PROMPT}\n\nText:\n\"\"\"{text[:6000]}\"\"\"\n\n"
        'Reply with ONLY JSON: {"items":[{"value":"...","kind":"..."}]} or {"items":[]}'
    )

    out: dict = {}

    def _worker():
        try:
            resp = client.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                format="json",
            )
            data = json.loads(resp["message"]["content"])
            raw_items = data.get("items", [])
            # The model sometimes returns a list of strings or other junk
            # instead of objects. Normalise here so _apply never sees a
            # non-dict item (which used to crash the worker with AttributeError
            # and put the supervisor into a restart loop).
            items = []
            for entry in raw_items if isinstance(raw_items, list) else []:
                if isinstance(entry, dict):
                    value, kind = entry.get("value"), entry.get("kind", "other")
                    if isinstance(value, str) and isinstance(kind, str):
                        items.append({"value": value, "kind": kind})
            out["items"] = items
        except Exception as exc:  # noqa: BLE001 — surfaced to tier fallback
            out["error"] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"ollama call exceeded {timeout}s")
    if "error" in out:
        raise out["error"]
    return out.get("items", [])


def _placeholder(kind: str, value: str) -> str:
    """Deterministic placeholder for a value.

    Deriving the suffix from the value (rather than a per-chunk counter) means
    the same identifier always maps to the same placeholder across chunks and
    runs, and distinct identifiers never collide.  This is the source-level
    half of the collision fix; remember_pii_batch() is the defensive half.
    """
    prefix = KIND_PREFIX.get(kind, "MISC")
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    return f"[[{prefix}-{digest}]]"


def _apply(text: str, items: list[dict[str, str]], known: dict[str, tuple[str, str]],
           counter: dict[str, int] | None = None) -> tuple[str, int, list[tuple[str, str, str]]]:
    """Replace PII values in `text` using a read-only `known` snapshot.

    `counter` is accepted for backwards compatibility but no longer used —
    placeholders are deterministic (see _placeholder).
    """
    replaced = 0
    pii_entries: list[tuple[str, str, str]] = []
    for item in items:
        # LLM tiers can return malformed items (e.g. bare strings). Skip them
        # instead of crashing the whole anonymise batch.
        if not isinstance(item, dict):
            continue
        value = (item.get("value") or "").strip()
        if not isinstance(value, str):
            continue
        kind = item.get("kind", "other")
        if not isinstance(kind, str):
            kind = "other"
        if kind not in PII_KINDS:
            kind = "other"
        if not value or len(value) < MIN_PII_LEN:
            continue
        if len(value) > MAX_PII_LEN or "[[" in value:
            continue
        ph, _ = known.get(value, (None, None))
        if ph is None:
            ph = _placeholder(kind, value)
            pii_entries.append((value, ph, kind))
        pattern = re.compile(r"(?<!\w)" + re.escape(value) + r"(?!\w)")
        new_text, n = pattern.subn(ph, text)
        if n > 0:
            text = new_text
            replaced += n
    return text, replaced, pii_entries


def anonymise_chunk(
    chunk_text: str,
    manifest=None,
    cfg_anonymiser: dict | None = None,
    *,
    known: dict[str, tuple[str, str]] | None = None,
) -> tuple[str, str, int, list[tuple[str, str, str]]]:
    """Anonymise one chunk: (text, tier, replacements, pii_entries).

    `known` is a read-only snapshot of real -> (placeholder, kind).  The CLI
    passes one snapshot shared by all workers and never opens a per-chunk DB
    connection.  When omitted it is derived from `manifest` for backwards
    compatibility (e.g. cmd_incremental).
    """
    if known is None:
        known = manifest.known_pii() if manifest is not None else {}
    cfg_anonymiser = cfg_anonymiser or {}

    text = chunk_text
    for real, (ph, _k) in known.items():
        if len(real) < MIN_PII_LEN or len(real) > MAX_PII_LEN:
            continue
        if "[[" in real or real not in text:
            continue
        text = re.sub(r"(?<!\w)" + re.escape(real) + r"(?!\w)", ph, text)

    # Detect German-heavy chunks: German capitalises ALL nouns, so two-word
    # capitalized phrases there are overwhelmingly noun phrases, not names.
    # For those chunks only strong signals (titled names, emails, phones,
    # dates, 3+ word phrases) justify an LLM call.
    lower_words = re.findall(r"[a-zäöüß]+", text.lower())
    german_hits = sum(1 for w in lower_words if w in _GERMAN_MARKERS)
    is_german = german_hits >= 3

    real_match = False
    for m in re.finditer(NAME_RE, text):
        matched = m.group(0).lower()
        if matched in _NON_NAME_PHRASES:
            continue
        # Phone-pattern guard: require 9+ actual digits (rejects year
        # sequences like "2 1936 3" that the loose regex catches).
        if re.fullmatch(r"[\d ]+", matched.strip()):
            if sum(c.isdigit() for c in matched) < 9:
                continue
        words = matched.split()
        if any(w in _COMMON_WORDS for w in words):
            continue
        if is_german and len(words) == 2:
            # German noun-phrase noise — only allow title+name through
            if not any(t in matched for t in ("frau ", "herr ", "mr ", "mrs ", "ms ", "miss ", "dr ")):
                continue
        real_match = True
        break
    if not real_match:
        return text, "prefilter-clean", 0, []

    tiers = cfg_anonymiser.get("preference", ["apple_pcc", "apple_device", "ollama"])
    ollama_model = cfg_anonymiser.get("ollama_model", "qwen2.5:14b")
    # Explicit config override: use_pcc=False disables the PCC tier entirely.
    use_pcc_cfg = cfg_anonymiser.get("use_pcc")
    last_error: Exception | None = None
    apple_attempted = False
    for tier in tiers:
        if tier in ("apple_pcc", "apple_device"):
            if tier == "apple_pcc" and use_pcc_cfg is False:
                continue
            # apple-fm-sdk routes on-device/PCC automatically — both tiers hit
            # the same backend. Attempt Apple ONCE, honouring the first tier's
            # use_pcc hint, instead of making an identical second call.
            if apple_attempted:
                continue
            apple_attempted = True
            if not _apple_fm_ok():
                last_error = RuntimeError("Apple Foundation Model unavailable")
                continue
            use_pcc = (tier == "apple_pcc") if use_pcc_cfg is None else bool(use_pcc_cfg)
            try:
                items = _apple_fm_find_pii(text, use_pcc=use_pcc)
            except Exception as exc:  # noqa: BLE001 — tier fallback is the point
                last_error = exc
                continue
        elif tier == "ollama":
            try:
                items = _ollama_find_pii(text, ollama_model)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
        else:
            continue
        new_text, replaced, pii_entries = _apply(text, items, known)
        return new_text, tier, replaced, pii_entries
    raise RuntimeError(f"all anonymisation tiers failed; last error: {last_error}")
