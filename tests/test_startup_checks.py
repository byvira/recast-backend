"""Startup checks: a hosted deploy must say it is production, and production must have its platform-key encryption key.

Pure functions, so no database or app start is needed.
"""

from app.core.startup_checks import fatal_problems, warnings_for


def test_hosted_deploy_without_production_is_refused():
    problems = fatal_problems(environment="development", hosted=True)
    assert len(problems) == 1
    assert "ENVIRONMENT=production" in problems[0]


def test_hosted_deploy_in_production_is_allowed():
    assert fatal_problems(environment="production", hosted=True) == []


def test_local_development_is_allowed():
    assert fatal_problems(environment="development", hosted=False) == []


def test_environment_name_is_compared_without_case_or_spaces():
    assert fatal_problems(environment=" Production ", hosted=True) == []


def test_production_without_the_encryption_key_warns():
    notes = warnings_for(environment="production", fernet_key="")
    assert len(notes) == 1
    assert "FERNET_SECRET_KEY" in notes[0]


def test_production_with_the_encryption_key_has_no_warning():
    assert warnings_for(environment="production", fernet_key="set") == []


def test_development_without_the_encryption_key_has_no_warning():
    assert warnings_for(environment="development", fernet_key="") == []
