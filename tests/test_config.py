import pytest

from app.config import ConfigError, Settings
from app.db import make_session_factory


def test_live_mode_fails_loudly_without_key():
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        Settings.from_env({"MOCK_GEMINI": "false", "SESSION_SECRET": "x"})


def test_live_mode_requires_session_secret():
    with pytest.raises(ConfigError, match="SESSION_SECRET"):
        Settings.from_env({"MOCK_GEMINI": "false", "GEMINI_API_KEY": "k"})


def test_mock_mode_needs_no_key(tmp_path):
    s = Settings.from_env({"MOCK_GEMINI": "true", "DATA_DIR": str(tmp_path)})
    assert s.mock_gemini and s.gemini_api_key is None and s.session_secret


def test_only_gemini_3_8_flash_is_allowed():
    with pytest.raises(ConfigError, match="gemini-3.8-flash"):
        Settings.from_env({"GEMINI_MODEL": "gemini-2.5-pro"})


def test_batch_path_disabled_until_spike_passes():
    with pytest.raises(ConfigError, match="spike"):
        Settings.from_env({"CORPUS_API_PATH": "batch"})


def test_invalid_policy_flags_rejected():
    with pytest.raises(ConfigError):
        Settings.from_env({"SCORE_FLOOR_MODE": "bogus"})
    with pytest.raises(ConfigError):
        Settings.from_env({"BAND_ROUNDING_RULE": "bogus"})


def test_database_is_created_in_a_missing_data_dir(tmp_path):
    make_session_factory(f"sqlite:///{tmp_path / 'not' / 'yet' / 'app.db'}")
    assert (tmp_path / "not" / "yet" / "app.db").exists()
