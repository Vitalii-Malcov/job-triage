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
    r"\b(?:nice[ -]to[ -]have|wünschenswert|von vorteil|idealerweise|optional|bonus)\b",
    re.IGNORECASE,
)
_SEGMENT_BOUNDARY = re.compile(r"(?:\r?\n)+|(?<=[.!?;])\s+")


def extract_skills(title: str | None, description: str | None) -> SkillExtraction:
    """Extract only technologies that literally occur in title/description."""
    text = "\n".join(part for part in (title or "", description or "") if part)
    must_have: set[str] = set()
    nice_to_have: set[str] = set()

    for segment in _SEGMENT_BOUNDARY.split(text):
        is_nice_to_have = bool(_NICE_TO_HAVE_MARKERS.search(segment))
        for skill, pattern in _TECHNOLOGY_PATTERNS:
            if not pattern.search(segment):
                continue
            if is_nice_to_have:
                nice_to_have.add(skill)
            else:
                must_have.add(skill)

    # A mandatory mention wins if the same technology also appears in an
    # optional sentence elsewhere in the vacancy.
    nice_to_have.difference_update(must_have)
    return SkillExtraction(
        must_have_skills=sorted(must_have),
        nice_to_have_skills=sorted(nice_to_have),
    )
