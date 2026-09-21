import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import git_ops, task_mode  # noqa: E402


# --------------------------------------------------------------------------
# 実行モードの判定（二重の鍵）
# --------------------------------------------------------------------------

def test_plain_request_stays_in_investigate():
    mode = task_mode.resolve("構成を調査して", "investigate", allow_implement=True)
    assert mode.profile_key == "investigate"
    assert mode.is_implement is False
    assert mode.denied is False


@pytest.mark.parametrize(
    "prefix",
    ["実装:", "実装：", "実装", "/impl", "/implement", "impl:", "IMPL:"],
)
def test_keywords_escalate_to_implement(prefix):
    mode = task_mode.resolve(f"{prefix} README を直して", "investigate", allow_implement=True)
    assert mode.is_implement
    assert mode.prompt == "README を直して"


def test_keyword_is_stripped_from_prompt_including_newline():
    mode = task_mode.resolve("実装:\nREADME を直して", "investigate", allow_implement=True)
    assert mode.prompt == "README を直して"


def test_keyword_without_project_permission_is_denied():
    mode = task_mode.resolve("実装: README を直して", "investigate", allow_implement=False)
    assert mode.denied is True
    assert mode.is_implement is False


def test_keyword_mid_text_does_not_escalate():
    # 本文中の「実装」に反応してはいけない
    mode = task_mode.resolve(
        "この機能の実装: について調査して", "investigate", allow_implement=True
    )
    assert mode.is_implement is False
    assert mode.prompt.startswith("この機能の")


def test_denied_prompt_is_not_consumed():
    prompt = "実装: README を直して"
    mode = task_mode.resolve(prompt, "investigate", allow_implement=False)
    assert mode.prompt == prompt


# --------------------------------------------------------------------------
# git 操作
# --------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=path, capture_output=True, check=True,
        )

    git("init", "-q", "-b", "main")
    (path / "NOTE.md").write_text("# Note\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")
    return path


def test_is_repo_detects_git(repo, tmp_path):
    assert git_ops.is_repo(repo) is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_ops.is_repo(plain) is False


def test_porcelain_parses_paths_without_truncation(repo):
    # 出力全体を strip すると1行目のパスが1文字欠ける、という退行を防ぐ
    (repo / "NOTE.md").write_text("# Note\nchanged\n", encoding="utf-8")
    entries = git_ops._porcelain(repo)
    assert "NOTE.md" in entries


def test_changed_since_detects_only_new_changes(repo):
    (repo / "PRE.md").write_text("作業中\n", encoding="utf-8")  # 実行前から dirty
    before = git_ops.snapshot(repo)

    (repo / "NEW.md").write_text("今回の変更\n", encoding="utf-8")
    changed, preexisting = git_ops.changed_since(repo, before)

    assert changed == ["NEW.md"]
    assert "PRE.md" in preexisting


def test_commit_leaves_preexisting_dirty_files_alone(repo):
    (repo / "PRE.md").write_text("作業中\n", encoding="utf-8")
    before = git_ops.snapshot(repo)
    (repo / "NEW.md").write_text("今回の変更\n", encoding="utf-8")

    changed, _ = git_ops.changed_since(repo, before)
    result = git_ops.commit_changes(repo, changed, "test commit")

    assert result.committed is True
    assert result.files == ["NEW.md"]
    # 作業中のファイルは未コミットのまま残っている
    assert "PRE.md" in git_ops._porcelain(repo)


def test_commit_with_no_paths_does_nothing(repo):
    assert git_ops.commit_changes(repo, [], "noop").committed is False


def test_commit_records_stat_and_diff(repo):
    before = git_ops.snapshot(repo)
    (repo / "NOTE.md").write_text("# Note\n追記\n", encoding="utf-8")
    changed, _ = git_ops.changed_since(repo, before)
    result = git_ops.commit_changes(repo, changed, "追記した")
    assert "NOTE.md" in result.stat
    assert "追記" in result.diff


def test_current_branch(repo):
    assert git_ops.current_branch(repo) == "main"


def test_commit_message_keeps_subject_and_body():
    message = git_ops.build_commit_message("propose", "README を直して\n\n詳細な指示")
    assert message.startswith("README を直して")
    assert "詳細な指示" in message
    assert "Co-Authored-By: Claude Opus 5" in message


def test_long_first_line_is_truncated_in_subject():
    message = git_ops.build_commit_message("propose", "あ" * 200)
    assert len(message.splitlines()[0]) <= 60
