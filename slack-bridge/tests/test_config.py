import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ConfigError, load_settings  # noqa: E402

BASE = """
projects:
  alpha:
    channel_id: "{alpha_channel}"
    name: "Alpha"
    working_directory: "{alpha_dir}"
    profile: investigate
  beta:
    channel_id: "{beta_channel}"
    name: "Beta"
    working_directory: "{beta_dir}"
    profile: {beta_profile}
permission_profiles:
  investigate:
    tools: ["Read", "Grep"]
    max_budget_usd: 3.0
runtime:
  timeout_seconds: 60
  max_workers: 1
"""


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")


def write_config(tmp_path, **overrides):
    a = tmp_path / "alpha"
    b = tmp_path / "beta"
    a.mkdir(exist_ok=True)
    b.mkdir(exist_ok=True)
    values = {
        "alpha_channel": "C111",
        "beta_channel": "C222",
        "alpha_dir": a.as_posix(),
        "beta_dir": b.as_posix(),
        "beta_profile": "investigate",
    }
    values.update(overrides)
    path = tmp_path / "projects.yaml"
    path.write_text(BASE.format(**values), encoding="utf-8")
    return path


def test_valid_config_loads(tmp_path):
    settings = load_settings(write_config(tmp_path))
    assert set(settings.projects) == {"alpha", "beta"}
    assert settings.project_for_channel("C222").name == "Beta"
    assert settings.project_for_channel("C999") is None
    assert settings.runtime.timeout_seconds == 60


def test_placeholder_channel_id_is_rejected(tmp_path):
    path = write_config(tmp_path, alpha_channel="C_REPLACE_ME_ALPHA")
    with pytest.raises(ConfigError, match="プレースホルダ"):
        load_settings(path)


def test_duplicate_channel_id_is_rejected(tmp_path):
    path = write_config(tmp_path, beta_channel="C111")
    with pytest.raises(ConfigError, match="重複"):
        load_settings(path)


def test_missing_working_directory_is_rejected(tmp_path):
    path = write_config(tmp_path, beta_dir=(tmp_path / "nope").as_posix())
    with pytest.raises(ConfigError, match="存在しません"):
        load_settings(path)


def test_unknown_profile_is_rejected(tmp_path):
    path = write_config(tmp_path, beta_profile="implement")
    with pytest.raises(ConfigError, match="未定義の profile"):
        load_settings(path)


def test_missing_app_token_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_APP_TOKEN")
    monkeypatch.setenv("SLACK_APP_TOKEN", "")
    with pytest.raises(ConfigError, match="SLACK_APP_TOKEN"):
        load_settings(write_config(tmp_path))


def test_non_xapp_app_token_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_APP_TOKEN", "xoxb-wrong")
    with pytest.raises(ConfigError, match="xapp-"):
        load_settings(write_config(tmp_path))
