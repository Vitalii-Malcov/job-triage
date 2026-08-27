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
    r"kenntnisse\s+(?:in|mit)|erfahrung\s+(?:mit|in)|"
    r"bringst\b.{0,100}\bmit|beherrsch(?:st|en)|(?:sicherer|versierter)\s+umgang\s+mit|"
    r"must|(?<!not\s)required|requirements?|experience\s+(?:with|in)|"
    r"proficiency\s+(?:in|with)|knowledge\s+of|skilled\s+in"
    r")\b",
    re.IGNORECASE,
)
_SEGMENT_BOUNDARY = re.compile(r"(?:\r?\n)+|(?<=[.!?;])\s+")


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
) -> str | None:
    if must_markers and not nice_markers:
        return "must"
    if nice_markers and not must_markers:
        return "nice"
    if not must_markers and not nice_markers:
        return None

    nearest_must = min(_distance(mention, marker) for marker in must_markers)
    nearest_nice = min(_distance(mention, marker) for marker in nice_markers)
    return "must" if nearest_must <= nearest_nice else "nice"


def extract_skills(title: str | None, description: str | None) -> SkillExtraction:
    """Extract only technologies that literally occur in title/description."""
    must_have: set[str] = set()
    nice_to_have: set[str] = set()

    # A literal technology in the role title describes the candidate being
    # sought (for example, "Django Developer") and is therefore mandatory.
    for skill, pattern in _TECHNOLOGY_PATTERNS:
        if pattern.search(title or ""):
            must_have.add(skill)

    for segment in _SEGMENT_BOUNDARY.split(description or ""):
        must_markers = list(_MUST_HAVE_MARKERS.finditer(segment))
        nice_markers = list(_NICE_TO_HAVE_MARKERS.finditer(segment))
        for skill, pattern in _TECHNOLOGY_PATTERNS:
            for mention in pattern.finditer(segment):
                classification = _classify_mention(mention, must_markers, nice_markers)
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
