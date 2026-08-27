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
        ("html", r"(?<![\w./?=&#%])html5?(?!\w)"),
        ("css", r"(?<!\w)(?:css3?|scss)(?!\w)"),
        ("ajax", r"(?<!\w)ajax(?!\w)"),
        ("c", r"(?<!\w)c(?![\w+#]|\s+(?:niveau|level)\b)"),
        ("c#", r"(?<!\w)c#(?!\w)"),
        ("c++", r"(?<!\w)c\+\+(?!\w)"),
        ("go", r"(?<!\w)(?:(?-i:Go)|golang)(?!\w)"),
        ("rust", r"(?<!\w)rust(?!\w)"),
        (".net", r"(?<!\w)(?<!vb\s)(?<!asp\s)\.net(?!\w)"),
        ("vb.net", r"(?<!\w)vb\s*\.\s*net(?!\w)"),
        ("asp.net", r"(?<!\w)asp\s*\.\s*net(?!\w)"),
        ("php", r"(?<!\w)php(?!\w)"),
        ("ruby", r"(?<!\w)ruby(?!\w)"),
        ("kotlin", r"(?<!\w)kotlin(?!\w)"),
        ("scala", r"(?<!\w)scala(?!\w)"),
        ("flask", r"(?<!\w)flask(?!\w)"),
        ("django", r"(?<!\w)django(?!\w)"),
        ("fastapi", r"(?<!\w)fast[ -]?api(?!\w)"),
        ("spring mvc", r"(?<!\w)spring\s+mvc(?!\w)"),
        ("spring", r"(?<!\w)spring(?:\s+(?:boot|framework))(?!\w)"),
        ("node.js", r"(?<!\w)node(?:\.js|js)(?!\w)"),
        ("react", r"(?<!\w)react(?:\.js|js)?(?!\w)"),
        ("angular", r"(?<!\w)angular(?:\.js|js)?(?!\w)"),
        ("vue.js", r"(?<!\w)vue(?:\.js|js)(?!\w)"),
        ("ngrx", r"(?<!\w)ngrx(?!\w)"),
        ("shopify", r"(?<!\w)shopify(?!\w)"),
        ("liquid", r"(?<!\w)liquid(?!\w)"),
        ("flutter", r"(?<!\w)flutter(?!\w)"),
        ("dart", r"(?<!\w)dart(?!\w)"),
        ("wpf", r"(?<!\w)wpf(?!\w)"),
        ("prism", r"(?<!\w)prism(?!\w)"),
        ("qt", r"(?<!\w)qt(?!\w)"),
        ("delphi", r"(?<!\w)delphi(?!\w)"),
        (
            "sql",
            r"(?<!\w)(?<!\bms\s)(?<!\bmicrosoft\s)(?<!\bt-)(?<!\bt\s)"
            r"(?<!\btransact-)(?<!\btransact\s)sql(?!\w)",
        ),
        ("t-sql", r"(?<!\w)(?:t[ -]?sql|transact[ -]?sql)(?!\w)"),
        (
            "ms sql server",
            r"(?<!\w)(?:microsoft\s+sql\s+server|ms\s*sql(?:\s+server)?|mssql)(?!\w)",
        ),
        ("postgresql", r"(?<!\w)(?:postgresql|postgres)(?!\w)"),
        ("mysql", r"(?<!\w)mysql(?!\w)"),
        ("mariadb", r"(?<!\w)mariadb(?!\w)"),
        ("mongodb", r"(?<!\w)(?:mongodb|mongo\s+db)(?!\w)"),
        ("redis", r"(?<!\w)redis(?!\w)"),
        ("elasticsearch", r"(?<!\w)elasticsearch(?!\w)"),
        ("solr", r"(?<!\w)solr(?!\w)"),
        ("sqlalchemy", r"(?<!\w)sqlalchemy(?!\w)"),
        ("pydantic", r"(?<!\w)pydantic(?!\w)"),
        ("jpa", r"(?<!\w)jpa(?!\w)"),
        ("jsf", r"(?<!\w)jsf(?!\w)"),
        ("gwt", r"(?<!\w)gwt(?!\w)"),
        ("git", r"(?<!\w)git(?!\w)"),
        ("svn", r"(?<!\w)svn(?!\w)"),
        ("jira", r"(?<!\w)jira(?!\w)"),
        ("confluence", r"(?<!\w)confluence(?!\w)"),
        ("jsm", r"(?<!\w)jsm(?!\w)"),
        ("active directory", r"(?<!\w)(?:microsoft\s+)?active\s+directory(?!\w)"),
        ("docker", r"(?<!\w)docker(?!\w)"),
        ("kubernetes", r"(?<!\w)(?:kubernetes|k8s)(?!\w)"),
        ("windows", r"(?<!\w)windows(?!\w)"),
        ("linux", r"(?<!\w)linux(?!\w)"),
        ("unix", r"(?<!\w)unix(?!\w)"),
        ("rest", r"(?<!\w)rest(?:ful)?(?!\w)"),
        ("graphql", r"(?<!\w)graphql(?!\w)"),
        ("openapi", r"(?<!\w)openapi(?!\w)"),
        ("swagger", r"(?<!\w)swagger(?!\w)"),
        ("xml", r"(?<!\w)xml(?!\w)"),
        ("oauth", r"(?<!\w)oauth(?:2(?:\.0)?| 2)?(?!\w)"),
        ("aws", r"(?<!\w)aws(?!\w)"),
        ("azure devops", r"(?<!\w)(?:microsoft\s+)?azure\s+devops(?!\w)"),
        ("azure", r"(?<!\w)(?:microsoft\s+)?azure(?!\s+devops\b)(?!\w)"),
        ("google cloud", r"(?<!\w)(?:google\s+cloud|gcp)(?!\w)"),
        ("terraform", r"(?<!\w)terraform(?!\w)"),
        ("ansible", r"(?<!\w)ansible(?!\w)"),
        ("jenkins", r"(?<!\w)jenkins(?!\w)"),
        ("teamcity", r"(?<!\w)teamcity(?!\w)"),
        ("visual studio", r"(?<!\w)visual\s+studio(?!\s+code\b)(?!\w)"),
        ("github actions", r"(?<!\w)github\s+actions(?!\w)"),
        ("ci/cd", r"(?<!\w)ci\s*/\s*cd(?!\w)"),
        ("cmake", r"(?<!\w)cmake(?!\w)"),
        ("make", r"(?<!\w)(?:gnu\s+make|(?-i:Make))(?!\w)"),
        ("kafka", r"(?<!\w)(?:apache\s+)?kafka(?!\w)"),
        ("rabbitmq", r"(?<!\w)rabbitmq(?!\w)"),
        ("oracle", r"(?<!\w)oracle(?!\w)"),
        ("prometheus", r"(?<!\w)prometheus(?!\w)"),
        ("grafana", r"(?<!\w)grafana(?!\w)"),
        ("elk", r"(?<!\w)elk(?:\s+stack)?(?!\w)"),
        ("fluentd", r"(?<!\w)fluentd(?!\w)"),
        ("splunk", r"(?<!\w)splunk(?!\w)"),
        ("servicenow", r"(?<!\w)service\s*now(?!\w)"),
        ("bash", r"(?<!\w)bash(?!\w)"),
        ("matlab", r"(?<!\w)matlab(?!\w)"),
        ("selenium", r"(?<!\w)selenium(?!\w)"),
        ("playwright", r"(?<!\w)playwright(?!\w)"),
        ("postman", r"(?<!\w)postman(?!\w)"),
        ("xray", r"(?<!\w)x[ -]?ray(?!\w)"),
        ("hp alm", r"(?<!\w)hp\s+alm(?!\w)"),
        ("solution manager", r"(?<!\w)(?:sap\s+)?solution\s+manager(?!\w)"),
        ("readyapi", r"(?<!\w)ready\s*api(?!\w)"),
        ("soapui", r"(?<!\w)soap\s*ui(?!\w)"),
        ("tosca", r"(?<!\w)tosca(?!\w)"),
        ("groovy", r"(?<!\w)groovy(?:\s+script)?(?!\w)"),
        ("pytest", r"(?<!\w)pytest(?!\w)"),
    )
)

KNOWN_TECHNOLOGIES: tuple[str, ...] = tuple(name for name, _ in _TECHNOLOGY_PATTERNS)

_NICE_TO_HAVE_MARKERS = re.compile(
    r"\b(?:nice[ -]to[ -]have|good[ -]to[ -]have|preferred|desirable|optional|bonus|"
    r"wünschenswert|von vorteil|idealerweise|nicht erforderlich|not required|"
    r"(?:is|are|would\s+be)\s+(?:a\s+)?plus)\b",
    re.IGNORECASE,
)
_MUST_HAVE_MARKERS = re.compile(
    r"\b(?:"
    r"(?<!nicht\s)erforderlich|vorausgesetzt|voraussetzung(?:en)?|"
    r"(?:fach)?kenntnisse(?:\s*(?::|(?:in|mit)\b))?|"
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
    r"\b(?:(?:ist|sind|wäre|wären)\s+)?(?:von vorteil|wünschenswert|preferred|desirable)|"
    r"(?:is|are|would\s+be)\s+(?:a\s+)?plus\b",
    re.IGNORECASE,
)
_EXPLICIT_REQUIRED_MARKERS = re.compile(
    r"\b(?:(?<!nicht\s)erforderlich|vorausgesetzt|must|(?<!not\s)required)\b",
    re.IGNORECASE,
)
_CONTRAST_MARKERS = re.compile(r"\b(?:aber|hingegen|jedoch|während|but|while|whereas)\b", re.I)
_PROFILE_REQUIREMENT_MARKERS = re.compile(
    r"\b(?:grundverständnis\s+(?:von|für)|understanding\s+of)\b",
    re.IGNORECASE,
)
_IDEALLY_MARKER = re.compile(r"\bidealerweise\b", re.IGNORECASE)
_ABBREVIATIONS = re.compile(r"\b(?:z\.\s?b\.|e\.g\.|d\.\s?h\.)", re.IGNORECASE)
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

    ideally_markers = list(_IDEALLY_MARKER.finditer(segment))
    if ideally_markers:
        first_ideally = ideally_markers[0].start()
        has_required_prefix = inherited_context == "must" or any(
            marker.start() < first_ideally for marker in must_markers
        )
        if has_required_prefix:
            return "must" if mention.end() <= first_ideally else "nice"

    clause_start = segment.rfind(",", 0, mention.start()) + 1
    clause_end = segment.find(",", mention.end())
    if clause_end == -1:
        clause_end = len(segment)
    clause_must_markers = [
        marker for marker in must_markers if clause_start <= marker.start() < clause_end
    ]
    clause_nice_markers = [
        marker for marker in nice_markers if clause_start <= marker.start() < clause_end
    ]
    if clause_must_markers and not clause_nice_markers:
        return "must"
    if clause_nice_markers and not clause_must_markers:
        return "nice"

    if inherited_context == "must" and nice_markers and not must_markers:
        first_nice_marker = min(marker.start() for marker in nice_markers)
        return "must" if mention.end() <= first_nice_marker else "nice"
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

    segments = []
    for line, context in lines:
        protected = _ABBREVIATIONS.sub(lambda match: match.group().replace(".", "\x00"), line)
        segments.extend(
            (segment.replace("\x00", "."), context)
            for segment in _SEGMENT_BOUNDARY.split(protected)
            if segment.strip()
        )
    return segments


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
    return not _EXPLICIT_REQUIRED_MARKERS.search(segment, first_mention, postfix.start())


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
        if inherited_context == "must":
            must_markers.extend(_PROFILE_REQUIREMENT_MARKERS.finditer(segment))
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
