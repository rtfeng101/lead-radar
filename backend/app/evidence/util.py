import re
from difflib import SequenceMatcher

# NYC ZIPs: Manhattan 100-102, Staten Island 103, Bronx 104, Queens/Brooklyn 111-114 + 116.
_NYC_ZIP_PREFIXES = ("100", "101", "102", "103", "104", "111", "112", "113", "114", "116")
_LEGAL_SUFFIXES = {"INC", "INCORPORATED", "LLC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LLP", "LP"}

# Deliberately narrow: "ROOF" alone also matches rooftop solar, HVAC "on the roof floor", sidewalk sheds...
# A bare "<verb> ... roof" only counts if the roof is the thing being worked on, not something mounted on it.
_NOT_ROOF_WORK = r"(?!\s+(MOUNT\w*|TOP\w*|DECK\w*|FLOOR|LEVEL|TERRACE|GARDEN|PLAYGROUND|ANTENNA|EQUIPMENT|HATCH|ACCESS|SOLAR|PANELS?|UNITS?|SHED|BULKHEAD|PARAPET))"
_ROOFING_RE = re.compile(
    r"\b(RE-?ROOF\w*|ROOFING"
    r"|ROOF\s+(REPLACEMENT|REPAIRS?|RESTORATION|INSTALL\w*|COVERING|MEMBRANE|SYSTEM|WATERPROOFING)"
    r"|(NEW|REPLAC\w+|REPAIR\w*|REMOV\w+|TEAR[\s-]?OFF)\s+(OF\s+)?(THE\s+|EXISTING\s+|NEW\s+)*ROOF\b" + _NOT_ROOF_WORK + r")",
    re.IGNORECASE,
)


def is_nyc_zip(zip_code: str | None) -> bool:
    return bool(zip_code) and zip_code[:3] in _NYC_ZIP_PREFIXES


def phone_digits(phone: str | None) -> str | None:
    d = re.sub(r"\D", "", phone or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else None


def phone_variants(digits: str) -> list[str]:
    """Socrata stores phones in whatever format the source gave, so try each one we have seen."""
    a, b, c = digits[:3], digits[3:6], digits[6:]
    return [digits, f"({a}) {b}-{c}", f"{a}-{b}-{c}"]


def normalize_name(name: str) -> str:
    n = re.sub(r"[^A-Z0-9 ]", " ", name.upper().replace("&", " AND "))
    return " ".join(t for t in n.split() if t not in _LEGAL_SUFFIXES)


def search_prefix(name: str) -> str | None:
    """Prefix for a cheap `LIKE 'PREFIX%'` pre-filter on a source's business-name column; fuzzy matching then
    happens in Python. The first word if it is selective enough ("HARBORLINE"); for short first words ("KNA") the
    first two words. None if neither is safe, which the collector reports as "not searched"."""
    tokens = normalize_name(name).split()
    if not tokens:
        return None
    if len(tokens[0]) >= 4 and tokens[0].isalnum():
        return tokens[0]
    if len(tokens) >= 2 and tokens[1] != "AND" and tokens[0].isalnum() and tokens[1].isalnum():
        return f"{tokens[0]} {tokens[1]}"
    return None


def name_similarity(a: str, b: str, *, truncated_a: bool = False) -> float:
    """0-1 similarity of two business names. `truncated_a`: `a` came from a fixed-width source column
    (DOB NOW cuts business names at 25 chars), so compare it against the same-length prefix of `b`."""
    na, nb = normalize_name(a), normalize_name(b)
    if truncated_a:
        nb = nb[: len(na)]
    return SequenceMatcher(None, na, nb).ratio()


def is_roofing(text: str | None) -> bool:
    return bool(text and _ROOFING_RE.search(text))
