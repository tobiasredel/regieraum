"""Kleine Helfer: Textvergleich, Kürzen, URL-Normalisierung, Kosten."""
from __future__ import annotations

import difflib
import re
import secrets
import string
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_ID_ALPHABET = string.ascii_letters + string.digits + "-_"

# Preise in US-Dollar je Million Token (Stand SPEC 11/2026): Eingabe, Ausgabe, Cache-Lesen, Cache-Schreiben (5 min)
MODEL_PRICES_USD = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
WEB_SEARCH_USD = 0.01  # je Suchanfrage
# Transkription in US-Dollar je Minute Audio
STT_PRICES_USD_PER_MIN = {
    "gpt-transcribe": 0.0045,
    "gpt-4o-transcribe": 0.006,
    "gpt-4o-mini-transcribe": 0.003,
    "whisper-1": 0.006,
}


def lang_code(value) -> str:
    """Zweibuchstabiger Sprachcode aus `show.language` („de“, „de-DE“, „en_GB“ …); sonst „de“."""
    m = re.match(r"\s*([A-Za-z]{2})(?![A-Za-z])", str(value or ""))
    return m.group(1).lower() if m else "de"


def new_id() -> str:
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(12))


def usage_cost_usd(model: str, usage) -> float:
    """Kosten eines Aufrufs aus dem Usage-Objekt (oder dict) der Antwort."""
    def g(name):
        v = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        return v or 0
    p_in, p_out, p_cr, p_cw = MODEL_PRICES_USD.get(model, MODEL_PRICES_USD["claude-sonnet-5-5"])
    cost = (g("input_tokens") * p_in + g("output_tokens") * p_out
            + g("cache_read_input_tokens") * p_cr + g("cache_creation_input_tokens") * p_cw) / 1e6
    stu = usage.get("server_tool_use") if isinstance(usage, dict) else getattr(usage, "server_tool_use", None)
    if stu is not None:
        n = stu.get("web_search_requests") if isinstance(stu, dict) else getattr(stu, "web_search_requests", 0)
        cost += (n or 0) * WEB_SEARCH_USD
    return cost


# ---------------------------------------------------------------- Text

_STOP = set("""
aber alle allem allen aller alles als also am an ans auch auf aus bei beim bin bis bist da damit dann das dass dein
deine dem den denn der des dessen die dies diese diesem diesen dieser dieses doch dort du durch ein eine einem einen
einer eines er es etwa euch euer für gegen gibt hab habe haben hat hatte hier ihm ihn ihr ihre im in ist ja jetzt
kann kein keine man mal mehr mein mich mir mit muss nach nicht nichts noch nun nur ob oder ohne schon sehr sein seine
sich sie sind so soll sollte über um und uns unser unter viel vom von vor war waren warum was weil wenn wer werden
wie wieder wir wird wo zu zum zur zwar the and or of to a
""".split())

_WORD = re.compile(r"[\wÄÖÜäöüß]+", re.UNICODE)


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


def content_words(text: str) -> set[str]:
    return {w for w in words(text) if len(w) >= 4 and w not in _STOP}


def text_similarity(a: str, b: str) -> float:
    """0..1, robust gegen Satzzeichen und Groß-/Kleinschreibung."""
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def containment(short: str, long: str) -> float:
    """Anteil der Wörter von ``short``, die (in Reihenfolge) in ``long`` vorkommen."""
    ws, wl = words(short), words(long)
    if not ws or not wl:
        return 0.0
    m = difflib.SequenceMatcher(None, ws, wl, autojunk=False)
    matched = sum(b.size for b in m.get_matching_blocks())
    return matched / len(ws)


def near_duplicate_text(a: str, b: str) -> bool:
    """Nahezu gleicher Text (Übersprechen): ähnlich oder einer steckt weitgehend im anderen."""
    if text_similarity(a, b) >= 0.6:
        return True
    short, long = (a, b) if len(words(a)) <= len(words(b)) else (b, a)
    return len(words(short)) >= 3 and containment(short, long) >= 0.75


def card_overlap(a: str, b: str) -> float:
    """Ähnlichkeit zweier Karten nach Inhaltswörtern (Jaccard) bzw. Wortfolge, das Maximum."""
    ca, cb = content_words(a), content_words(b)
    jac = len(ca & cb) / len(ca | cb) if ca and cb else 0.0
    return max(jac, text_similarity(a, b))


def clip_text(text: str, limit: int) -> str:
    """Hart auf ``limit`` Zeichen kürzen, an Satz- oder Wortgrenze, mit Auslassungszeichen."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    for sep in (". ", "; ", ": ", " – ", ", "):
        i = cut.rfind(sep)
        if i >= limit * 0.6:
            return cut[: i + (1 if sep.strip() in ".;" else 0)].rstrip(" ,:–") + ("" if sep.strip() in ".;" else "…")
    i = cut.rfind(" ")
    if i >= limit * 0.5:
        cut = cut[:i]
    return cut.rstrip(" ,;:–-") + "…"


# ---------------------------------------------------------------- URLs

def normalize_url(url: str | None) -> str | None:
    """Vergleichsform einer URL: Schema/Host klein, ohne www., Fragment, Tracking-Parameter, Schluss-Slash."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        return None
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith(("utm_", "fbclid", "gclid", "mc_", "ref"))]
    path = p.path.rstrip("/")
    return urlunsplit(("https", host, path, urlencode(q), ""))


def domain_of(url: str | None) -> str:
    try:
        h = (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def extract_urls(text: str) -> list[str]:
    out = []
    for u in URL_RE.findall(text or ""):
        u = u.rstrip(".,;:!?")
        if u not in out:
            out.append(u)
    return out
