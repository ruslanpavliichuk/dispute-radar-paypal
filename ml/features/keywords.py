import unicodedata

# CONTEXT.md 7.3 list (nao recebi, nao chegou, atraso, defeito, quebrado, errado,
# diferente), reduced to accent-free stems so "atrasado", "defeituoso" or "quebrada" match
# too. Developer-approved on 2026-10-16.
KEYWORDS = (
    "nao recebi",
    "nao chegou",
    "atras",
    "defeit",
    "quebrad",
    "errad",
    "diferente",
)


def fold(text: str) -> str:
    """Lowercase and strip accents: "Não" -> "nao"."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def has_keyword(text: object) -> bool:
    return isinstance(text, str) and any(k in fold(text) for k in KEYWORDS)
