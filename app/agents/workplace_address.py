"""Night audit 2026-10-27: the workplace address a posting states, if any.

Finding the employer's coordinates was manual work for every commute check.
This reads street addresses ("Musterstraße 12, 63450 Hanau") from a posting
and labels each by what it most likely is, from the words just before it:

- WORKPLACE -- introduced as Arbeitsort / Einsatzort / Dienstort /
  Arbeitsplatz / "Standort:" / workplace / office location;
- LEGAL_SEAT -- Sitz der Gesellschaft / Registergericht / HRB / Impressum /
  Firmensitz / Hauptsitz / Zentrale / headquarters;
- AGENCY -- the posting comes from a staffing agency (its own address is
  not the client's workplace);
- UNLABELLED -- an address with no telling context.

Pure: no network. The address text itself is the employer's public data;
the candidate's home is never read or produced here. A UNLABELLED, LEGAL_SEAT
or AGENCY address is only a hint -- a route to it is never treated as the
verified workplace route.
"""

import re
from dataclasses import dataclass
from typing import Literal

AddressKind = Literal["WORKPLACE", "LEGAL_SEAT", "AGENCY", "UNLABELLED"]

_STREET_WORD = (
    r"(?:stra(?:ß|ss)e|str\.|weg|allee|platz|ring|gasse|damm|ufer|chaussee|"
    r"landstra(?:ß|ss)e|park|hof|steig|pfad|markt|berg|feld|anlage|promenade)"
)
_CAP = r"[A-ZÄÖÜ][\wäöüß\-]*"
_STREET_NOUN = (
    r"(?:Stra(?:ß|ss)e|Str\.|Weg|Allee|Platz|Ring|Gasse|Damm|Ufer|Chaussee|"
    r"Landstra(?:ß|ss)e|Park|Hof|Markt|Berg|Feld|Anlage|Promenade)"
)
_STREET = (
    # "Senckenberganlage", "Hanauer Landstraße" (suffix inside the last word)
    rf"(?:{_CAP}[ \-]){{0,3}}?[A-ZÄÖÜ][\wäöüß\-]*?{_STREET_WORD}\.?"
    # "Frankfurter Straße" (separate capitalized street noun)
    rf"|(?:{_CAP}[ \-]){{1,3}}{_STREET_NOUN}"
    # "Am Wall", "An der Hauptwache"
    rf"|(?:Am|An der|An den|Im|In der|Auf der|Zum|Zur|Hinter der)\s{_CAP}"
)
# "Frankfurt am Main", "Bad Homburg vor der Höhe" -- never the next
# sentence's first word ("Hanau. Sitz der Gesellschaft ...").
_CITY = rf"(?:(?:Bad|Sankt|St\.)\s)?{_CAP}(?:\s(?:am|an der|im|in der|bei|ob der|vor der)\s{_CAP})?"
# Only capitalized words make a street, so "Hanau und Lindenallee" is not one.
_ADDRESS = re.compile(
    rf"(?P<street>{_STREET})"
    r"\s+(?P<number>\d{1,4}\s?[a-zA-Z]?(?:\s?[-–]\s?\d{1,4}[a-zA-Z]?)?)"
    r"\s*,?\s*(?:D-)?(?P<postcode>\d{5})\s+"
    rf"(?P<city>{_CITY})"
)
_WORKPLACE = re.compile(
    r"arbeitsort|einsatzort|dienstort|arbeitsplatz|standort\s*:|work\s*place|"
    r"workplace|office\s+location|job\s+location|unser\s+(?:b(?:ü|ue)ro|standort)\s+in",
    re.IGNORECASE,
)
_LEGAL = re.compile(
    r"sitz\s+der\s+gesellschaft|registergericht|\bhrb\b|impressum|firmensitz|hauptsitz|"
    r"zentrale|headquarters|\bhq\b|gesch(?:ä|ae)ftsf(?:ü|ue)hr|ust\.?-?id",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WorkplaceAddress:
    text: str  # "Musterstraße 12, 63450 Hanau"
    postcode: str
    city: str
    kind: AddressKind


def extract_addresses(text: str, *, posted_by_agency: bool = False) -> list[WorkplaceAddress]:
    """Every street address in `text`, WORKPLACE first, each labelled."""
    found: list[WorkplaceAddress] = []
    seen: set[str] = set()
    previous_end = 0
    for match in _ADDRESS.finditer(text or ""):
        lead_start = max(previous_end, match.start() - 80)
        previous_end = match.end()
        street = " ".join(match.group("street").split())
        city = " ".join(match.group("city").split())
        number = re.sub(r"\s+", "", match.group("number"))
        label = f"{street} {number}, {match.group('postcode')} {city}"
        if label in seen:
            continue
        seen.add(label)
        # The label nearest to the address wins, and a label never reaches
        # past the previous address (Codex round 9: "Sitz der Gesellschaft:
        # <seat>. Arbeitsort: <workplace>" labels each address correctly).
        lead = text[lead_start : match.start()]
        legal = [m.end() for m in _LEGAL.finditer(lead)]
        workplace = [m.end() for m in _WORKPLACE.finditer(lead)]
        if legal and (not workplace or legal[-1] > workplace[-1]):
            kind: AddressKind = "LEGAL_SEAT"
        elif workplace:
            kind = "WORKPLACE"
        elif posted_by_agency:
            kind = "AGENCY"
        else:
            kind = "UNLABELLED"
        found.append(WorkplaceAddress(label, match.group("postcode"), city, kind))
    order = {"WORKPLACE": 0, "UNLABELLED": 1, "AGENCY": 2, "LEGAL_SEAT": 3}
    return sorted(found, key=lambda a: order[a.kind])


def address_words(text: str) -> str:
    """Spelling-insensitive form for comparing addresses: casefolded, ß/str.
    expanded, every other character a single space (word boundaries kept,
    so house number 5 never matches 50)."""
    folded = (text or "").casefold().replace("ß", "ss")
    folded = re.sub(r"str\.", "strasse", folded)
    return " ".join(re.sub(r"[^0-9a-zäöü]+", " ", folded).split())


# "Bad Homburg" and "Bad Nauheim" are different towns: a generic first word
# keeps the word after it (Codex round 13). "Frankfurt am Main" stays
# "frankfurt" so it matches a plain "Frankfurt".
_CITY_PREFIXES = frozenset(
    {"bad", "sankt", "st", "neu", "alt", "gross", "klein", "ober", "unter", "nieder", "hohen"}
)


# Qualifiers that tell same-named towns apart (Frankfurt am Main / Frankfurt
# (Oder), Codex round 14).
CITY_QUALIFIERS = frozenset({"main", "oder"})


def city_qualifiers(value: str | None) -> set[str]:
    words = re.findall(r"[a-z]+", (value or "").casefold())
    return {word for word in words[1:] if word in CITY_QUALIFIERS}


def city_key(value: str | None) -> str:
    value = (value or "").casefold().split(",")[0]
    value = re.sub(r"\d+", " ", value)
    for umlaut, plain in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        value = value.replace(umlaut, plain)
    words = re.findall(r"[a-z]+", value)
    if not words:
        return ""
    if words[0] in _CITY_PREFIXES and len(words) > 1:
        return f"{words[0]} {words[1]}"
    return words[0]


def best_workplace(
    text: str, *, posted_by_agency: bool = False, job_location: str | None = None
) -> WorkplaceAddress | None:
    """The address to measure the commute to: a labelled WORKPLACE, else the
    single UNLABELLED address -- and, when the job's own location is known,
    only one in that city (a Karlsruhe head-office address in a Frankfurt
    posting is not the workplace). Two different candidates are ambiguous.
    Never a legal seat or an agency's own address."""
    addresses = extract_addresses(text, posted_by_agency=posted_by_agency)
    city = city_key(job_location)

    def in_city(address: WorkplaceAddress) -> bool:
        return not city or city_key(address.city) == city

    workplaces = [a for a in addresses if a.kind == "WORKPLACE" and in_city(a)]
    if workplaces:
        return workplaces[0]
    unlabelled = [a for a in addresses if a.kind == "UNLABELLED" and in_city(a)]
    return unlabelled[0] if len(unlabelled) == 1 else None


# Stage 22 P1: a workplace town the posting names in prose, without a street
# address ("Standort Bruchsal", "Einsatzort: Mainz", "ausschließlich in
# Hannover"). Only a hint that the BA location may be a head office.
_STATED_TOWN = re.compile(
    r"(?i:arbeitsort|einsatzort|dienstort|standort|ausschlie(?:ß|ss)lich\s+in)\b"
    r"\s*:?\s*(?i:in\s+|ist\s+|am\s+)?(?P<rest>[^\n;]{0,90})"
)
_TOWN_NAME = r"((?:Bad |Neu-|Sankt |St\. )?[A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?)"
# "60311 Frankfurt" anywhere in the statement wins (a street may come first)
_POSTCODE_TOWN = re.compile(r"\b\d{5}\s+" + _TOWN_NAME)
_LEADING_TOWN = re.compile(_TOWN_NAME)
_NOT_A_TOWN = frozenset(
    {
        "deutschland",
        "germany",
        "hessen",
        "remote",
        "homeoffice",
        "home",
        "unser",
        "unsere",
        "unseren",
        "unserem",
        "der",
        "die",
        "das",
        "dem",
        "den",
        "nach",
        "vereinbarung",
        "rhein",  # "Rhein-Main", "Rhein-Neckar": regions, not towns
        "ruhrgebiet",
        "region",
    }
)


def stated_workplace_towns(text: str) -> set[str]:
    """City keys of workplace towns named in prose (see `_STATED_TOWN`)."""
    towns = set()
    for match in _STATED_TOWN.finditer(text or ""):
        rest = match.group("rest")
        town = _POSTCODE_TOWN.search(rest)
        if town is None:
            town = _LEADING_TOWN.match(rest)
            # a street with its house number ("Hauptstraße 5"), not a town --
            # but "Heidelberg"/"Bamberg" alone are towns (Codex P1 round 2)
            if town is None or re.match(r"\s*\d", rest[town.end() :]):
                continue
        name = town.group(1)
        if re.split(r"[ -]", name)[0].casefold() in _NOT_A_TOWN:
            continue
        key = city_key(name)
        if key:
            towns.add(key)
    return towns
