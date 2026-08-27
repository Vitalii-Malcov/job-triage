import re
from dataclasses import dataclass

from app.models.job import SkillSource


@dataclass(frozen=True)
class SkillExtraction:
    must_have_skills: list[str]
    nice_to_have_skills: list[str]
    skill_source: SkillSource = "description_extracted"


# High-signal backend/IT technologies: the core starts with names observed in
# live Bundesagentur descriptions (Python, Flask, Django, SQL, PostgreSQL,
# MySQL, Git, Linux) and adds common adjacent languages, frameworks, storage,
# delivery, cloud and messaging tools. Patterns require literal mentions; no
# title/industry-based inference or related-technology expansion is performed.
_TECHNOLOGY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("python", r"(?<!\w)python(?!\w)"),
        ("java", r"(?<!\w)java(?!\w)"),
        ("javascript", r"(?<!\w)javascript(?!\w)"),
        ("typescript", r"(?<!\w)typescript(?!\w)"),
        ("c#", r"(?<!\w)c#(?!\w)"),
        ("c++", r"(?<!\w)c\+\+(?!\w)"),
        (".net", r"(?<!\w)\.net(?!\w)"),
        ("php", r"(?<!\w)php(?!\w)"),
        ("ruby", r"(?<!\w)ruby(?!\w)"),
        ("kotlin", r"(?<!\w)kotlin(?!\w)"),
        ("scala", r"(?<!\w)scala(?!\w)"),
        ("flask", r"(?<!\w)flask(?!\w)"),
        ("django", r"(?<!\w)django(?!\w)"),
        ("fastapi", r"(?<!\w)fast[ -]?api(?!\w)"),
        ("spring", r"(?<!\w)spring(?:\s+(?:boot|framework))(?!\w)"),
        ("node.js", r"(?<!\w)node(?:\.js|js)(?!\w)"),
        ("react", r"(?<!\w)react(?:\.js|js)?(?!\w)"),
        ("angular", r"(?<!\w)angular(?:\.js|js)?(?!\w)"),
        ("vue.js", r"(?<!\w)vue(?:\.js|js)(?!\w)"),
        ("sql", r"(?<!\w)sql(?!\w)"),
        ("postgresql", r"(?<!\w)(?:postgresql|postgres)(?!\w)"),
        ("mysql", r"(?<!\w)mysql(?!\w)"),
        ("mariadb", r"(?<!\w)mariadb(?!\w)"),
        ("mongodb", r"(?<!\w)(?:mongodb|mongo\s+db)(?!\w)"),
        ("redis", r"(?<!\w)redis(?!\w)"),
        ("elasticsearch", r"(?<!\w)elasticsearch(?!\w)"),
        ("solr", r"(?<!\w)solr(?!\w)"),
        ("sqlalchemy", r"(?<!\w)sqlalchemy(?!\w)"),
        ("git", r"(?<!\w)git(?!\w)"),
        ("svn", r"(?<!\w)svn(?!\w)"),
        ("docker", r"(?<!\w)docker(?!\w)"),
        ("kubernetes", r"(?<!\w)(?:kubernetes|k8s)(?!\w)"),
        ("linux", r"(?<!\w)linux(?!\w)"),
        ("unix", r"(?<!\w)unix(?!\w)"),
        ("rest", r"(?<!\w)rest(?:ful)?(?!\w)"),
        ("graphql", r"(?<!\w)graphql(?!\w)"),
        ("openapi", r"(?<!\w)openapi(?!\w)"),
        ("swagger", r"(?<!\w)swagger(?!\w)"),
        ("oauth", r"(?<!\w)oauth(?:2(?:\.0)?| 2)?(?!\w)"),
        ("aws", r"(?<!\w)aws(?!\w)"),
        ("azure", r"(?<!\w)(?:microsoft\s+)?azure(?!\w)"),
        ("google cloud", r"(?<!\w)(?:google\s+cloud|gcp)(?!\w)"),
        ("terraform", r"(?<!\w)terraform(?!\w)"),
        ("ansible", r"(?<!\w)ansible(?!\w)"),
        ("jenkins", r"(?<!\w)jenkins(?!\w)"),
        ("github actions", r"(?<!\w)github\s+actions(?!\w)"),
        ("kafka", r"(?<!\w)(?:apache\s+)?kafka(?!\w)"),
        ("rabbitmq", r"(?<!\w)rabbitmq(?!\w)"),
        ("pytest", r"(?<!\w)pytest(?!\w)"),
    )
)

KNOWN_TECHNOLOGIES: tuple[str, ...] = tuple(name for name, _ in _TECHNOLOGY_PATTERNS)

_NICE_TO_HAVE_MARKERS = re.compile(
    r"\b(?:nice[ -]to[ -]have|good[ -]to[ -]have|preferred|desirable|optional|bonus|"
    r"wünschenswert|von vorteil|idealerweise|nicht erforderlich|not required)\b",
    re.IGNORECASE,
)
_MUST_HAVE_MARKERS = re.compile(
    r"\b(?:"
    r"(?<!nicht\s)erforderlich|vorausgesetzt|voraussetzung(?:en)?|"
    r"(?:fach)?kenntnisse\s*(?::|(?:in|mit)\b)|"
    r"erfahrungen\s+mit|berufserfahrung\s+als|"
    r"erfahrung\s+(?:mit|in|im\s+umgang\s+mit)|"
    r"mit\b.{0,160}\bvertraut|strong\s+understanding\s+of|"
    r"bringst\b.{0,100}\bmit|beherrsch(?:st|en)|(?:sicherer|versierter)\s+umgang\s+mit|"
    r"must|(?<!not\s)required|requirements?|experience\s+(?:with|in)|"
    r"proficiency\s+(?:in|with)|knowledge\s+of|skilled\s+in"
    r")\b",
    re.IGNORECASE,
)
_SEGMENT_BOUNDARY = re.compile(r"(?:\r?\n)+|(?<=[.!?;])\s+")
_POSTFIX_NICE_MARKERS = re.compile(
    r"\b(?:(?:ist|sind|wäre|wären)\s+)?(?:von vorteil|wünschenswert|preferred|desirable)\b",
    re.IGNORECASE,
)
_EXPLICIT_REQUIRED_MARKERS = re.compile(
    r"\b(?:(?<!nicht\s)erforderlich|vorausgesetzt|must|(?<!not\s)required)\b",
    re.IGNORECASE,
)
_CONTRAST_MARKERS = re.compile(r"\b(?:aber|hingegen|jedoch|während|but|while|whereas)\b", re.I)
_BULLET_PREFIX = re.compile(r"^\s*(?:[-*•]+|>>+)\s*")
_MUST_SECTION_HEADERS = {
    "das bringst du mit",
    "das bringen sie mit",
    "dein profil",
    "ihr profil",
    "your profile",
    "qualifications",
    "qualifikationen",
    "was sollst du mitbringen",
}
_NICE_SECTION_HEADERS = {"ideal skills", "nice to have", "wünschenswert"}
_OTHER_SECTION_HEADERS = {
    "aufgaben",
    "deine aufgaben",
    "ihre aufgaben",
    "unsere aufgaben",
    "responsibilities",
    "your responsibilities",
    "wir bieten",
    "unser angebot",
    "what we offer",
    "benefits",
    "über uns",
    "about us",
}


def _distance(left: re.Match[str], right: re.Match[str]) -> int:
    if left.end() <= right.start():
        return right.start() - left.end()
    if right.end() <= left.start():
        return left.start() - right.end()
    return 0


def _classify_mention(
    mention: re.Match[str],
    must_markers: list[re.Match[str]],
    nice_markers: list[re.Match[str]],
    inherited_context: str | None,
    segment: str,
    postfix_nice_scope: bool,
) -> str | None:
    if postfix_nice_scope:
        return "nice"
    if inherited_context == "nice" and not _EXPLICIT_REQUIRED_MARKERS.search(segment):
        return "nice"
    if must_markers and not nice_markers:
        return "must"
    if nice_markers and not must_markers:
        return "nice"
    if not must_markers and not nice_markers:
        return inherited_context

    nearest_must = min(_distance(mention, marker) for marker in must_markers)
    nearest_nice = min(_distance(mention, marker) for marker in nice_markers)
    return "must" if nearest_must <= nearest_nice else "nice"


def _normalized_header(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip().rstrip(":?!").casefold())


def _section_header(line: str) -> tuple[str | None, str] | None:
    prefix, separator, remainder = line.partition(":")
    candidate = _normalized_header(prefix if separator else line)
    if candidate in _MUST_SECTION_HEADERS:
        return "must", remainder.strip()
    if candidate in _NICE_SECTION_HEADERS:
        return "nice", remainder.strip()
    return None


def _is_known_other_section_header(line: str) -> bool:
    return _normalized_header(line) in _OTHER_SECTION_HEADERS


def _is_generic_section_header(line: str, had_bullet: bool) -> bool:
    return not had_bullet and line.rstrip().endswith(":") and len(line) <= 80


def _continues_previous_segment(previous: str, current: str) -> bool:
    previous_normalized = previous.rstrip().casefold()
    current_normalized = current.lstrip().casefold()
    continuation_endings = (",", " and", " or", " und", " oder", " sowie")
    continuation_starts = ("and ", "or ", "und ", "oder ", "sowie ")
    has_requirement_context = bool(
        _MUST_HAVE_MARKERS.search(previous) or _NICE_TO_HAVE_MARKERS.search(previous)
    )
    return has_requirement_context and (
        previous_normalized.endswith(continuation_endings)
        or current_normalized.startswith(continuation_starts)
        or (current[:1].islower() and not previous.rstrip().endswith((".", "!", "?", ";")))
    )


def _contextual_segments(description: str) -> list[tuple[str, str | None]]:
    lines: list[tuple[str, str | None]] = []
    section_context: str | None = None

    for raw_line in description.splitlines():
        had_bullet = bool(_BULLET_PREFIX.match(raw_line))
        line = _BULLET_PREFIX.sub("", raw_line).strip()
        if not line:
            continue

        header = _section_header(line)
        if header is not None:
            section_context, remainder = header
            if not remainder:
                continue
            line = remainder
        elif re.match(r"^fachkenntnisse\s*:", line, re.IGNORECASE):
            section_context = "must"
        elif _is_known_other_section_header(line):
            section_context = None
            continue
        elif _is_generic_section_header(line, had_bullet):
            # An unknown short heading may be a nested label such as
            # "Technologien:" inside "Dein Profil". It is structural text,
            # not a context boundary: skip it while preserving any active
            # must/nice section. With no parent section, None stays None.
            continue

        if (
            lines
            and lines[-1][1] == section_context
            and _continues_previous_segment(lines[-1][0], line)
        ):
            previous, context = lines[-1]
            lines[-1] = (f"{previous} {line}", context)
        else:
            lines.append((line, section_context))

    return [
        (segment, context)
        for line, context in lines
        for segment in _SEGMENT_BOUNDARY.split(line)
        if segment.strip()
    ]


def _postfix_nice_scopes_all_mentions(
    segment: str,
    mentions: list[re.Match[str]],
    must_markers: list[re.Match[str]],
) -> bool:
    if not mentions:
        return False
    first_mention = min(mention.start() for mention in mentions)
    last_mention = max(mention.end() for mention in mentions)
    postfix_markers = [
        marker
        for marker in _POSTFIX_NICE_MARKERS.finditer(segment)
        if marker.start() >= last_mention
    ]
    if not postfix_markers:
        return False
    postfix = postfix_markers[0]
    if _CONTRAST_MARKERS.search(segment, first_mention, postfix.start()):
        return False
    return not any(first_mention <= marker.start() < postfix.start() for marker in must_markers)


def extract_skills(title: str | None, description: str | None) -> SkillExtraction:
    """Extract only technologies that literally occur in title/description."""
    must_have: set[str] = set()
    nice_to_have: set[str] = set()

    # A literal technology in the role title describes the candidate being
    # sought (for example, "Django Developer") and is therefore mandatory.
    for skill, pattern in _TECHNOLOGY_PATTERNS:
        if pattern.search(title or ""):
            must_have.add(skill)

    for segment, inherited_context in _contextual_segments(description or ""):
        must_markers = list(_MUST_HAVE_MARKERS.finditer(segment))
        nice_markers = list(_NICE_TO_HAVE_MARKERS.finditer(segment))
        mentions = [
            mention for _, pattern in _TECHNOLOGY_PATTERNS for mention in pattern.finditer(segment)
        ]
        postfix_nice_scope = _postfix_nice_scopes_all_mentions(segment, mentions, must_markers)
        for skill, pattern in _TECHNOLOGY_PATTERNS:
            for mention in pattern.finditer(segment):
                classification = _classify_mention(
                    mention,
                    must_markers,
                    nice_markers,
                    inherited_context,
                    segment,
                    postfix_nice_scope,
                )
                if classification == "must":
                    must_have.add(skill)
                elif classification == "nice":
                    nice_to_have.add(skill)

    # A mandatory mention wins if the same technology also appears in an
    # optional sentence elsewhere in the vacancy.
    nice_to_have.difference_update(must_have)
    return SkillExtraction(
        must_have_skills=sorted(must_have),
        nice_to_have_skills=sorted(nice_to_have),
    )
