"""Deterministic organisation-name matching.

Employer strings on profiles vary ("Kofax India Pvt. Ltd.", "Tungsten Automation,
a Kofax company", "Open Text"). A brief organisation matches when its normalised
name (or alias) appears as a whole run of tokens in the employer, or equals a
run of adjacent tokens joined together ("open text" -> "opentext"). Plain
substring matching is avoided: "IBM" must not match "Mibmax".

The LLM never extends this list. A company acquired by the target (e.g. a
former subsidiary) only matches if the brief names it as an alias.
"""

from __future__ import annotations

import re
import unicodedata

from ..models import BriefSpec, ExpertGroup, Organisation

_LEGAL_SUFFIXES = {
    "inc", "incorporated", "ltd", "limited", "llc", "llp", "plc", "gmbh", "ag", "sa", "sas", "bv",
    "nv", "pvt", "private", "corp", "corporation", "co", "company", "spa", "srl", "oy", "ab", "as",
    "sp", "zoo", "kg",
}


# Letters that Unicode decomposition does not reduce to ASCII ("Paweł" would become "pawe").
_FOLD = str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ß": "ss",
                       "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ı": "i", "þ": "th", "Þ": "Th"})


def fold(text: str) -> str:
    """ASCII form of ``text``, case kept: accents dropped, special letters transliterated."""
    return unicodedata.normalize("NFKD", text.translate(_FOLD)).encode("ascii", "ignore").decode()


def tokens(name: str) -> list[str]:
    text = fold(name).lower().replace("&", " and ")
    return [t for t in re.findall(r"[a-z0-9]+", text)]


def _core(name: str) -> list[str]:
    """Tokens without trailing legal-form words ("Kofax India Pvt Ltd" -> kofax india)."""
    toks = tokens(name)
    while toks:
        # "W.R. Grace & Co" and "W.R. Grace and Company" both reduce to "w r grace".
        if toks[-1] in _LEGAL_SUFFIXES or (toks[-1] == "and" and len(toks) > 1):
            toks.pop()
            continue
        # Dotted legal forms split into single letters: "S.A." -> s a, "sp. z o.o." -> sp z o o,
        # "sp. k." -> sp k. Runs of 2+ letters, or 1 letter after a legal word, so "Plan B" keeps "b".
        run = 0
        while run < len(toks) and len(toks[-1 - run]) == 1:
            run += 1
        after_suffix = run == 1 and len(toks) > 1 and toks[-2] in _LEGAL_SUFFIXES
        if (run >= 2 or after_suffix) and run < len(toks):
            del toks[-run:]
            continue
        break
    return toks


# "Cerner, now Oracle Health", "Stater (formerly X)": keep the name the page gives first.
_RENAMED = re.compile(r"\s*(?:,|\(|\s-\s)\s*(?:now|formerly|previously|part of)\b.*$", re.IGNORECASE)
# Descriptions instead of names: "A major European insurer", "Regional US bank (unnamed)".
_PLACEHOLDER = re.compile(
    r"\b(?:unnamed|anonymous|undisclosed|not named|confidential)\b"
    r"|^(?:an?|one)\s"
    r"|^(?:the\s+)?(?:large|leading|major|global|regional|top|specialist|fortune|several|various)\b",
    re.IGNORECASE,
)


def clean_org_name(name: str) -> str:
    """Strip rename notes and trailing punctuation: "Cerner, now Oracle Health" -> "Cerner"."""
    return _RENAMED.sub("", " ".join(name.split())).strip(" ,;-")


def is_placeholder_org(name: str) -> bool:
    """True for descriptions that stand in for a name ("A major European insurance company")."""
    cleaned = clean_org_name(name)
    # All-lowercase multi-word strings are descriptions ("insurance company"); "eBay" is a name.
    descriptive = " " in cleaned and cleaned == cleaned.lower()
    return not cleaned or bool(_PLACEHOLDER.search(cleaned)) or descriptive


def mentions_any(text: str | None, names: list[str]) -> bool | None:
    """Whole-word, case-insensitive mention of any name in ``text`` (None when there is no text)."""
    if not text:
        return None
    return any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text, re.IGNORECASE) for n in names if n)


def lines_mentioning(text: str | None, names: list[str], limit: int = 2, max_chars: int = 300) -> list[str]:
    """Verbatim lines of ``text`` that mention any name (whole word), for use as quotes."""
    if not text:
        return []
    found = []
    for line in text.splitlines():
        line = line.strip(" #*-	")
        if line and mentions_any(line, names):
            found.append(line[:max_chars])
            if len(found) == limit:
                break
    return found


def org_key(name: str) -> str:
    """Comparison key ignoring case, punctuation and legal form ("CLIP Group S.A." == "CLIP Group")."""
    return " ".join(_core(name))


def _acronyms(name: str) -> set[str]:
    """Upper-case words of 3-5 letters in the original name ("PKP PLK" -> {pkp, plk})."""
    return {t.lower() for t in re.findall(r"\b[A-Z]{3,5}\b", fold(name))}


def _match_at(needle: list[str], hay: list[str], i: int, acronyms: set[str]) -> bool:
    """``needle`` matches ``hay`` from ``i``, each token exactly or, for an acronym, as the
    initials of that many words ("plk" = "polskie linie kolejowe" or "polskich linii kolejowych")."""
    if not needle:
        return True
    tok, rest = needle[0], needle[1:]
    if i < len(hay) and hay[i] == tok and _match_at(rest, hay, i + 1, acronyms):
        return True
    run = hay[i:i + len(tok)]
    return (tok in acronyms and len(run) == len(tok) and "".join(w[0] for w in run) == tok
            and _match_at(rest, hay, i + len(tok), acronyms))


def same_organisation(org_name: str, other: str) -> bool:
    """``other`` is ``org_name`` itself, up to legal form, case endings and acronyms
    ("PKP Polskie Linie Kolejowe S.A." is "PKP PLK"), not just a name containing it
    ("PKP Cargo Terminale" is not "PKP Cargo"). Bracketed parts are ignored
    ("PKP Polskie Linie Kolejowe (PKP PLK)")."""
    needle, hay = _core(org_name), _core(re.sub(r"\s*\([^)]*\)", " ", other))
    if not needle or not hay:
        return False

    def full(needle_left: list[str], i: int) -> bool:
        if not needle_left:
            return i == len(hay)
        tok, rest = needle_left[0], needle_left[1:]
        if i < len(hay) and hay[i] == tok and full(rest, i + 1):
            return True
        run = hay[i:i + len(tok)]
        return (tok in acronyms and len(run) == len(tok) and "".join(w[0] for w in run) == tok
                and full(rest, i + len(tok)))

    acronyms = _acronyms(org_name)
    return full(needle, 0)


def name_matches(org_name: str, employer: str) -> bool:
    needle, hay = _core(org_name), tokens(employer)
    if not needle or not hay:
        return False
    n = len(needle)
    if any(hay[i:i + n] == needle for i in range(len(hay) - n + 1)):
        return True
    # Acronyms against full names, which also absorbs Polish case endings:
    # "UTK" matches "Urząd Transportu Kolejowego" and "Urzędu Transportu Kolejowego".
    acronyms = _acronyms(org_name)
    if acronyms and any(_match_at(needle, hay, i, acronyms) for i in range(len(hay))):
        return True
    compact = "".join(needle)
    if len(compact) < 4:
        return False
    for size in (2, 3):
        if any("".join(hay[i:i + size]) == compact for i in range(len(hay) - size + 1)):
            return True
    return False


def matching_organisations(employer: str, organisations: list[Organisation]) -> list[str]:
    """Canonical names of every brief organisation the employer string matches."""
    return [o.name for o in organisations if any(name_matches(n, employer) for n in [o.name, *o.aliases])]


def organisations_for_group(spec: BriefSpec, group: ExpertGroup) -> list[Organisation]:
    """Organisations worth tagging on a candidate's positions for this group:
    the group's named organisations, the target, and the excluded organisations."""
    found: dict[str, Organisation] = {}
    candidates = list(group.eligible_organisations)
    if spec.target_organisation:
        candidates.append(spec.target_organisation)
    candidates += [ex.organisation for ex in spec.excluded_organisations]
    for o in candidates:
        found.setdefault(o.name.lower(), o)
    return list(found.values())
