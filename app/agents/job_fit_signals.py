"""Stage 17C: deterministic signals read from a vacancy's own text.

Pure functions, no I/O. Each returns what the posting states plus a short
evidence snippet, or an explicit UNKNOWN -- never a guess. They feed the
independent criteria of app.services.job_fit_assessment; none of them makes
a decision on its own.
"""

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

RemoteMode = Literal["FULLY_REMOTE", "PARTIAL_REMOTE", "HYBRID", "ONSITE", "UNKNOWN"]
Cefr = Literal["A1", "A2", "B1", "B2", "C1", "C2"]
SalaryPeriod = Literal["YEAR", "MONTH"]

_STATES = {
    "baden-württemberg": "Baden-Württemberg",
    "bayern": "Bayern",
    "berlin": "Berlin",
    "brandenburg": "Brandenburg",
    "bremen": "Bremen",
    "hamburg": "Hamburg",
    "hessen": "Hessen",
    "mecklenburg-vorpommern": "Mecklenburg-Vorpommern",
    "niedersachsen": "Niedersachsen",
    "nordrhein-westfalen": "Nordrhein-Westfalen",
    "nrw": "Nordrhein-Westfalen",
    "rheinland-pfalz": "Rheinland-Pfalz",
    "saarland": "Saarland",
    "sachsen-anhalt": "Sachsen-Anhalt",
    "sachsen": "Sachsen",
    "schleswig-holstein": "Schleswig-Holstein",
    "thüringen": "Thüringen",
}
_STATE_ALTERNATION = "|".join(re.escape(name) for name in sorted(_STATES, key=len, reverse=True))


def _snippet(text: str, match: re.Match[str], width: int = 60) -> str:
    start = max(0, match.start() - width // 2)
    end = min(len(text), match.end() + width // 2)
    return " ".join(text[start:end].split())


# --- remote work ----------------------------------------------------------

# "bis zu 100% remote" is an upper bound, not a confirmed fully-remote job.
_UP_TO_REMOTE = re.compile(
    r"bis\s+zu\s+100\s*%\s*(?:remote|home\s*-?\s*office|mobil)", re.IGNORECASE
)
_FULLY_REMOTE = re.compile(
    r"(?:100\s*%|vollständig|vollstaendig|komplett|ausschließlich|ausschliesslich|full(?:y)?)"
    r"[\s-]*(?:remote|im\s+home\s*-?\s*office|home\s*-?\s*office|mobil(?:es\s+arbeiten)?)"
    r"|remote[\s-]*only|standortunabhängig|standortunabhaengig"
    r"|(?:deutschlandweit|bundesweit)[\s-]*remote|remote[\s(]*(?:deutschlandweit|bundesweit)"
    r"|arbeitsort\s*:\s*remote",
    re.IGNORECASE,
)
_FULLY_REMOTE = re.compile(
    _FULLY_REMOTE.pattern + r"|home\s*-?\s*office\s*(?:zu\s*)?100\s*%|remote\s*\(\s*100\s*%",
    re.IGNORECASE,
)
# "Remote Germany", "Remote EU", "Germany-wide remote": a stated region of work, weaker than
# "100 % remote". Only used when the posting names no hybrid / on-site rule (see classify_remote).
_REMOTE_REGIONAL = re.compile(
    r"remote[\s(/-]*(?:in\s+|within\s+)?(?:germany|deutschland|eu|europe|europa|dach)\b"
    r"|\b(?:germany|deutschland|eu|europe|europa|dach)[\s)/-]*remote\b",
    re.IGNORECASE,
)
_NO_REMOTE = re.compile(
    r"kein(?:e|en)?\s+(?:home\s*-?\s*office|remote|mobiles\s+arbeiten)"
    r"|(?:ausschließlich|ausschliesslich|nur)\s+vor\s+ort",
    re.IGNORECASE,
)
_HYBRID = re.compile(
    r"\bhybrid|präsenztag|praesenztag|\d\s*(?:tage?|x)\s*(?:pro\s+woche|/\s*woche|wöchentlich)?"
    r"\s*(?:im\s+büro|im\s+buero|vor\s+ort|präsenz)",
    re.IGNORECASE,
)
_SOME_REMOTE = re.compile(
    r"remote|home\s*-?\s*office|mobiles?\s+arbeit|telearbeit|heimarbeit", re.IGNORECASE
)
_REMOTE_REGION = re.compile(
    r"(?:remote|home\s*-?\s*office|mobil\w*)[^.;\n]{0,40}?(?:aus|in|innerhalb(?:\s+von)?)\s+"
    r"(?:dem\s+|der\s+)?(" + _STATE_ALTERNATION + r")\b",
    re.IGNORECASE,
)


# Negation is clause-scoped: "kein Homeoffice", "Remote Germany ist nicht möglich", "Vollzeit
# bieten wir nicht an". A negator in another clause never counts, so "100 % Remote. Büropräsenz
# ist nicht erforderlich." keeps the remote statement affirmative. A clause ends at . ; , newline
# or a conjunction ("... mandatory and Python is preferred" are two clauses).
_CLAUSE_BOUND = re.compile(
    r"[.;,\n!?]|\b(?:and|und|but|aber|while|whereas|sowie|jedoch)\b", re.IGNORECASE
)
_NEGATOR = re.compile(
    r"\b(?:kein|keine|keinen|keiner|keinem|nicht|ohne|no|not|without|never|nie|cannot|can't"
    r"|unable|leider)\b",
    re.IGNORECASE,
)
# "No exceptions" / "not only" are emphasis, not a negation of what they stress.
_NEGATION_IDIOM = re.compile(
    r"\bno\s+(?:exceptions?|doubt|worries)\b|\bnot\s+only\b|\bkeine\s+ausnahmen?\b|\bohne\s+ausnahme\b",
    re.IGNORECASE,
)


def _has_negator(text: str) -> bool:
    return bool(_NEGATOR.search(_NEGATION_IDIOM.sub(" ", text)))


_NEGATOR_NEAR_BEFORE = 6  # words
# "No remote experience required", "Remote-Erfahrung": a requirement about skills or equipment,
# not a statement about whether the job can be done remotely.
_REMOTE_SKILL_NOUN = re.compile(
    r"^\w*[\s-]*(?:experience|erfahrung|kenntnisse|skills?|tools?|setup|ausstattung|equipment)",
    re.IGNORECASE,
)


def _clause(text: str, start: int, end: int) -> tuple[str, str]:
    """(the part of the clause before `start`, the part after `end`)."""
    # The clause boundaries of a text are computed once and then bisected: rescanning the text
    # for every mention is quadratic on a page that repeats a word thousands of times.
    starts, ends = _clause_bounds_of(text)
    i = bisect_right(ends, start)
    left = ends[i - 1] if i else 0
    j = bisect_left(starts, end)
    return text[left:start], text[end : starts[j] if j < len(starts) else len(text)]


@lru_cache(maxsize=4)
def _clause_bounds_of(text: str) -> tuple[list[int], list[int]]:
    bounds = list(_CLAUSE_BOUND.finditer(text))
    return [m.start() for m in bounds], [m.end() for m in bounds]


def _negated(text: str, match: re.Match[str]) -> bool:
    before, after = _clause(text, match.start(), match.end())
    near_before = " ".join(before.split()[-_NEGATOR_NEAR_BEFORE:])
    return bool(_has_negator(near_before) or _has_negator(after))


def _affirmed(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    """The first match of `pattern` that is not negated inside its own clause."""
    for match in pattern.finditer(text):
        if not _negated(text, match):
            return match
    return None


# Office presence. STRONG phrases veto a full-remote claim on their own; WEAK vocabulary (a Büro
# mentioned in a perk, a site, onboarding, customer travel) vetoes only together with an
# obligation or frequency cue in the same clause. Remote work is "100 %" only if every vetoing
# clause is negated ("keine Büropräsenz") or made optional ("kannst optional ins Büro").
_PRESENCE_STRONG = re.compile(
    r"\bon-?\s?site\b|\b\w*pr(?:ä|ae)senz\w*|\bpresence\b|\battend\w*|\banwesen\w*"
    r"|\bvor[\s-]+ort[\s-]+(?:arbeit\w*|pflicht|termine?)"
    r"|\b(?:jeden|every|each)\s+(?:montag|dienstag|mittwoch|donnerstag|freitag"
    r"|monday|tuesday|wednesday|thursday|friday)\b",
    re.IGNORECASE,
)
_PRESENCE_WEAK = re.compile(
    r"\bvor[\s-]+ort\b|\b\w*b(?:ü|ue)ro\w*|(?<!home )(?<!home-)\boffices?\b|\bheadquarters?\b"
    r"|\bhauptsitz\b|\b(?:unser(?:em|er)?|our)\s+(?:standort|site|location)\b"
    r"|\bonboarding\b|\beinarbeitung\b|\bkick-?off\b"
    r"|\bteam[\s-]?(?:meetings?|treffen|events?)\s+(?:in|at|im)\s+(?-i:[A-ZÄÖÜ])",
    re.IGNORECASE,
)
# Office time the employee may choose is not a presence rule (own name: the language section
# defines a different _OPTIONAL further down).
_OFFICE_OPTIONAL = re.compile(
    r"\boptional\w*|\bfreiwillig\w*|\bvoluntar\w*|\bauf\s+wunsch\b|\bnach\s+wunsch\b"
    r"|\bif\s+you\s+(?:want|like|wish|prefer)\b|\bwenn\s+du\s+(?:willst|m(?:ö|oe)chtest)\b"
    r"|\bkannst\b|\bsie\s+k(?:ö|oe)nnen\b|\byou\s+(?:can|may)\b|\bdu\s+kannst\b",
    re.IGNORECASE,
)


# Wording that makes office presence an obligation ("must attend", "jeden Dienstag", "2x/Woche")
_OBLIGATION = re.compile(
    r"\bmust\b|\bmandatory\b|\brequired?\b|\bpflicht\w*|\bzwingend\b|\berforderlich\b"
    r"|\battend\w*|\b(?:jeden|every|each)\b|\b\d\s*(?:x|times|mal|tage?|days?)\b|\btwice\b"
    r"|\bweekly\b|\bw(?:ö|oe)chentlich\b|\bmonatlich\w*|\bmonthly\b|\bquartals\w*|\bquarterly\b"
    r"|\bregelm(?:ä|ae)(?:ß|ss)ig\w*|\bregularly\b|\bfindet\b[^.;\n]{0,60}\bstatt\b"
    r"|\btakes?\s+place\b|\bverpflichtend\b|\berfolgt\b|\bexpected\b|\bwird\s+erwartet\b"
    r"|\berwarten\b|\bmuss\w*|\bm(?:ü|ue)ssen\b|\bsollst\b|\bsollten?\b|\bshould\b"
    r"|\b(?:have|has|need|needs)\s+to\b|\bshort\s+notice\b|\bkurzfristig\w*",
    re.IGNORECASE,
)


# "(no) office attendance required / needed", "Büropräsenz nicht erforderlich"
_NOT_NEEDED = re.compile(
    r"\b(?:nicht|not|no|kein\w*)\W+(?:\w+\W+){0,2}?(?:required|needed|necessary|erforderlich"
    r"|n(?:ö|oe)tig|notwendig|vorgeschrieben|expected|verpflichtend)\b",
    re.IGNORECASE,
)


def _near(text: str, match: re.Match[str], words: int = 4) -> str:
    """The few words right before and after a match, inside its clause."""
    before, after = _clause(text, match.start(), match.end())
    return " ".join(before.split()[-words:] + after.split()[:words])


# Unambiguous obligation. Deliberately not "required" / "Pflicht" ("keine Pflicht", "not required").
_STRONG_OBLIGATION = re.compile(
    r"\bmandatory\b|\bmust\b|\bzwingend\b|\bverpflichtend\b|\bnot\s+optional\b"
    r"|\brequired\s+(?:office|on-?site|attendance|presence|b(?:ü|ue)ro|pr(?:ä|ae)senz)"
    r"|\bnicht\s+(?:optional|freiwillig)\b",
    re.IGNORECASE,
)


# A negator directly before a strong obligation word cancels that word ("kein zwingender Büro...").
_NEGATED_STRONG = re.compile(
    r"\b(?:no|not|kein\w*|nicht|ohne|without)\s+(?:\w+\s+)?(?:mandatory|zwingend\w*"
    r"|verpflichtend\w*|must)\b",
    re.IGNORECASE,
)
# Perks and facilities, not working arrangements.
_BENEFIT = re.compile(
    r"\byoga\b|\bkurs\w*|\bclasses\b|\bparking\b|\bparkplatz\w*|\bfitness\w*|\bmassage\w*"
    r"|\bkantine\b|\bcanteen\b|\bsnacks?\b|\bobst\b|\bfruit\b|\bgetr(?:ä|ae)nke\b|\bbenefits?\b"
    r"|\bgratis\b|\bkostenlos\w*|\bfree\b|\bperks?\b|\bbieten\b|\banbieten\b|\bangebot\w*|\bwe\s+offer\b|\bwe\s+provide\b",
    re.IGNORECASE,
)


def _presence(text: str) -> re.Match[str] | None:
    """Office vocabulary that stands. Negation or optionality must sit right next to the phrase
    ("keine Büropräsenz", "no office attendance required", "kannst optional ins Büro"); an
    unrelated "no exceptions" or "so you can meet the team" in the clause does not cancel it."""
    candidates = [(m, True) for m in _PRESENCE_STRONG.finditer(text)]
    candidates += [(m, False) for m in _PRESENCE_WEAK.finditer(text)]
    for match, strong in sorted(candidates, key=lambda pair: pair[0].start()):
        before, after = _clause(text, match.start(), match.end())
        near_before = " ".join(before.split()[-_NEGATOR_NEAR_BEFORE:])
        after_head = " ".join(after.split()[:6])
        clause = f"{before} {match.group(0)} {after}"
        if _BENEFIT.search(clause):
            continue  # "Weekly yoga classes in our office": a perk, not a working arrangement
        if not strong and not _OBLIGATION.search(clause):
            continue  # a site or customer travel with no obligation cue
        # "No mandatory attendance" is not mandatory; "No exceptions to mandatory ..." still is
        if _STRONG_OBLIGATION.search(_NEGATED_STRONG.sub(" ", clause)):
            return match  # "mandatory", "not optional": nothing else in the clause cancels it
        if (
            _has_negator(near_before)
            or _NOT_NEEDED.search(after_head)
            or _OFFICE_OPTIONAL.search(_near(text, match))
        ):
            continue
        return match
    return None


# Countries outside Germany. US / USA / UK are case-sensitive ("us" is a pronoun).
_COUNTRY = (
    r"(?-i:US|USA|UK|U\.S\.)|(?:the\s+)?united\s+states|(?:the\s+)?united\s+kingdom"
    r"|great\s+britain|canada|australia|india|brazil|mexico|poland|polen|austria|(?:ö|oe)sterreich"
    r"|switzerland|schweiz|france|frankreich|spain|spanien|italy|italien|netherlands"
    r"|niederlande|ireland|irland|portugal|romania|rum(?:ä|ae)nien|usa|den\s+usa"
    r"|den\s+vereinigten\s+staaten|gro(?:ß|ss)britannien|belgium|belgien|czech\w*|tschechien"
    r"|denmark|d(?:ä|ae)nemark|sweden|schweden|norway|norwegen|finland|finnland|greece"
    r"|griechenland|hungary|ungarn|turkey|t(?:ü|ue)rkei|china|japan|singapore|south\s+africa"
    r"|luxembourg|luxemburg|bulgaria|bulgarien|croatia|kroatien|serbia|serbien|ukraine"
)
_APPLICANT = r"(?:candidates?|applicants?|bewerber\w*|mitarbeiter\w*|talent|residents?|citizens?)"
# A restriction names a country as the place the APPLICANT works from. A company or customer
# location ("UK-based company", "serves UK residents") does not.
_COUNTRY_RESTRICTION = re.compile(
    rf"\b(?:{_COUNTRY})[\s-]*(?:only|nur)\b"
    rf"|\b(?:{_COUNTRY})[\s-]*based\s+{_APPLICANT}\s+(?:only|nur|are\s+(?:eligible|required)"
    rf"|may\s+apply|wanted|needed|required)\b"
    rf"|\bnur\s+(?:aus|in|f(?:ü|ue)r)\s+(?:dem\s+|der\s+|den\s+)?(?:{_COUNTRY})\b"
    rf"|\bonly\s+{_APPLICANT}\s+(?:of|from|in|based|located|living|residing)\s+(?:in\s+)?"
    rf"(?:the\s+)?(?:{_COUNTRY})\b"
    rf"|\b{_APPLICANT}\s+of\s+(?:the\s+)?(?:{_COUNTRY})\s+only\b"
    rf"|\b{_APPLICANT}[^.;\n]{{0,30}}?\b(?:based|located|living|residing|wohnhaft|wohnsitz)\s+in\s+"
    rf"(?:the\s+)?(?:{_COUNTRY})\b"
    rf"|\b(?:remote|home\s*-?\s*office|mobil\w*)[^.;\n]{{0,40}}?\b(?:only\s+)?(?:in|from|within|aus)\s+"
    rf"(?:the\s+)?(?:{_COUNTRY})\b"
    rf"|\bmust\s+(?:be\s+)?(?:located|based|reside|living)\s+in\s+(?:the\s+)?(?:{_COUNTRY})\b"
    rf"|\b(?:permitted|allowed|erlaubt|possible|m(?:ö|oe)glich)\s+(?:only\s+|nur\s+)(?:from|in|aus)\s+"
    rf"(?:the\s+)?(?:{_COUNTRY})\b"
    rf"|\bonly\s+(?:from|in)\s+(?:the\s+)?(?:{_COUNTRY})\b"
    rf"|\b(?:restricted|limited|beschr(?:ä|ae)nkt)\s+(?:to|auf)\s+(?:the\s+)?(?:{_COUNTRY})\b"
    # nationality adjective + residency/citizenship ("Polish residency is required")
    r"|\b(?:polish|british|american|french|spanish|italian|dutch|swiss|austrian|belgian|czech"
    r"|irish|portuguese|romanian|canadian|indian|polnische\w*|britische\w*|franz(?:ö|oe)sische\w*"
    r"|(?:ö|oe)sterreichische\w*|schweizer\w*)\s+(?:residen\w*|citizen\w*|staatsb(?:ü|ue)rger\w*"
    r"|nationality|work\s+permit|wohnsitz)"
    rf"|\b(?:requires?|erfordert)\s+(?:residen(?:cy|ce)|wohnsitz)\s+in\s+(?:the\s+)?(?:{_COUNTRY})\b",
    re.IGNORECASE,
)
_BROAD = r"germany|deutschland|europe|europa|\beu\b|emea|dach|worldwide|global|weltweit"
# Germany/Europe is an ALTERNATIVE place of work only when joined to the country by or/und,
# a slash or "incl." ("UK or Germany", "Poland / Deutschland"). ", Germany is our largest
# market" or ", global customer base" are not alternatives.
_JOIN = r"(?:/|\+|&|or|oder|and|und|incl\.?|inkl\.?|including|einschlie(?:ß|ss)lich)"
_ARTICLE = r"(?:in\s+|im\s+|from\s+|aus\s+|within\s+)?(?:the\s+)?"
_ALTERNATIVE_AFTER = re.compile(rf"^\s*{_JOIN}\s*{_ARTICLE}(?:{_BROAD})", re.IGNORECASE)
_ALTERNATIVE_BEFORE = re.compile(rf"(?:{_BROAD})\s*{_JOIN}\s*{_ARTICLE}$", re.IGNORECASE)
# A stated German region of work for remote jobs ("Remote in Deutschland", "Poland or Germany").
_GERMAN_REMOTE = re.compile(
    r"(?:remote|home\s*-?\s*office|mobil\w*)[^.;\n]{0,40}?(?:\b(?:in|aus|from|within|or|oder|and|und)|/)\s*"
    r"(?:the\s+)?(?:germany|deutschland)\b|\b(?:deutschland|bundes)weit",
    re.IGNORECASE,
)
_FOREIGN_MENTION = re.compile(rf"\b(?:{_COUNTRY})\b", re.IGNORECASE)
# Wording that limits WHO may take the job. Unresolved (no stated German region) -> review.
_ELIGIBILITY_WORDING = re.compile(
    r"\bonly\b|\bnur\b|\bausschlie(?:ß|ss)lich\b|\bexclusively\b|\beligible\b|\bresidents?\b"
    r"|\bcitizens?\b|\bwohnsitz\b|\bwohnhaft\b|\bwork\s+(?:permit|authori[sz]ation)\b"
    r"|\barbeitserlaubnis\b|\bvisa\b|\bans(?:ä|ae)ssig\b|\bresid(?:e|es|ing)\b|\bliv(?:e|es|ing)\s+in\b"
    r"|\bbased\s+in\b|\blocated\s+in\b|\bpermitted\b|\ballowed\b|\berlaubt\b"
    r"|\brestricted\b|\blimited\s+to\b|\bbeschr(?:ä|ae)nkt\b|\bresiden(?:cy|ce)\b",
    re.IGNORECASE,
)
_ELIGIBILITY_SUBJECT = re.compile(
    r"remote|home\s*-?\s*office|candidates?|applicants?|bewerb\w*|resident|resid|wohn|based|located"
    r"|work\b|arbeit\w*|eligible|permit|visa|citizen|liv(?:e|ing)|you\b|du\b|sie\b|erlaubt|allowed"
    r"|position|stelle|\brole\b|\bjob\b|vacancy|rolle",
    re.IGNORECASE,
)
# A broad region named in the SAME clause resolves the wording ("only within the EU").
_BROAD_REGION = re.compile(rf"\b(?:{_BROAD})", re.IGNORECASE)


_ADDITIVE = re.compile(
    r"\balso\s+(?:available|possible|permitted|allowed|an\s+option)\b"
    r"|\b(?:auch|ebenfalls|zus(?:ä|ae)tzlich)\s+(?:m(?:ö|oe)glich|erlaubt|angeboten)\b"
    r"|\bas\s+well\b|\bin\s+addition\s+to\b",
    re.IGNORECASE,
)


def _country_restriction(text: str) -> str | None:
    for match in _COUNTRY_RESTRICTION.finditer(text):
        after = text[match.end() : match.end() + 40]
        before = text[max(0, match.start() - 40) : match.start()]
        if _ALTERNATIVE_AFTER.match(after) or _ALTERNATIVE_BEFORE.search(before):
            continue
        before_words = " ".join(text[max(0, match.start() - 60) : match.start()].split()[-3:])
        if _has_negator(before_words) or _has_negator(match.group(0)):
            continue  # "need not be based in the UK"; "no exceptions" later on does not count
        clause_before, clause_after = _clause(text, match.start(), match.end())
        if _ADDITIVE.search(clause_before + " " + clause_after):  # "also available from Poland"
            continue
        return " ".join(match.group(0).split())[:40]
    return None


def _unresolved_eligibility(text: str) -> bool:
    """A clause limits who may take the job or where it may be done (only / nur / residents /
    must reside / permitted ...) without naming Germany, the EU or another broad region IN THE
    SAME CLAUSE. A region stated elsewhere does not resolve it ("Fully remote EU. Work is
    permitted only from Poland.")."""
    for match in _ELIGIBILITY_WORDING.finditer(text):
        before, after = _clause(text, match.start(), match.end())
        clause = before + " " + match.group(0) + " " + after
        if _negated(text, match) or _BROAD_REGION.search(clause):
            continue
        if _ELIGIBILITY_SUBJECT.search(clause):
            return True
    return False


@dataclass(frozen=True)
class RemoteSignal:
    mode: RemoteMode
    # A German state or a foreign country the remote work is restricted to, if the posting says so.
    region_restriction: str | None = None
    # Remote work limited to a country outside Germany ("Remote US only"); never a commute question.
    country_restriction: str | None = None
    evidence: str | None = None
    # Remote work is stated but who may do it is unclear (foreign country named, "only ...").
    # Never a confirmed FULLY_REMOTE; the operator decides.
    eligibility_unverified: bool = False


# A denial AFTER the keyword is a predicate ("ist nicht möglich", "is not available"), not any
# later "no" ("... with no office attendance required").
_DENIAL_AFTER = re.compile(
    r"\b(?:nicht|not|kein\w*|never|nie)\b\W+(?:\w+\W+){0,2}?(?:m(?:ö|oe)glich|possible|available"
    r"|offered|verf(?:ü|ue)gbar|vorgesehen|an|angeboten|supported|gew(?:ä|ae)hrt|erlaubt|allowed"
    r"|der\s+fall|permitted|gestattet|infrage|in\s+frage)\b"
    r"|\bunavailable\b|\bnicht\s+(?:\w+\s+){0,3}?(?:infrage|in\s+frage)\b"
    r"|\bno\s+longer\b|\bnot\s+an\s+option\b|\bkeine\s+option\b|\bnicht\s+mehr\b"
    # a prohibition carries its own negation: "Remote work is prohibited / ist ausgeschlossen"
    r"|\b(?:prohibited|forbidden|banned|ausgeschlossen|untersagt|verboten)\b",
    re.IGNORECASE,
)


def _affirmed_phrase(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    """Like `_affirmed`, for remote-work phrases: negated only by a negator right before the
    phrase or a denial predicate after it, not by any later "no" in the clause."""
    for match in pattern.finditer(text):
        before, after = _clause(text, match.start(), match.end())
        near_before = " ".join(before.split()[-_NEGATOR_NEAR_BEFORE:])
        if _has_negator(near_before) or _DENIAL_AFTER.search(after):
            continue
        return match
    return None


_SKILL_BETWEEN = re.compile(
    r"\b(?:experience|erfahrung|kenntnisse|skills?|equipment|ausstattung|knowledge)\b",
    re.IGNORECASE,
)


def _remote_denied(text: str) -> bool:
    """A remote/home-office mention that is denied in its own clause ("kein Homeoffice",
    "unable to support working remotely", "Remote ist nicht möglich"), ignoring requirements
    about remote SKILLS and unrelated negations ("no office attendance required")."""
    for match in _SOME_REMOTE.finditer(text):
        if _REMOTE_SKILL_NOUN.match(text[match.end() : match.end() + 30].lstrip(" -")):
            continue
        before, after = _clause(text, match.start(), match.end())
        near_before = " ".join(before.split()[-_NEGATOR_NEAR_BEFORE:])
        if _DENIAL_AFTER.search(after):
            return True  # "... for remote work because it is not permitted"
        if _SKILL_BETWEEN.search(near_before):
            continue  # "No prior experience is necessary for remote work"
        if _has_negator(near_before):
            return True
    return False


def classify_remote(text: str) -> RemoteSignal:
    """FULLY_REMOTE (which skips the commute gate) needs clean affirmative evidence: a full-remote
    phrase, no office/presence vocabulary outside a negated or optional clause, no denial of remote
    work anywhere, and no unresolved eligibility wording. Everything else fails towards HYBRID,
    ONSITE or PARTIAL_REMOTE, never towards skipping the gate."""
    restriction = None
    region = _REMOTE_REGION.search(text)
    if region:
        restriction = _STATES[region.group(1).lower()]
    country = _country_restriction(text) if _SOME_REMOTE.search(text) else None

    def done(mode: RemoteMode, match: re.Match[str], unverified: bool = False) -> RemoteSignal:
        return RemoteSignal(mode, restriction, country, _snippet(text, match), unverified)

    unresolved = _unresolved_eligibility(text) if _SOME_REMOTE.search(text) else False
    onsite = _NO_REMOTE.search(text)
    if onsite:
        return done("ONSITE", onsite)
    up_to = _UP_TO_REMOTE.search(text)
    if up_to:
        return done("PARTIAL_REMOTE", up_to, unresolved)
    some = _SOME_REMOTE.search(text)
    if some and _remote_denied(text):
        # remote work is denied somewhere in the text: whatever else it says, it is on-site
        return done("ONSITE", some)
    hybrid = _affirmed_phrase(_HYBRID, text)
    if hybrid:
        return done("HYBRID", hybrid, unresolved)
    full = _affirmed_phrase(_FULLY_REMOTE, text)
    regional = _affirmed_phrase(_REMOTE_REGIONAL, text)
    if full or regional:
        confirmed = full or regional
        presence = _presence(text)
        if presence:
            # office vocabulary next to a full-remote claim vetoes it (never labels an ad on
            # its own): a stated obligation is HYBRID, a bare mention stays unverified remote
            near_before, near_after = _clause(text, presence.start(), presence.end())
            clause = f"{near_before} {presence.group(0)} {near_after}"
            mode: RemoteMode = "HYBRID" if _OBLIGATION.search(clause) else "PARTIAL_REMOTE"
            # a bare mention leaves a full-remote CLAIM unconfirmed: the operator reads it
            return done(mode, presence, unresolved or mode == "PARTIAL_REMOTE")
        german = bool(regional or _GERMAN_REMOTE.search(text))
        foreign = bool(_FOREIGN_MENTION.search(text)) and not german
        if unresolved or foreign:
            # who may do this remote job is unclear: not confirmed as workable from Germany
            return done("PARTIAL_REMOTE", confirmed, unverified=True)
        return done("FULLY_REMOTE", confirmed)
    if some:
        return done("PARTIAL_REMOTE", some, unresolved)
    return RemoteSignal("UNKNOWN")


# --- German language requirement -----------------------------------------

_GERMAN = re.compile(r"deutsch(?!land)\w*|german(?!y)", re.IGNORECASE)
_CEFR = re.compile(r"(?<![A-Za-z0-9])([ABC][12])(?![0-9])")
_DESCRIPTIVE: tuple[tuple[re.Pattern[str], Cefr], ...] = (
    (re.compile(r"muttersprach|native", re.IGNORECASE), "C2"),
    (re.compile(r"verhandlungssicher|fließend|fliessend|fluent|exzellent", re.IGNORECASE), "C1"),
    (re.compile(r"sehr\s+gut\w*|ausgezeichnet\w*|excellent|very\s+good", re.IGNORECASE), "C1"),
    (re.compile(r"\bgut(?:e|en)?\b|\bgood\b|solide", re.IGNORECASE), "B2"),
    (re.compile(r"grundkenntnisse|basic|grundlegend", re.IGNORECASE), "A2"),
)
# The first entries of _DESCRIPTIVE are fluency/native wording (Stage 22 P1).
_STRONG_DESCRIPTORS = 2
_OPTIONAL = re.compile(
    r"wünschenswert|wuenschenswert|von\s+vorteil|idealerweise|nice\s+to\s+have|ein\s+plus"
    r"|optional|gerne|bevorzugt|preferred",
    re.IGNORECASE,
)


RequirementBasis = Literal[
    "EXPLICIT_REQUIREMENT", "INFERRED_REQUIREMENT", "PREFERRED_REQUIREMENT", "UNKNOWN"
]


@dataclass(frozen=True)
class LanguageRequirement:
    level: Cefr | None
    # False when the posting softens it (von Vorteil, wünschenswert, ...).
    mandatory: bool | None
    evidence: str | None = None
    # Night audit 2026-10-27: "sehr gute Deutschkenntnisse" names no CEFR
    # level or certificate; the C1 read from it is an interpretation
    # (INFERRED_REQUIREMENT), unlike a stated "C1" (EXPLICIT_REQUIREMENT).
    basis: RequirementBasis = "UNKNOWN"
    # Stage 22 P1: the selected requirement is worded as fluency / native
    # ("verhandlungssicher", "fließend", "Muttersprache") or states a C
    # level -- unlike "sehr gute", which reads as C1 but is only an inference.
    strong: bool = False


_CLAUSE_BREAK = re.compile(r"\.\s|\n|;|•")
# "(mind. B2)", "z. B.": a period after these does not end the statement.
_ABBREVIATIONS = frozenset(
    {"mind", "min", "max", "bzw", "ca", "inkl", "ggf", "evtl", "bspw", "etc", "vgl", "sog", "nr"}
    | {"z", "b", "u", "a", "d", "h", "usw"}
)
_OTHER_LANGUAGE = re.compile(
    r"englisch|english|französisch|franzoesisch|french|spanisch|spanish|italienisch"
    r"|polnisch|russisch|türkisch|tuerkisch|niederländisch|niederlaendisch|chinesisch|arabisch",
    re.IGNORECASE,
)


# Parts of a German requirement scope: split at commas and conjunctions, so
# "B2 zwingend erforderlich und C1 wünschenswert" are two statements.
_PART_BOUNDARY = re.compile(r",|\s(?:und|sowie|oder|bzw\.?)\s", re.IGNORECASE)
# A following part that is nothing but a softener ("..., von Vorteil")
# applies to the part before it; any other content starts a new statement.
_BARE_SOFTENER = re.compile(
    r"^\W*(?:(?:ist|sind|wäre|waere|wären|waeren)\s+)?(?:" + _OPTIONAL.pattern + r")\W*$",
    re.IGNORECASE,
)
# A CEFR level this far after the German mention still belongs to it.
# "Deutschkenntnisse für die ... Kommunikation mit Kunden auf
# mindestens B2-Niveau" states the level well after the mention.
_LEVEL_REACH = 140
# "Führerschein Klasse C1" is a driving licence, not a language level.
_LICENCE = re.compile(r"(?:f(?:ü|ue)hrerschein|fahrerlaubnis|klasse)\W*$", re.IGNORECASE)
# "C1/C2", "B2–C1", "C1, C2 oder Muttersprache": alternatives or a range;
# the lowest one is the minimum asked.
_ALTERNATIVE_JOIN = re.compile(r"\s*(?:[/,–-]|oder|bis)\s*", re.IGNORECASE)
# "Deutsch- und Englischkenntnisse", "Deutsch und Englisch auf B2-Niveau":
# languages joined directly by und/sowie/oder share what follows them (Codex
# round 9); a comma list ("Deutsch, Englisch von Vorteil") stays separate.
_COMPOUND_CONTINUES = re.compile(r"-?\s(?:und|sowie|oder)\s+", re.IGNORECASE)


def _parts(text: str, lo: int, hi: int) -> list[tuple[int, int]]:
    bounds, begin = [], lo
    for boundary in _PART_BOUNDARY.finditer(text, lo, hi):
        bounds.append((begin, boundary.start()))
        begin = boundary.end()
    bounds.append((begin, hi))
    return bounds


def _part_optional(text: str, parts: list[tuple[int, int]], index: int) -> bool:
    begin, end = parts[index]
    if _OPTIONAL.search(text, begin, end):
        return True
    following = parts[index + 1] if index + 1 < len(parts) else None
    return following is not None and bool(_BARE_SOFTENER.match(text[following[0] : following[1]]))


def _part_index(parts: list[tuple[int, int]], offset: int) -> int:
    return max(i for i, (begin, _) in enumerate(parts) if begin <= offset)


def _german_scope(text: str, mention: re.Match[str], clause: tuple[int, int]) -> tuple[int, int]:
    """The span that can state this German mention's level: never across
    another language mention or another German mention (Codex rounds 7-8:
    "Deutsch B2 zwingend erforderlich und Englisch C1 wünschenswert")."""
    lo = max(clause[0], mention.start() - 30)
    for other in _OTHER_LANGUAGE.finditer(text, lo, mention.start()):
        lo = other.end()
    hi = clause[1]
    begin = mention.end()
    compound = _COMPOUND_CONTINUES.match(text, mention.end())
    if compound is not None:
        # The shared ending and anything after it (a softener) belong to
        # German too; a word between them ("idealerweise auch") does not.
        partner = _OTHER_LANGUAGE.match(text, compound.end())
        if partner is not None:
            begin = partner.end()
    for pattern in (_OTHER_LANGUAGE, _GERMAN):
        following = pattern.search(text, begin, hi)
        if following is not None:
            hi = following.start()
    return lo, hi


def _merge_compound(
    text: str, mention: re.Match[str], parts: list[tuple[int, int]], own: int
) -> list[tuple[int, int]]:
    """Join "Deutsch-" with the "und Englischkenntnisse ..." part it shares."""
    if _COMPOUND_CONTINUES.match(text, mention.end()) is None or own + 1 >= len(parts):
        return parts
    nxt = parts[own + 1]
    piece = text[nxt[0] : nxt[1]]
    partner = _OTHER_LANGUAGE.match(text, nxt[0] + len(piece) - len(piece.lstrip()))
    if partner is None:
        return parts
    return [*parts[:own], (parts[own][0], nxt[1]), *parts[own + 2 :]]


def _clause_bounds(text: str, match: re.Match[str]) -> tuple[int, int]:
    start, end = 0, len(text)
    for brk in _CLAUSE_BREAK.finditer(text):
        if brk.group().startswith("."):
            word = re.search(r"(\w+)$", text[max(0, brk.start() - 12) : brk.start()])
            if word is not None and word.group(1).casefold() in _ABBREVIATIONS:
                continue
        if brk.end() <= match.start():
            start = brk.end()
        elif brk.start() >= match.end():
            end = brk.start()
            break
    return start, end


def _nearest_descriptor(
    text: str, mention: re.Match[str], clause_start: int, clause_end: int
) -> tuple[Cefr, bool] | None:
    """The descriptive level word closest to the German mention (before or
    after it, same clause); on equal distance the longer phrase wins, so
    "sehr gute" beats its own "gute"."""
    lo = max(clause_start, mention.start() - 30)
    hi = min(clause_end, mention.end() + 25)
    best: tuple[int, int, Cefr, bool] | None = None
    for index, (pattern, value) in enumerate(_DESCRIPTIVE):
        strong = index < _STRONG_DESCRIPTORS
        for match in pattern.finditer(text, lo, hi):
            if match.end() <= mention.start():
                distance = mention.start() - match.end()
            elif match.start() >= mention.end():
                distance = match.start() - mention.end()
            else:
                distance = 0
            key = (distance, -(match.end() - match.start()), value, strong)
            if best is None or key[:2] < best[:2]:
                best = key
    return (best[2], best[3]) if best else None


def extract_german_requirement(text: str) -> LanguageRequirement:
    """The strongest mandatory German level the posting asks for (an
    optional level only when none is mandatory), or level None. The
    level is read from a narrow window around each German mention, so
    "sehr gute Englisch- und gute Deutschkenntnisse" yields B2, not C1."""
    order = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6}
    best: tuple[int, int, int, Cefr, str] | None = None
    for mention in _GERMAN.finditer(text):
        clause_start, clause_end = _clause_bounds(text, mention)
        lo, hi = _german_scope(text, mention, (clause_start, clause_end))
        parts = _parts(text, lo, hi)
        own = _part_index(parts, mention.start())
        parts = _merge_compound(text, mention, parts, own)
        evidence = " ".join(text[clause_start:clause_end].split())[:160]
        # Every stated CEFR level is judged in its own part (Codex round 8:
        # "Deutsch C1 wünschenswert, B2 zwingend erforderlich" states a
        # mandatory B2). A level before the mention counts only in the
        # mention's own part ("C1 Deutsch", not "Englisch C1, Deutsch").
        levels: list[tuple[Cefr, bool, bool, bool]] = []
        previous: re.Match[str] | None = None
        for cefr in _CEFR.finditer(text, lo, min(hi, mention.end() + _LEVEL_REACH)):
            index = _part_index(parts, cefr.start())
            if cefr.end() <= mention.start() and index != own:
                continue
            if _LICENCE.search(text, max(0, cefr.start() - 25), cefr.start()):
                continue
            level: Cefr = cefr.group(1)  # type: ignore[assignment]
            if previous is not None and _ALTERNATIVE_JOIN.fullmatch(
                text, previous.end(), cefr.start()
            ):
                # Same statement: keep the lower level of the alternatives.
                last_level, last_mandatory, _, _ = levels[-1]
                lower = min(last_level, level, key=order.__getitem__)
                levels[-1] = (lower, last_mandatory, True, lower in ("C1", "C2"))
                previous = cefr
                continue
            levels.append(
                (level, not _part_optional(text, parts, index), True, level in ("C1", "C2"))
            )
            previous = cefr
        if not levels:
            descriptor = _nearest_descriptor(text, mention, clause_start, clause_end)
            if descriptor is not None:
                value, strong = descriptor
                levels.append((value, not _part_optional(text, parts, own), False, strong))
        for level, mandatory, explicit, strong in levels:
            # A mandatory level beats any optional one, then
            # the higher level; an explicit level beats the same inferred.
            candidate = (
                1 if mandatory else 0,
                order[level],
                1 if explicit else 0,
                # Stage 22 P1: on an otherwise equal requirement the
                # fluency wording wins, independent of sentence order.
                1 if strong else 0,
                level,
                evidence,
                strong,
            )
            if best is None or candidate[:4] > best[:4]:
                best = candidate
    if best is None:
        return LanguageRequirement(level=None, mandatory=None)
    mandatory_rank, _, explicit, _, level, evidence, strong = best
    mandatory = bool(mandatory_rank)
    if not mandatory:
        basis: RequirementBasis = "PREFERRED_REQUIREMENT"
    else:
        basis = "EXPLICIT_REQUIREMENT" if explicit else "INFERRED_REQUIREMENT"
    return LanguageRequirement(
        level=level, mandatory=mandatory, evidence=evidence, basis=basis, strong=strong
    )


# --- salary ------------------------------------------------------------------
#
# Night audit 2026-10-27: "IT-Zulage 1.500 € mtl." was read as the base pay
# (18,000 EUR/year), "Weihnachtsgeld 1.000 €" as 12,000 EUR/year and
# "Jahresbonus bis 5.000 €" as 60,000 EUR/year; "x 13 Gehälter" was counted
# x12. Every amount is now read together with the words just before it: an
# allowance/bonus is reported as a component and never becomes the salary,
# an hourly rate stays hourly, and an estimate shown by a job board is
# labelled as such instead of passing for the employer's figure.

_TARIFF = re.compile(r"\bTV-?L\b|\bTVöD\b|\bTVoeD\b|tarifvertrag|tarifliche|\bE\s?\d{1,2}\b")
_AMOUNT = r"(\d{1,3}(?:[.\s]\d{3})+|\d{4,6}|\d{2,3}\s?[kK])"
_SALARY = re.compile(
    _AMOUNT + r"(?:\s*(?:€|EUR|Euro))?(?:\s*(?:-|–|bis)\s*" + _AMOUNT + r")?\s*(?:€|EUR|Euro)",
    re.IGNORECASE,
)
# Stage 19: "zwischen 42.000 und 48.000 €" is a range (both bounds), not
# two amounts of which only the second one was kept.
_SALARY_BETWEEN = re.compile(
    r"zwischen\s+" + _AMOUNT + r"(?:\s*(?:€|EUR|Euro))?\s+und\s+" + _AMOUNT + r"\s*(?:€|EUR|Euro)",
    re.IGNORECASE,
)
_SALARY_PREFIX = re.compile(
    r"(?:€|EUR)\s*" + _AMOUNT + r"(?:\s*(?:-|–|bis)\s*(?:€|EUR)?\s*" + _AMOUNT + r")?",
    re.IGNORECASE,
)
_MONTH = re.compile(r"monat|/\s*m\b|mtl|month", re.IGNORECASE)
_MONTHS_COUNT = re.compile(r"monatsgeh(?:ä|ae)lter|monatsgehalt", re.IGNORECASE)
_YEAR = re.compile(r"jahr|p\.\s?a\.|jährlich|jaehrlich|annual|year", re.IGNORECASE)
_HOURLY_AMOUNT = r"(\d{1,3}(?:,\d{1,2})?)"
_HOURLY = re.compile(
    r"stundenlohn\D{0,20}?"
    + _HOURLY_AMOUNT
    + r"\s*(?:€|EUR|Euro)|"
    + _HOURLY_AMOUNT
    + r"\s*(?:€|EUR|Euro)\s*(?:/|pro|je)\s*(?:std\.?|stunde|h)\b",
    re.IGNORECASE,
)
# Pay components that are NOT the base salary, by the word naming them.
_COMPONENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ALLOWANCE",
        re.compile(
            r"zulage|zuschuss|zuschlag|pauschale|budget|jobticket|gutschein|"
            r"fahrtkosten|erstattung|inflationsausgleich|einmalzahlung|allowance",
            re.IGNORECASE,
        ),
    ),
    (
        "BONUS",
        re.compile(
            r"bonus|prämie|praemie|provision|tantieme|gewinnbeteiligung|"
            r"erfolgsbeteiligung|variable[nr]?\s+(?:anteil|vergütung|gehalt)",
            re.IGNORECASE,
        ),
    ),
    ("CHRISTMAS_BONUS", re.compile(r"weihnachtsgeld", re.IGNORECASE)),
    ("HOLIDAY_PAY", re.compile(r"urlaubsgeld", re.IGNORECASE)),
)
_LEAD_BOUNDARY = re.compile(r"[.;:!?\n•]|,\s|\s[-–]\s|\b(?:und|sowie|plus|zzgl\.?|zuzüglich)\b")
_UP_TO = re.compile(r"(?:bis\s+zu|maximal|max\.|höchstens)\s*$", re.IGNORECASE)
_ESTIMATE = re.compile(
    r"gesch(?:ä|ae)tzt|sch(?:ä|ae)tzung|estimate|prognose|gehaltsspanne\s+laut|"
    r"laut\s+(?:xing|stepstone|indeed|kununu|glassdoor)|gehaltsrechner|gehaltsvergleich",
    re.IGNORECASE,
)
_SALARIES_PER_YEAR = re.compile(
    r"(?:x|×|\*)\s*(1[34])\b|\b(1[34])\s*(?:monats)?geh(?:ä|ae)lter|\b(1[34])\.\s*monatsgehalt",
    re.IGNORECASE,
)
_NEGOTIABLE = re.compile(
    r"gehalt\w*\s+(?:nach|n\.)\s+(?:vereinbarung|absprache)|nach\s+vereinbarung|"
    r"verhandlungssache|verhandelbar|\bVB\b",
    re.IGNORECASE,
)

SalaryProvenance = Literal["EMPLOYER_STATED", "JOB_BOARD_ESTIMATE"]


@dataclass(frozen=True)
class SalaryComponent:
    kind: str  # ALLOWANCE | BONUS | CHRISTMAS_BONUS | HOLIDAY_PAY
    evidence: str


@dataclass(frozen=True)
class SalaryInfo:
    # Base salary per year. `yearly_min` is None for an upper bound only
    # ("bis zu 55.000 €"); both None when no base salary is stated.
    yearly_min: float | None = None
    yearly_max: float | None = None
    # MONTH = the posting gave a monthly figure; yearly values are
    # x `salaries_per_year` (12 unless the posting states 13/14).
    period: SalaryPeriod | None = None
    tariff: bool = False
    evidence: str | None = None
    provenance: SalaryProvenance | None = None
    salaries_per_year: int | None = None
    hourly_min: float | None = None
    hourly_max: float | None = None
    negotiable: bool = False
    components: tuple[SalaryComponent, ...] = ()


def _amount(raw: str) -> float:
    raw = raw.strip()
    if raw[-1] in "kK":
        return float(raw[:-1].strip()) * 1000
    return float(re.sub(r"[.\s]", "", raw))


def _lead(text: str, start: int, width: int = 60) -> str:
    """The words before an amount, back to the nearest clause boundary."""
    window = text[max(0, start - width) : start]
    boundaries = list(_LEAD_BOUNDARY.finditer(window))
    return window[boundaries[-1].end() :] if boundaries else window


def _trail(text: str, end: int) -> str:
    """The noun right after an amount ("1.500 € IT-Zulage"): at most two
    words, never across "+", "(" or a clause boundary, so "€150000 (EUR) +
    20% bonus" keeps the salary."""
    window = re.split(r"[+(]", text[end : end + 40], maxsplit=1)[0]
    boundary = _LEAD_BOUNDARY.search(window)
    window = window[: boundary.start()] if boundary else window
    return " ".join(re.findall(r"[\w-]+", window)[:2])


_SENTENCE_END = re.compile(r"[!?\n]|\.\s")


def _sentence(text: str, match: re.Match[str]) -> str:
    starts = [m.end() for m in _SENTENCE_END.finditer(text, 0, match.start())]
    end = _SENTENCE_END.search(text, match.end())
    return text[(starts[-1] if starts else 0) : end.start() if end else len(text)]


def _component(lead: str, trail: str) -> str | None:
    for kind, pattern in _COMPONENTS:
        if pattern.search(lead) or pattern.search(trail):
            return kind
    return None


def _salary_matches(text: str) -> list[re.Match[str]]:
    found: list[re.Match[str]] = []
    for pattern in (_SALARY_BETWEEN, _SALARY, _SALARY_PREFIX):
        for match in pattern.finditer(text):
            if not any(m.start() < match.end() and match.start() < m.end() for m in found):
                found.append(match)
    return sorted(found, key=lambda m: m.start())


def extract_salary(text: str) -> SalaryInfo:
    components: list[SalaryComponent] = []
    candidates: list[SalaryInfo] = []
    per_year = _SALARIES_PER_YEAR.search(text)
    months = int(next(g for g in per_year.groups() if g)) if per_year else 12
    for match in _salary_matches(text):
        lead = _lead(text, match.start())
        trail = text[match.end() : match.end() + 40]
        kind = _component(lead, _trail(text, match.end()))
        if kind is not None:
            components.append(SalaryComponent(kind, _snippet(text, match)))
            continue
        low = _amount(match.group(1))
        high = _amount(match.group(2)) if match.group(2) else low
        # "59.724 Euro (14 Monatsgehälter)" is a yearly figure: the word
        # "Monatsgehälter" counts salaries, it is not a period, and no
        # monthly salary reaches 15,000.
        monthly_word = _MONTH.search(_MONTHS_COUNT.sub(" ", trail))
        if (monthly_word and high < 15000) or (1000 <= high < 10000 and not _YEAR.search(trail)):
            period: SalaryPeriod = "MONTH"
            factor = months
        elif high >= 20000:
            period, factor = "YEAR", 1
        else:
            continue  # an hourly rate or an unrelated number
        candidates.append(
            SalaryInfo(
                yearly_min=None if _UP_TO.search(lead) else low * factor,
                yearly_max=high * factor,
                period=period,
                evidence=_snippet(text, match),
                provenance=(
                    "JOB_BOARD_ESTIMATE"
                    if _ESTIMATE.search(_sentence(text, match))
                    else "EMPLOYER_STATED"
                ),
                salaries_per_year=factor if period == "MONTH" else None,
            )
        )
    hourly = _HOURLY.search(text)
    hourly_value = float((hourly.group(1) or hourly.group(2)).replace(",", ".")) if hourly else None
    extras = dict(
        hourly_min=hourly_value,
        hourly_max=hourly_value,
        negotiable=bool(_NEGOTIABLE.search(text)),
        components=tuple(components),
    )
    # The employer's own figure wins over a job board's estimate.
    chosen = next((c for c in candidates if c.provenance == "EMPLOYER_STATED"), None) or (
        candidates[0] if candidates else None
    )
    if chosen is not None:
        return SalaryInfo(**{**chosen.__dict__, **extras})
    tariff = _TARIFF.search(text)
    if tariff:
        return SalaryInfo(tariff=True, evidence=_snippet(text, tariff), **extras)
    if hourly:
        return SalaryInfo(evidence=_snippet(text, hourly), **extras)
    return SalaryInfo(**extras)


# --- entry level ---------------------------------------------------------------

_ENTRY_POSITIVE = re.compile(
    r"\bjunior|\btrainee|berufseinst|einsteiger|quereinst|absolvent|graduate|entry[\s-]?level"
    r"|erste\s+(?:berufs)?erfahrung|keine\s+berufserfahrung",
    re.IGNORECASE,
)
_ENTRY_NEGATIVE = re.compile(
    r"\bsenior|\blead\b|\bprincipal|head\s+of|\bleiter|teamleit|architekt|architect"
    r"|mehrjährig|mehrjaehrig|langjährig|langjaehrig|umfangreiche\s+(?:berufs)?erfahrung",
    re.IGNORECASE,
)
# Number words as postings write them ("Mindestens zwei bis drei Jahre
# Berufserfahrung"). Digits alone left such a stated requirement UNKNOWN.
_NUMBER_WORDS = {
    "ein": 1,
    "eine": 1,
    "zwei": 2,
    "drei": 3,
    "vier": 4,
    "fünf": 5,
    "fuenf": 5,
    "sechs": 6,
    "sieben": 7,
    "acht": 8,
    "neun": 9,
    "zehn": 10,
}
_WORDS = "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))
_NUMBER = rf"((?<!\d)\d{{1,2}}(?!\d)|(?<![a-zäöüß])(?:{_WORDS})(?![a-zäöüß]))"
# The lower bound of a range is the requirement ("2-3" and "zwei bis drei" -> 2).
_RANGE_TAIL = rf"(?:-|–|bis(?:\s+zu)?)\s*(?:\d{{1,2}}|(?:{_WORDS})(?![a-zäöüß]))"
# "seit vier Jahren bestehendes Unternehmen mit Erfahrung" is company age,
# not a requirement ("seit"/"vor" directly before the number).
_NOT_AGE = r"(?<!seit\s)(?<!vor\s)"
_YEARS = re.compile(
    rf"{_NOT_AGE}{_NUMBER}\s*\+?\s*(?:{_RANGE_TAIL}\s*)?(?:jahre|years)[^.;\n]{{0,40}}?(?:erfahrung|experience)"
    rf"|(?:erfahrung|experience)[^.;\n]{{0,30}}?{_NOT_AGE}{_NUMBER}\s*\+?\s*(?:{_RANGE_TAIL}\s*)?(?:jahre|years)",
    re.IGNORECASE,
)


def _years_value(token: str) -> int:
    return int(token) if token.isdigit() else _NUMBER_WORDS[token.casefold()]


@dataclass(frozen=True)
class EntryLevelSignals:
    positive: list[str] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)
    required_years: int | None = None


def entry_level_signals(title: str, text: str) -> EntryLevelSignals:
    combined = f"{title}\n{text}"
    positive = sorted({m.group(0).lower() for m in _ENTRY_POSITIVE.finditer(combined)})
    negative = sorted({m.group(0).lower() for m in _ENTRY_NEGATIVE.finditer(combined)})
    years = [_years_value(m.group(1) or m.group(2)) for m in _YEARS.finditer(combined)]
    return EntryLevelSignals(positive, negative, max(years) if years else None)


# --- start date --------------------------------------------------------------

_IMMEDIATE = re.compile(r"ab\s+sofort|zum\s+nächstmöglichen|zum\s+naechstmoeglichen|asap", re.I)


def asks_immediate_start(text: str) -> bool:
    return bool(_IMMEDIATE.search(text))


# --- working time ---------------------------------------------------------

WorkTimeMode = Literal["FULL_TIME", "PART_TIME", "UNKNOWN"]
_COMBINED_DE_AFTER = (
    r"(?=\s*(?:$|[.,;:!?)(/\n])"
    r"|\s+(?:zu|zum|zur|ab|in|bei|f(?:ü|ue)r|mit|im|am|als|oder|und|m(?:ö|oe)glich|stellen?"
    r"|besch(?:ä|ae)ftigung|arbeit|arbeiten|angebote?|jobs?|positionen?|verf(?:ü|ue)gbar)\b)"
)
_COMBINED_EN_AFTER = (
    r"(?=\s*(?:$|[.,;:!?)(/\n])"
    r"|\s+(?:work|working|positions?|roles?|jobs?|employment|basis|schedule|hours|possible"
    r"|available|opportunit\w*|contract|options?|arrangements?|to|for|with|from|in|at|is|are|as"
    r"|on)\b)"
)
# "50 Vollzeitmitarbeitern" in a company blurb is a headcount, not this vacancy's hours.
_FULL_TIME = re.compile(
    r"\bvollzeit(?!mitarbeit|kr(?:ä|ae)ft|kraft|besch(?:ä|ae)ftigte|angestellte|nah)\w*"
    r"|\bfull[\s-]?time(?!\s+(?:employees?|staff|team|colleagues))\b"
    # "Voll- oder Teilzeit", "Voll-/Teilzeit", "full- or part-time": full time is offered
    # -- only when what follows is about the JOB or ends the phrase (allow-list: a person noun such
    #    as "engineers" or "Mitarbeiter" describes staff, not this vacancy, so it must not count)
    r"|\bvoll-\s*(?:/|oder|und|&|bzw\.?)\s*teilzeit\b" + _COMBINED_DE_AFTER + r""
    r"|\bfull[\s-]*(?:/|or|and|&)\s*part[\s-]?time\b" + _COMBINED_EN_AFTER + r"",
    re.IGNORECASE,
)
# A labelled field is the vacancy's own hours even next to benefit vocabulary:
# "Arbeitszeit: Teilzeit", "Umfang: Teilzeit 50 %", "Vertragsart: Teilzeit".
_PART_TIME_LABELLED = re.compile(
    r"\b(?:arbeitszeit|vertragsart|umfang|stellenumfang|pensum|besch(?:ä|ae)ftigung(?:sart|sumfang)?"
    r"|working\s+hours|employment\s+type)\W{0,4}teilzeit\b",
    re.IGNORECASE,
)
_MAX_NEGATION_CHECKS = 25
_REDUCED_FULL_TIME = re.compile(r"\bvollzeitnah", re.IGNORECASE)
_PART_TIME = re.compile(r"\bteilzeit\w{0,30}|\bpart[\s-]?time\b", re.IGNORECASE)
# A staff statement in the same sentence ("Our employees work full- and part-time") is not an
# offer of full time for THIS vacancy.
_STAFF_SUBJECT = re.compile(
    r"\b(?:employees?|staff|workforce|colleagues|engineers?|developers?|people|team"
    r"|mitarbeiter\w*|belegschaft|kolleg\w+|besch(?:ä|ae)ftigte\w*)\b"
    # "Wir arbeiten in Voll- und Teilzeit", "We work full- and part-time": how the company works
    r"|\b(?:wir|sie|we|they)\s+(?:\w+\s+)?(?:arbeiten|work|working)\b",
    re.IGNORECASE,
)
_COMBINED_FULL_PART = re.compile(r"\bvoll-|\bfull[\s-]*(?:/|or|and|&)", re.IGNORECASE)
# "Teilzeitmöglichkeiten", "Teilzeitmodell", "Teilzeitoption": a benefit the employer lists, not the
# hours of this job.
_PART_TIME_OFFER_WORD = re.compile(
    r"teilzeit(?:m(?:ö|oe)glichkeit|modell|option|variante|angebot|regelung|geeignet)\w*",
    re.IGNORECASE,
)
# Right after the word, inside the same clause: an explicit POSSIBILITY word. Schedule wording
# ("nach Vereinbarung", "bei Bedarf", "auf Wunsch") describes the hours, so it is not an offer;
# "Teilzeit nach Absprache möglich" is still an offer because of "möglich".
_PART_TIME_OFFER_AFTER = re.compile(
    r"^[\s-]*(?:(?!with\b|mit\b|and\b|und\b|sowie\b|but\b|aber\b)\w+\s+){0,2}?"
    r"(?:m(?:ö|oe)glich|denkbar|available|possible|options?|arrangements?|can\s+be\s+discussed"
    r"|geeignet|nichts\s+im\s+wege|steht\s+(?:\w+\s+){0,2}?nichts|eligible)\b",
    re.IGNORECASE,
)
_PART_TIME_JOB_NOUN = re.compile(
    r"^[\s-]*(?:\w+\s+){0,2}?(?:position|job|role|stelle|contract|vertrag|arbeitsplatz)\b",
    re.IGNORECASE,
)
_PART_TIME_OFFER_BEFORE = re.compile(
    r"(?:\bauch|\bggf\.?|\bgegebenenfalls|\boptional|\balternativ|\bon\s+request"
    r")\s+(?:in\s+)?$",
    re.IGNORECASE,
)
# "Möglichkeit zur Teilzeitbeschäftigung", "die Option auf Teilzeit"
_PART_TIME_OFFER_LEAD = re.compile(
    r"\b(?:m(?:ö|oe)glichkeit(?:en)?|options?|chance|possibilit(?:y|ies))\s+(?:zur?|auf|der|einer|von|f(?:ü|ue)r|of|for)\s+(?:\w+\s+){0,4}$"
    r"|\b(?:also\s+)?available\s+$|\bk(?:ö|oe)nnen\s+(?:sie|du|ihr)\s+(?:\w+\s+){0,3}in\s+$"
    r"|\bkann\s+(?:\w+\s+){0,3}(?:mit|in)\s+$",
    re.IGNORECASE,
)
_ONLY_PART_TIME = re.compile(
    r"\b(?:nur|ausschlie(?:ß|ss)lich|lediglich)\s+(?:in\s+)?teilzeit"
    r"|\bpart[\s-]?time\s+only\b|\bonly\s+part[\s-]?time\b",
    re.IGNORECASE,
)


_PART_TIME_JOB_WORD = re.compile(r"teilzeit(?:stelle|arbeitsplatz|job|position)\w*", re.IGNORECASE)
_OWN_HOURS = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:stunden|std|hours?|hrs|h\b|wochenstunden)"
    r"|\bteilzeit\s*\(?\s*\d+\s*%",
    re.IGNORECASE,
)
_SENTENCE_BREAK = re.compile(r"[.;\n]")
_HOURS_CLAUSE_BREAK = re.compile(r"[.;,\n]")


def _own_hours_nearby(text: str, match: re.Match[str]) -> bool:
    """Explicit hours in the same CLAUSE as the word: "(20 hours per week)", "(75 %)". A comma ends
    the clause, so the hours of a preceding "Vollzeit (40 h)," are not those of this mention."""
    lo, hi = max(0, match.start() - 60), match.end() + 60
    window = text[lo:hi]
    rel = match.start() - lo
    rel_end = match.end() - lo
    starts = [m.end() for m in _HOURS_CLAUSE_BREAK.finditer(window, 0, rel)]
    ends = _HOURS_CLAUSE_BREAK.search(window, rel_end)
    sentence = window[(starts[-1] if starts else 0) : ends.start() if ends else len(window)]
    return bool(_OWN_HOURS.search(sentence))


def _part_time_is_an_offer(text: str, match: re.Match[str]) -> bool:
    """True when the part-time word describes something the employer MAY allow ("Teilzeit nach
    Absprache möglich", "part-time options") rather than the hours of this vacancy."""
    if _PART_TIME_LABELLED.search(text[max(0, match.start() - 30) : match.end()]):
        return False
    if _PRESENT_BEFORE.search(text[max(0, match.start() - 25) : match.start()]):
        return False  # "aktuell Teilzeit möglich": today's terms, whatever follows
    if _PART_TIME_JOB_WORD.fullmatch(match.group(0)):
        return False  # "Teilzeitstelle", "Teilzeitarbeitsplatz": the job itself is part time
    if _own_hours_nearby(text, match):
        return False  # stated hours of THIS vacancy beat any option wording around them
    if _PART_TIME_OFFER_WORD.fullmatch(match.group(0)):
        return True
    if _PART_TIME_OFFER_LEAD.search(text[max(0, match.start() - 40) : match.start()]):
        return True
    after = re.split(r"[,;.\n]", text[match.end() : match.end() + 60], maxsplit=1)[0]
    if _PART_TIME_JOB_NOUN.match(after):
        return False  # "part-time position with pension options": the position IS part time
    if _PART_TIME_OFFER_AFTER.match(after):
        return True
    return bool(_PART_TIME_OFFER_BEFORE.search(text[max(0, match.start() - 25) : match.start()]))


@dataclass(frozen=True)
class WorkTimeSignal:
    mode: WorkTimeMode
    evidence: str | None = None


def _sentence_has_staff_subject(text: str, match: re.Match[str]) -> bool:
    lo, hi = max(0, match.start() - 100), match.end() + 60
    window = text[lo:hi]
    rel, rel_end = match.start() - lo, match.end() - lo
    starts = [m.end() for m in _SENTENCE_BREAK.finditer(window, 0, rel)]
    stop = _SENTENCE_BREAK.search(window, rel_end)
    sentence = window[(starts[-1] if starts else 0) : stop.start() if stop else len(window)]
    return bool(_STAFF_SUBJECT.search(sentence))


def _is_staff_statement(text: str, match: re.Match[str]) -> bool:
    """A combined "Voll-/Teilzeit" match whose own sentence has a staff subject."""
    return bool(_COMBINED_FULL_PART.match(match.group(0))) and _sentence_has_staff_subject(
        text, match
    )


_COMBINED_TAIL = re.compile(
    r"^[\s-]*(?:/|oder|und|&|bzw\.?|or|and)\s*(?:teilzeit|part[\s-]?time)", re.IGNORECASE
)


_COMBINED_LEAD = re.compile(
    r"(?:vollzeit\w*|voll-|full[\s-]?time|full[\s-]*)[\s-]*(?:\([^)]{0,20}\)\s*)?"
    r"(?:/|oder|und|&|bzw\.?|or|and|sowie|als\s+auch)\s*(?:in\s+|vollzeitnaher\s+)?$",
    re.IGNORECASE,
)


def _combined_span(text: str, full: re.Match[str]) -> tuple[int, int]:
    """The full-time phrase with its "oder Teilzeit" / "-/Teilzeit" tail, if it has one."""
    if _COMBINED_FULL_PART.match(full.group(0)):
        return full.start(), full.end()
    tail = _COMBINED_TAIL.match(text[full.end() : full.end() + 30])
    return (full.start(), full.end() + tail.end()) if tail else (full.start(), full.end())


_THIS_POSITION = re.compile(
    r"\b(?:diese|die|this|the|unsere|our)\s+(?:stelle|position|role|vakanz)\b[^.;\n]{0,40}$",
    re.IGNORECASE,
)


# "Vollzeit ab 2027, aktuell Teilzeit": today's terms are part time.
_PRESENT_BEFORE = re.compile(
    r"\b(?:aktuell|derzeit|momentan|zun(?:ä|ae)chst|anfangs|vorerst|currently|initially"
    r"|at\s+first|for\s+now)\W{0,3}$",
    re.IGNORECASE,
)


def _vacancy_defining(text: str, match: re.Match[str]) -> bool:
    """A part-time mention that says what THIS vacancy is: a labelled field, "Teilzeitstelle",
    stated hours, or "This position is part-time". A list item ("Vollzeit, Teilzeit und mobiles
    Arbeiten") is not."""
    before = text[max(0, match.start() - 60) : match.start()]
    return bool(
        _PART_TIME_LABELLED.search(text[max(0, match.start() - 30) : match.end()])
        or _PART_TIME_JOB_WORD.fullmatch(match.group(0))
        or _own_hours_nearby(text, match)
        or _THIS_POSITION.search(before)
        or _PRESENT_BEFORE.search(before)
    )


def _own_part_time_elsewhere(text: str, span: tuple[int, int], *, combined: bool) -> bool:
    """Another, non-offer, non-negated part-time mention that is not part of a "Vollzeit oder
    Teilzeit" phrase: the vacancy's own statement, which a general full-time statement never
    overrides."""
    checked = 0
    for match in _PART_TIME.finditer(text):
        if match.start() < span[1] and match.end() > span[0]:
            continue  # part of the phrase itself
        if _COMBINED_LEAD.search(text[max(0, match.start() - 50) : match.start()]):
            continue  # the "Teilzeit" of another "Vollzeit oder Teilzeit"
        if _part_time_is_an_offer(text, match):
            continue
        if not combined and not _vacancy_defining(text, match):
            continue  # a plain "Vollzeit" yields only to the vacancy's own statement
        checked += 1
        if checked > _MAX_NEGATION_CHECKS or not _negated(text, match):
            return True
    return False


# A full-time mention that is a FUTURE possibility, not the current terms of the vacancy.
# Structural, not a phrase list: the CLAUSE (bounded by , . ; newline) around the mention is
# deferred when it holds a forward-looking marker, a future year, or option/transition wording.
# Only forward-looking markers: "aktuell/zunächst/currently" describe the present and do not defer.
_FUTURE_MARKER = re.compile(
    r"\bsp(?:ä|ae)ter\w*|\bdanach\b|\bperspektivisch\w*|\beventuell\b|\beventually\b|\blater\b"
    r"|\bin\s+(?:the\s+)?future\b|\berst\b|\bonly\s+after\b|\bonce\b"
    r"|\bnach\s+(?:\w+\s+){0,3}?(?:einarbeitung\w*|probe\w*)"
    r"|\bafter\s+(?:\w+\s+){0,3}?(?:onboarding|probation\w*|training)"
    r"|\bab\s+(?:[\w.]+\s+){0,2}?20(?:2[7-9]|[3-9]\d)\b|\bfrom\s+(?:\w+\s+){0,2}?20(?:2[7-9]|[3-9]\d)\b",
    re.IGNORECASE,
)
# "Option auf Vollzeit", "Übergang in eine unbefristete Vollzeitstelle", "Aufstockung auf Vollzeit"
_FUTURE_LEAD = re.compile(
    r"\b(?:option|m(?:ö|oe)glichkeit|chance|opportunit(?:y|ies)|(?:ü|ue)bergang"
    r"|wechsel|aufstockung|transition|move|conversion)\w*\b",
    re.IGNORECASE,
)
_CLAUSE_END = re.compile(r"[,.;\n]")
_FULL_TIME_OPTION_WORD = re.compile(
    r"\b(?:vollzeit|full[\s-]?time)\W{0,3}(?:option|opportunit\w*|m(?:ö|oe)glichkeit\w*)",
    re.IGNORECASE,
)
_CLAUSE_WINDOW = 120


def _full_time_deferred(text: str, match: re.Match[str], *, soft: bool = False) -> bool:
    """True when the clause of this full-time mention defers it (see above). HARD markers
    (later, after the probation, from 2027) discard the mention. SOFT wording (option, transition,
    "Teilzeit, Vollzeit ...") is ambiguous -- "Option auf 4 oder 5 Tage Vollzeit" offers full time
    today -- so it counts only with `soft=True`, i.e. next to a current part-time statement.
    Only a CURRENT full-time statement may open the automatic gate: a future possibility is not
    evidence of today's terms. Bounded: only the clause, `_CLAUSE_WINDOW` characters a side."""
    before = re.split(_CLAUSE_END, text[max(0, match.start() - _CLAUSE_WINDOW) : match.start()])[-1]
    after = re.split(_CLAUSE_END, text[match.end() : match.end() + _CLAUSE_WINDOW], maxsplit=1)[0]
    clause = before + match.group(0) + after
    if _FUTURE_MARKER.search(clause):
        return True
    return soft and bool(
        _FUTURE_LEAD.search(before)
        or _FULL_TIME_OPTION_WORD.match(text[match.start() : match.start() + 40])
        or _staged_part_time_first(text, match)
    )


def _staged_part_time_first(text: str, match: re.Match[str]) -> bool:
    """ "Teilzeit, Vollzeit ab ...": a plain part-time word, then the full-time word as a separate
    comma clause. Not "Teilzeit oder Vollzeit" (an offer of both) and not a list the other way
    round. The vacancy is held for a human instead of being confirmed as full time."""
    before = text[max(0, match.start() - 40) : match.start()]
    m = re.search(r"(?:teilzeit\w*|part[\s-]?time)\s*[,;:]\s*(?:(?:\w+\s+){0,2})?$", before, re.I)
    return bool(m)


_MAX_PART_TIME_SCAN = 60


def _deferred_full_time_with_current_part_time(text: str) -> bool:
    """A future full-time remark next to a current part-time statement of the vacancy: the
    part-time statement wins, whichever other full-time words (staff, general) the text has.
    An exhausted scan budget answers True (held for a human), never False."""
    deferred = False
    for checked, match in enumerate(_FULL_TIME.finditer(text)):
        if checked >= _MAX_NEGATION_CHECKS:
            return True
        if _full_time_deferred(text, match, soft=True):
            deferred = True
            break
    if not deferred:
        return False
    for checked, match in enumerate(_PART_TIME.finditer(text)):
        if checked >= _MAX_PART_TIME_SCAN:
            return True
        if _COMBINED_LEAD.search(text[max(0, match.start() - 50) : match.start()]):
            continue  # the "Teilzeit" of a "Vollzeit oder Teilzeit" phrase
        if not _part_time_is_an_offer(text, match) and not _negated(text, match):
            return True
    return False


def _affirmed_bounded(pattern: re.Pattern[str], text: str) -> tuple[re.Match[str] | None, bool]:
    """(first non-negated match, budget exhausted). Hostile pages that repeat a negated phrase
    thousands of times must not cost quadratic time: after `_MAX_NEGATION_CHECKS` the caller is
    told the budget ran out."""
    for checked, match in enumerate(pattern.finditer(text)):
        if checked >= _MAX_NEGATION_CHECKS:
            return None, True
        if not _negated(text, match):
            return match, False
    return None, False


def _current_full_time(text: str) -> tuple[re.Match[str] | None, bool]:
    """First non-negated, non-deferred full-time match (and whether the budget ran out)."""
    for checked, match in enumerate(_FULL_TIME.finditer(text)):
        if checked >= _MAX_NEGATION_CHECKS:
            return None, True
        if not _negated(text, match) and not _full_time_deferred(text, match):
            return match, False
    return None, False


def classify_work_time(text: str) -> WorkTimeSignal:
    """What the posting states about working hours. "Vollzeit oder Teilzeit" counts as
    full time (it is offered); only a part-time posting is PART_TIME. A listed option
    ("Teilzeitmöglichkeiten") and a missing statement are UNKNOWN. Unsafe direction first: when the
    work is too large to decide (hostile repetition) the answer is the one that keeps a human in
    the loop (PART_TIME), never FULL_TIME or UNKNOWN."""
    full, over_full = _current_full_time(text)
    if full and _deferred_full_time_with_current_part_time(text):
        return WorkTimeSignal("PART_TIME", _snippet(text, full))
    if full and _is_staff_statement(text, full):
        full = None  # "Our employees work full- and part-time": about the staff, not the job
    span = _combined_span(text, full) if full else None
    combined = bool(full) and (
        span[1] > full.end() or bool(_COMBINED_FULL_PART.match(full.group(0)))
    )
    if full and _own_part_time_elsewhere(text, span, combined=combined):
        # "We offer full- and part-time work. This position is part-time.": the general
        # statement never overrides the vacancy's own part-time statement
        return WorkTimeSignal("PART_TIME", _snippet(text, full))
    if full:
        # "Nur Teilzeit. Vollzeit eventuell spaeter moeglich.": an explicit part-time-only
        # statement of the vacancy is never overridden by a general or future full-time remark
        only_pt, _ = _affirmed_bounded(_ONLY_PART_TIME, text)
        if only_pt:
            return WorkTimeSignal("PART_TIME", _snippet(text, only_pt))
    if full:
        return WorkTimeSignal("FULL_TIME", _snippet(text, full))
    if over_full:
        return WorkTimeSignal("PART_TIME", None)
    only, over = _affirmed_bounded(_ONLY_PART_TIME, text)
    if only:
        return WorkTimeSignal("PART_TIME", _snippet(text, only))
    if over:
        return WorkTimeSignal("PART_TIME", None)
    checked = 0
    for match in _PART_TIME.finditer(text):
        if _part_time_is_an_offer(text, match):
            continue
        checked += 1
        if checked > _MAX_NEGATION_CHECKS:
            return WorkTimeSignal("PART_TIME", _snippet(text, match))
        if not _negated(text, match):
            return WorkTimeSignal("PART_TIME", _snippet(text, match))
    return WorkTimeSignal("UNKNOWN")


def full_time_denied(text: str) -> bool:
    """True when the posting explicitly denies full time ("Keine Vollzeit moeglich"). The
    classifier answers UNKNOWN for it; the source's own full-time flag must not override it."""
    if _REDUCED_FULL_TIME.search(text):
        return True  # "Vollzeitnah": close to, not, full time
    for checked, match in enumerate(_FULL_TIME.finditer(text)):
        if checked >= _MAX_NEGATION_CHECKS:
            return False
        if _negated(text, match):
            return True
    return False
