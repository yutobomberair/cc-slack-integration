"""Slack から利用枠を聞く（`usage`）のテスト。

通知は届くのに自分からは聞けない、という状態を解消するための機能。スマホから
確認できることが目的なので、**Claude を起動しないこと**と**claude-usage が無くても
ブリッジが壊れないこと**を重点的に見る。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import usage  # noqa: E402


# --------------------------------------------------------------------------
# ディレクティブの解釈
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["usage", "USAGE", "/usage", "使用量", "残量", "利用量"])
def test_directive_without_argument(text):
    # コロン無しの単独形を受ける（ls と同じ。一番よく打つ形なので強制しない）
    assert usage.parse_directive(text) == ""


@pytest.mark.parametrize(
    "text",
    ["usage status", "usage: status", "usage：status", "/usage: status", "使用量: status"],
)
def test_directive_with_argument(text):
    assert usage.parse_directive(text) == "status"


def test_plain_request_is_not_a_directive():
    assert usage.parse_directive("usage の仕組みを説明して") == "の仕組みを説明して"
    assert usage.parse_directive("利用枠について調べて") is None


def test_directive_only_matches_the_first_line():
    # 本文中の usage に反応すると、この機能自体を相談できなくなる
    assert usage.parse_directive("この仕組みについて\nusage の挙動を教えて") is None


def test_empty_prompt_is_not_a_directive():
    assert usage.parse_directive("") is None
    assert usage.parse_directive("   ") is None


# --------------------------------------------------------------------------
# claude-usage が無くても壊れない（依存は任意）
# --------------------------------------------------------------------------

def test_missing_claude_usage_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_USAGE_PATH", str(tmp_path / "nowhere"))
    # 一度読み込むと sys.modules に残るので、未導入の状態を作り直す
    for name in [n for n in sys.modules if n.startswith("claude_usage")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(
        sys, "path", [p for p in sys.path if "claude-usage" not in p]
    )

    text = usage.report("summary")
    assert "取得できません" in text
    assert "CLAUDE_USAGE_PATH" in text      # 直し方を伝える


def test_project_path_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_USAGE_PATH", str(tmp_path))
    assert usage.project_path() == tmp_path


def test_default_path_is_the_sibling_project():
    # 同じリポジトリの隣にある前提
    assert usage.DEFAULT_PATH.name == "claude-usage"


# --------------------------------------------------------------------------
# 返信の整形
# --------------------------------------------------------------------------

def test_output_is_wrapped_in_a_code_block(monkeypatch):
    class FakeApi:
        @staticmethod
        def report(command):
            return "5h 7%\n7d 8%"

    monkeypatch.setattr(usage, "_api", lambda: FakeApi)
    text = usage.report("summary")
    # バーや桁揃えが崩れると読めなくなるのでコードブロックで囲む
    assert text.startswith("```")
    assert text.endswith("```")
    assert "5h 7%" in text


def test_empty_command_defaults_to_summary(monkeypatch):
    seen = {}

    class FakeApi:
        @staticmethod
        def report(command):
            seen["command"] = command
            return "x"

    monkeypatch.setattr(usage, "_api", lambda: FakeApi)
    usage.report("")
    assert seen["command"] == "summary"


def test_failure_in_claude_usage_does_not_raise(monkeypatch):
    class FakeApi:
        @staticmethod
        def report(command):
            raise RuntimeError("壊れた")

    monkeypatch.setattr(usage, "_api", lambda: FakeApi)
    text = usage.report("summary")
    assert "失敗しました" in text          # ブリッジは落とさない
