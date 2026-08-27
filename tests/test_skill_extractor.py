import pytest

from app.agents.skill_extractor import extract_skills


def test_extracts_literal_technologies_from_realistic_german_text():
    result = extract_skills(
        "Backend Entwickler",
        "Erfahrung mit Python, Flask und PostgreSQL ist erforderlich. "
        "Docker und Kubernetes sind wünschenswert.",
    )

    assert result.must_have_skills == ["flask", "postgresql", "python"]
    assert result.nice_to_have_skills == ["docker", "kubernetes"]
    assert result.skill_source == "description_extracted"


def test_does_not_infer_related_technologies_that_are_absent():
    result = extract_skills(
        "Junior Python Developer",
        "You will maintain Python services and collaborate with the team.",
    )

    assert result.must_have_skills == ["python"]
    assert result.nice_to_have_skills == []
    assert "fastapi" not in result.must_have_skills
    assert "docker" not in result.must_have_skills
    assert "postgresql" not in result.must_have_skills


def test_empty_or_none_description_is_safe_and_keeps_extracted_provenance():
    result = extract_skills("Backend Developer", None)

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == []
    assert result.skill_source == "description_extracted"


def test_literal_title_match_is_allowed_without_inferring_adjacent_skills():
    result = extract_skills("Django Developer", "")

    assert result.must_have_skills == ["django"]
    assert result.nice_to_have_skills == []


def test_explicit_german_requirement_classifies_multiple_technologies_as_must_have():
    result = extract_skills("Backend Entwickler", "Erfahrung mit Python und Django erforderlich")

    assert result.must_have_skills == ["django", "python"]
    assert result.nice_to_have_skills == []


def test_german_optional_context_classifies_skill_as_nice_to_have():
    result = extract_skills("Backend Entwickler", "Python wäre wünschenswert")

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == ["python"]


def test_company_stack_mentions_are_context_only():
    result = extract_skills(
        "Backend Entwickler",
        "Unser Stack besteht aus Python, PostgreSQL und Docker",
    )

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == []


def test_required_mention_wins_over_optional_mention_of_same_technology():
    result = extract_skills(
        "Backend Entwickler",
        "Python wäre wünschenswert. Erfahrung mit Python ist erforderlich.",
    )

    assert result.must_have_skills == ["python"]
    assert result.nice_to_have_skills == []


def test_several_technologies_in_one_english_requirement_segment():
    result = extract_skills(
        "Backend Engineer",
        "Experience with Python, PostgreSQL and Docker is required",
    )

    assert result.must_have_skills == ["docker", "postgresql", "python"]
    assert result.nice_to_have_skills == []


def test_nearest_marker_separates_required_and_optional_mentions_in_one_segment():
    result = extract_skills(
        "Backend Engineer",
        "Python is required, while Docker is optional",
    )

    assert result.must_have_skills == ["python"]
    assert result.nice_to_have_skills == ["docker"]


def test_bmw_postfix_von_vorteil_applies_to_the_whole_database_list():
    result = extract_skills(
        "Softwareentwickler",
        "Erfahrung mit Datenbanken wie PostgreSQL, Oracle oder MS SQL sind von Vorteil.",
    )

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == ["ms sql server", "oracle", "postgresql"]


def test_allergosan_postfix_von_vorteil_applies_to_typescript_and_rest():
    result = extract_skills(
        "Softwareentwickler",
        "Kenntnisse in Liquid, TypeScript und RESTful APIs von Vorteil.",
    )

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == ["liquid", "rest", "typescript"]


def test_nevaris_fachkenntnisse_context_continues_into_following_bullet():
    result = extract_skills(
        "Softwareentwickler",
        "Fachkenntnisse: C/C++ oder C#, Microsoft .NET\n"
        "- Datenbanken (SQL oder ORACLE), Client-Server-Architektur",
    )

    assert result.must_have_skills == [".net", "c", "c#", "c++", "oracle", "sql"]
    assert result.nice_to_have_skills == []


def test_rewe_multiline_requirement_keeps_context_for_continued_technology_list():
    result = extract_skills(
        "SRE",
        "Strong understanding of cloud infrastructure (specifically GCP) and\n"
        "* containerization technologies (Docker, Kubernetes).\n"
        "* Solid knowledge of system administration (Linux).",
    )

    assert result.must_have_skills == ["docker", "google cloud", "kubernetes", "linux"]
    assert result.nice_to_have_skills == []


def test_agrarcommander_mit_git_vertraut_is_must_have():
    result = extract_skills(
        "Softwareentwickler",
        "Du bist mit Git sowie automatisierten Build- und Testprozessen vertraut.",
    )

    assert result.must_have_skills == ["git"]
    assert result.nice_to_have_skills == []


@pytest.mark.parametrize(
    "header",
    [
        "Das bringst Du mit",
        "Das bringen Sie mit",
        "Dein Profil",
        "Ihr Profil",
        "Your profile",
        "Qualifications",
        "Qualifikationen",
        "Was sollst du mitbringen?",
    ],
)
def test_requirement_section_headers_apply_to_bullets_until_new_section(header):
    result = extract_skills(
        "Backend Engineer",
        f"{header}:\n* Python\n* Docker und Kubernetes\nUnsere Aufgaben:\n* PostgreSQL betreiben",
    )

    assert result.must_have_skills == ["docker", "kubernetes", "python"]
    assert result.nice_to_have_skills == []


@pytest.mark.parametrize("header", ["Ideal Skills", "Nice to have", "Wünschenswert"])
def test_optional_section_headers_apply_to_bullets_until_new_section(header):
    result = extract_skills(
        "Backend Engineer",
        f"{header}:\n"
        "* Experience with Python\n"
        "* Docker und Kubernetes\n"
        "Responsibilities:\n"
        "* PostgreSQL betreiben",
    )

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == ["docker", "kubernetes", "python"]


@pytest.mark.parametrize(
    ("description", "skill"),
    [
        ("Mehrjährige Berufserfahrung als Python Developer.", "python"),
        ("Erfahrungen mit Docker sind erforderlich.", "docker"),
        ("Erfahrung im Umgang mit Kubernetes.", "kubernetes"),
    ],
)
def test_live_ba_requirement_phrasings_are_must_have(description, skill):
    result = extract_skills("Softwareentwickler", description)

    assert result.must_have_skills == [skill]
    assert result.nice_to_have_skills == []


def test_unknown_nested_heading_preserves_requirement_section_context():
    result = extract_skills(
        "Backend Engineer",
        "Dein Profil:\nTechnologien:\n- Python\n- Docker",
    )

    assert result.must_have_skills == ["docker", "python"]
    assert result.nice_to_have_skills == []


def test_unknown_nested_heading_preserves_optional_section_context():
    result = extract_skills(
        "Backend Engineer",
        "Ideal Skills:\nCloud:\n- AWS\n- Docker",
    )

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == ["aws", "docker"]


def test_unknown_heading_without_parent_does_not_create_requirement_context():
    result = extract_skills("Backend Engineer", "Technologies:\n- Python")

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == []


def test_known_responsibilities_header_still_ends_requirement_section():
    result = extract_skills(
        "Backend Engineer",
        "Dein Profil:\n- Python\nResponsibilities:\n- Docker",
    )

    assert result.must_have_skills == ["python"]
    assert result.nice_to_have_skills == []


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Erfahrung mit Jira ist erforderlich.", ["jira"]),
        ("Kenntnisse in Microsoft SQL Server sind erforderlich.", ["ms sql server"]),
        ("Erfahrung mit Windows ist erforderlich.", ["windows"]),
        ("Erfahrung mit CI/CD ist erforderlich.", ["ci/cd"]),
        ("Erfahrung mit Rust ist erforderlich.", ["rust"]),
    ],
)
def test_live_audit_technologies_are_recognized_as_requirements(description, expected):
    result = extract_skills("Softwareentwickler", description)

    assert result.must_have_skills == expected
    assert result.nice_to_have_skills == []


def test_c_requirement_does_not_match_csharp_or_cpp_as_c():
    result = extract_skills("Softwareentwickler", "Kenntnisse in C, C# und C++ erforderlich.")

    assert result.must_have_skills == ["c", "c#", "c++"]
    assert (
        "c"
        not in extract_skills(
            "Softwareentwickler", "Kenntnisse in C# und C++ erforderlich."
        ).must_have_skills
    )


def test_oracle_in_postfix_optional_database_list_is_nice_to_have():
    result = extract_skills(
        "Softwareentwickler",
        "PostgreSQL, Oracle oder MSSQL sind von Vorteil.",
    )

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == ["ms sql server", "oracle", "postgresql"]


def test_is_a_plus_marks_python_as_optional():
    result = extract_skills("Softwareentwickler", "Matlab or Python is a plus.")

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == ["matlab", "python"]


def test_idealerweise_only_applies_to_the_following_database_list():
    result = extract_skills(
        "Softwareentwickler",
        "Dein Profil:\nSQL Kenntnisse, idealerweise PostgreSQL/MySQL.",
    )

    assert result.must_have_skills == ["sql"]
    assert result.nice_to_have_skills == ["mysql", "postgresql"]


def test_agrarcommander_idealerweise_scope_keeps_sql_required():
    result = extract_skills(
        "Softwareentwickler",
        "Qualifikationen:\n"
        "Du kennst dich mit relationalen Datenbanken und SQL aus - "
        "idealerweise mit PostgreSQL, MySQL/MariaDB oder MSSQL.",
    )

    assert result.must_have_skills == ["sql"]
    assert result.nice_to_have_skills == [
        "mariadb",
        "ms sql server",
        "mysql",
        "postgresql",
    ]


def test_idealerweise_tail_does_not_make_preceding_tools_optional():
    result = extract_skills(
        "Softwareentwickler",
        "Kenntnisse gängiger Werkzeuge wie Git, Jira, Confluence und idealerweise Jenkins.",
    )

    assert result.must_have_skills == ["confluence", "git", "jira"]
    assert result.nice_to_have_skills == ["jenkins"]


def test_compound_postfix_optional_scope_includes_angular_and_azure():
    result = extract_skills(
        "Softwareentwickler",
        "Angular-Kenntnisse und Erfahrung mit Azure sind von Vorteil.",
    )

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == ["angular", "azure"]


def test_explicit_requirement_before_optional_postfix_remains_must_have():
    result = extract_skills(
        "Softwareentwickler",
        "Python ist erforderlich, Docker wäre von Vorteil.",
    )

    assert result.must_have_skills == ["python"]
    assert result.nice_to_have_skills == ["docker"]


@pytest.mark.parametrize("abbreviation", ["z. B.", "z.B.", "e.g.", "d. h.", "d.h."])
def test_common_abbreviation_does_not_split_git_from_requirement(abbreviation):
    result = extract_skills(
        "Softwareentwickler",
        f"Kenntnisse in Versionsverwaltung ({abbreviation} Git) sind von Vorteil.",
    )

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == ["git"]


@pytest.mark.parametrize(
    "phrase",
    ["Grundverständnis von", "Grundverständnis für", "Understanding of"],
)
def test_understanding_construction_is_requirement_inside_profile_section(phrase):
    result = extract_skills(
        "Softwareentwickler",
        f"Ihr Profil:\n{phrase} AWS und Azure.",
    )

    assert result.must_have_skills == ["aws", "azure"]
    assert result.nice_to_have_skills == []


@pytest.mark.parametrize(
    ("literal", "normalized"),
    [
        ("HTML5", "html"),
        ("CSS3", "css"),
        ("AJAX", "ajax"),
        ("VB.NET", "vb.net"),
        ("VB .NET", "vb.net"),
        ("ASP.NET", "asp.net"),
        ("ASP .NET", "asp.net"),
        ("T-SQL", "t-sql"),
        ("T SQL", "t-sql"),
        ("Microsoft Active Directory", "active directory"),
        ("JSM", "jsm"),
        ("JPA", "jpa"),
        ("WPF", "wpf"),
        ("Qt", "qt"),
        ("Pydantic", "pydantic"),
        ("Golang", "go"),
        ("Bash", "bash"),
        ("Prometheus", "prometheus"),
        ("Grafana", "grafana"),
        ("ELK Stack", "elk"),
        ("Fluentd", "fluentd"),
        ("Splunk", "splunk"),
        ("ServiceNow", "servicenow"),
        ("XML", "xml"),
        ("CMake", "cmake"),
        ("GNU Make", "make"),
        ("Selenium", "selenium"),
        ("Playwright", "playwright"),
        ("Postman", "postman"),
        ("Xray", "xray"),
        ("HP ALM", "hp alm"),
        ("SAP Solution Manager", "solution manager"),
        ("ReadyAPI", "readyapi"),
        ("SoapUI", "soapui"),
        ("Tosca", "tosca"),
        ("Groovy Script", "groovy"),
        ("Flutter", "flutter"),
        ("Dart", "dart"),
        ("Matlab", "matlab"),
        ("TeamCity", "teamcity"),
        ("Azure DevOps", "azure devops"),
        ("Shopify", "shopify"),
        ("Liquid", "liquid"),
        ("NgRx", "ngrx"),
        ("Visual Studio", "visual studio"),
        ("Prism", "prism"),
        ("Delphi", "delphi"),
        ("Spring MVC", "spring mvc"),
        ("JSF", "jsf"),
        ("GWT", "gwt"),
    ],
)
def test_confirmed_live_audit_technology_literal_is_normalized(literal, normalized):
    result = extract_skills("Softwareentwickler", f"Erfahrung mit {literal} erforderlich.")

    assert result.must_have_skills == [normalized]
    assert result.nice_to_have_skills == []


def test_go_does_not_match_ordinary_lowercase_english_word():
    result = extract_skills("Softwareentwickler", "You must go through the requirements.")

    assert result.must_have_skills == []


def test_make_does_not_match_ordinary_lowercase_english_word():
    result = extract_skills("Softwareentwickler", "You must make reliable software.")

    assert result.must_have_skills == []


def test_existing_c_family_patterns_remain_independent_with_new_dictionary_entries():
    result = extract_skills("Softwareentwickler", "Kenntnisse in C, C# und C++ erforderlich.")

    assert result.must_have_skills == ["c", "c#", "c++"]


def test_azure_devops_is_specific_and_not_duplicated_as_azure():
    result = extract_skills("Softwareentwickler", "Erfahrung mit Azure DevOps erforderlich.")

    assert result.must_have_skills == ["azure devops"]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/index.html",
        "https://example.com/html/jobs",
        "https://example.com/jobs?format=html",
    ],
)
def test_html_in_url_is_not_extracted_as_skill(url):
    result = extract_skills("Softwareentwickler", f"Details are required: {url}")

    assert result.must_have_skills == []


def test_scss_and_css_variants_normalize_without_duplicates():
    result = extract_skills(
        "Softwareentwickler",
        "Kenntnisse in CSS, CSS3 und SCSS erforderlich.",
    )

    assert result.must_have_skills == ["css"]


def test_spring_mvc_does_not_also_extract_generic_spring():
    result = extract_skills("Softwareentwickler", "Erfahrung mit Spring MVC erforderlich.")

    assert result.must_have_skills == ["spring mvc"]


def test_spring_boot_keeps_existing_spring_normalization():
    result = extract_skills("Softwareentwickler", "Erfahrung mit Spring Boot erforderlich.")

    assert result.must_have_skills == ["spring"]


def test_spring_mvc_and_spring_boot_remain_independent_literals():
    result = extract_skills(
        "Softwareentwickler",
        "Erfahrung mit Spring MVC und Spring Boot erforderlich.",
    )

    assert result.must_have_skills == ["spring", "spring mvc"]


def test_visual_studio_does_not_match_visual_studio_code():
    result = extract_skills(
        "Softwareentwickler",
        "Erfahrung mit Visual Studio Code erforderlich.",
    )

    assert result.must_have_skills == []


def test_t_sql_disambiguation_does_not_hide_sql_after_word_ending_in_t():
    result = extract_skills("Softwareentwickler", "Erfahrung mit SQL Server erforderlich.")

    assert result.must_have_skills == ["sql"]
