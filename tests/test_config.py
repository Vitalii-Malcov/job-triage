import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_default_settings_are_valid():
    Settings()


def test_negative_company_research_ttl_hours_rejected():
    with pytest.raises(ValidationError):
        Settings(company_research_ttl_hours=-1)


def test_negative_company_research_auto_max_per_run_rejected():
    with pytest.raises(ValidationError):
        Settings(company_research_auto_max_per_run=-1)


def test_zero_is_a_valid_boundary_for_company_research_settings():
    settings = Settings(company_research_ttl_hours=0, company_research_auto_max_per_run=0)
    assert settings.company_research_ttl_hours == 0
    assert settings.company_research_auto_max_per_run == 0
