import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import PermissionProfile, Project, Runtime, Settings  # noqa: E402
from app.project_router import route, unknown_channel_message  # noqa: E402
from app.concurrency import SeenEvents  # noqa: E402
from app.slack_handler import strip_mentions  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    cwd_a = tmp_path / "navigation-core"
    cwd_b = tmp_path / "propose"
    cwd_a.mkdir()
    cwd_b.mkdir()
    return Settings(
        bot_token="xoxb-test",
        app_token="xapp-test",
        projects={
            "progress-navi": Project(
                key="progress-navi",
                name="Progress Navi",
                channel_id="C111",
                working_directory=cwd_a,
                profile="investigate",
            ),
            "propose": Project(
                key="propose",
                name="Propose",
                channel_id="C222",
                working_directory=cwd_b,
                profile="investigate",
            ),
        },
        profiles={"investigate": PermissionProfile(key="investigate")},
        runtime=Runtime(),
    )


def test_known_channel_routes_to_its_project(settings):
    assert route(settings, "C111").project.key == "progress-navi"
    assert route(settings, "C222").project.name == "Propose"


def test_channels_map_to_different_working_directories(settings):
    a = route(settings, "C111").project.working_directory
    b = route(settings, "C222").project.working_directory
    assert a != b


def test_unknown_channel_is_not_routed(settings):
    result = route(settings, "C999")
    assert not result.is_known
    assert result.project is None
    assert "C999" in unknown_channel_message("C999")

def test_strip_mentions_removes_bot_mention():
    assert strip_mentions("<@U12345ABC> 構成を調査して") == "構成を調査して"
    assert strip_mentions("<@U1> <@W2> やって") == "やって"


def test_strip_mentions_keeps_body_when_mention_is_inline():
    assert strip_mentions("お願い <@U1> します") == "お願い します"


def test_strip_mentions_preserves_multiline_structure():
    text = "<@U1>\n構成を調査して。\n\n・現状\n・推奨構成"
    assert strip_mentions(text) == "構成を調査して。\n\n・現状\n・推奨構成"


def test_seen_events_deduplicates():
    seen = SeenEvents()
    assert seen.add_if_new("Ev1") is True
    assert seen.add_if_new("Ev1") is False
    assert seen.add_if_new("Ev2") is True


def test_seen_events_evicts_oldest_beyond_capacity():
    seen = SeenEvents(capacity=3)
    for i in range(5):
        assert seen.add_if_new(f"Ev{i}") is True
    assert seen.add_if_new("Ev0") is True   # 追い出し済みなので新規扱い
    assert seen.add_if_new("Ev4") is False  # まだ保持されている
