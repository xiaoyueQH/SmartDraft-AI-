import pytest
from pydantic import ValidationError

from backend.config import Settings


def test_environment_overrides_nested_settings(monkeypatch):
    monkeypatch.setenv("SF_APP__PORT", "8191")
    monkeypatch.setenv("SF_DATABASE__POOL_SIZE", "12")
    settings = Settings(_env_file=None)
    assert settings.app.port == 8191
    assert settings.database.pool_size == 12


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_invalid_pool_size_is_rejected(monkeypatch, value):
    monkeypatch.setenv("SF_DATABASE__POOL_SIZE", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_unknown_nested_setting_is_rejected(monkeypatch):
    monkeypatch.setenv("SF_DATABASE__POOL_SZE", "12")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_model_profiles_are_independently_configurable_and_validated(monkeypatch):
    monkeypatch.setenv("SF_AGENT__DRAFT_WRITER__MODEL", "writer-snapshot")
    monkeypatch.setenv("SF_AGENT__DRAFT_WRITER__CONCURRENCY", "3")
    settings = Settings(_env_file=None)
    assert settings.agent.draft_writer.model == "writer-snapshot"
    assert settings.agent.draft_writer.concurrency == 3
    assert settings.agent.consultation.model == ""
    monkeypatch.setenv("SF_AGENT__DRAFT_WRITER__CONCURRENCY", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_credentials_are_masked_and_url_preserves_special_characters(monkeypatch):
    password = "local-secret@:/?#"
    monkeypatch.setenv("SF_DATABASE__PASSWORD", password)
    settings = Settings(_env_file=None)
    assert password not in settings.model_dump_json()
    assert password not in repr(settings)
    assert password not in str(settings.database.connection_url())
    assert settings.database.connection_url().password == password
