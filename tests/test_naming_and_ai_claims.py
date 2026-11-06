"""Architecture 3 (naming): the project's own descriptions must match what the code does."""

import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LLM_SDKS = {
    "anthropic",
    "openai",
    "cohere",
    "mistralai",
    "ollama",
    "litellm",
    "langchain",
    "langchain_core",
    "langchain_openai",
    "transformers",
    "llama_cpp",
    "vertexai",
    "google_generativeai",
    "google_genai",
}
GOOGLE_LLM_MODULES = ("google.generativeai", "google.genai")
GOOGLE_LLM_NAMES = {"generativeai", "genai"}


def _python_files():
    for folder in ("app", "scripts"):
        yield from (ROOT / folder).rglob("*.py")


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
            if node.module == "google" and any(a.name in GOOGLE_LLM_NAMES for a in node.names):
                roots.add("google_genai")
    for module in (a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names):
        if module.startswith(GOOGLE_LLM_MODULES):
            roots.add("google_genai")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(GOOGLE_LLM_MODULES):
            roots.add("google_genai")
    return roots


def test_no_module_imports_an_llm_sdk():
    offenders = {
        str(path.relative_to(ROOT)): sorted(found)
        for path in _python_files()
        if (found := _imported_roots(path) & LLM_SDKS)
    }
    assert offenders == {}


def test_no_llm_sdk_is_a_declared_dependency():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = list(project["dependencies"])
    for extra in project.get("optional-dependencies", {}).values():
        declared.extend(extra)
    names = {
        re.split(r"[\s\[<>=!~;]", dep.strip(), maxsplit=1)[0].lower().replace("_", "-")
        for dep in declared
    }
    sdk_names = {sdk.replace("_", "-") for sdk in LLM_SDKS}
    assert names  # sanity: the parser saw the dependency list
    assert names & sdk_names == set()


def test_the_sdk_guard_itself_detects_google_llm_imports(tmp_path):
    for index, line in enumerate(
        (
            "import google.generativeai",
            "from google import genai",
            "from google.genai import Client",
            "import anthropic",
            "from openai import OpenAI",
        )
    ):
        probe = tmp_path / f"probe_{index}.py"
        probe.write_text(line + chr(10), encoding="utf-8")
        assert _imported_roots(probe) & LLM_SDKS, line


def test_package_description_makes_no_ai_claim_the_code_does_not_support():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    description = project["description"].lower()
    assert "multi-agent" not in description and "ai-powered" not in description
    assert "jobtriage" in description and "no llm" in description


def test_fastapi_title_and_readme_use_the_product_name():
    from app.main import app

    assert app.title == "JobTriage"
    first_line = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("# JobTriage")
    assert "AI-Assisted" not in first_line


def test_agents_package_states_that_it_is_rule_based():
    import app.agents as agents

    assert "Nothing in this package calls a language model" in agents.__doc__


def test_the_gmail_draft_marker_domain_is_a_protocol_constant_and_unchanged():
    # Renaming it would make every already-created draft unrecognizable.
    from app.providers.email.draft_base import MESSAGE_ID_PATTERN

    assert MESSAGE_ID_PATTERN.fullmatch("<s9d." + "a" * 32 + "@ai-job-search.invalid>")
