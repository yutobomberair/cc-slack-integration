import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import formatting, git_ops  # noqa: E402


# --------------------------------------------------------------------------
# リモート URL の解釈
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url,owner,repo",
    [
        ("https://github.com/yutobomberair/navigation-core.git", "yutobomberair", "navigation-core"),
        ("https://github.com/yutobomberair/navigation-core", "yutobomberair", "navigation-core"),
        ("git@github.com:yutobomberair/Pet-Camera.git", "yutobomberair", "Pet-Camera"),
        ("ssh://git@github.com/owner/repo.git", "owner", "repo"),
    ],
)
def test_remote_url_forms_are_parsed(tmp_path, url, owner, repo):
    path = tmp_path / "r"
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", url], cwd=path, check=True)

    info = git_ops.remote_info(path)
    assert info is not None
    assert (info.owner, info.repo) == (owner, repo)
    assert info.is_github


def test_no_remote_returns_none(tmp_path):
    path = tmp_path / "r"
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    assert git_ops.remote_info(path) is None


def test_compare_url_points_at_pr_form():
    info = git_ops.RemoteInfo("u", "github.com", "owner", "repo")
    url = info.compare_url("main", "claude/20260920-abc123")
    assert url == (
        "https://github.com/owner/repo/compare/main...claude/20260920-abc123?expand=1"
    )


def test_non_github_host_has_no_links():
    info = git_ops.RemoteInfo("u", "gitlab.com", "owner", "repo")
    assert info.is_github is False
    assert info.compare_url("main", "x") is None
    assert info.branch_url("x") is None


# --------------------------------------------------------------------------
# ブランチ名
# --------------------------------------------------------------------------

def test_thread_branch_is_deterministic():
    a = git_ops.thread_branch_name("1758348000.123456")
    b = git_ops.thread_branch_name("1758348000.123456")
    assert a == b
    assert a.startswith("claude/")


def test_different_threads_get_different_branches():
    a = git_ops.thread_branch_name("1758348000.123456")
    b = git_ops.thread_branch_name("1758348001.000000")
    assert a != b


def test_branch_name_is_git_safe():
    name = git_ops.thread_branch_name("1758348000.123456")
    assert " " not in name
    assert ".." not in name
    assert not name.endswith(".lock")


def test_malformed_thread_ts_does_not_crash():
    assert git_ops.thread_branch_name("not-a-timestamp").startswith("claude/unknown-")


# --------------------------------------------------------------------------
# push の歯止め
# --------------------------------------------------------------------------

@pytest.fixture
def repo_with_remote(tmp_path):
    """bare リポジトリを origin にした実リポジトリ。ネットワーク不要で push を試せる。"""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)

    work = tmp_path / "work"
    work.mkdir()

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=work, capture_output=True, check=True,
        )

    git("init", "-q", "-b", "main")
    (work / "NOTE.md").write_text("# Note\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")
    git("remote", "add", "origin", str(bare))
    git("push", "-q", "-u", "origin", "main")
    return work


def test_push_creates_remote_branch(repo_with_remote):
    (repo_with_remote / "NOTE.md").write_text("# Note\nchange\n", encoding="utf-8")
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-aqm", "change"],
        cwd=repo_with_remote, check=True,
    )

    result = git_ops.push_to_branch(repo_with_remote, "claude/20260920-abc123")
    assert result.pushed is True
    assert result.branch == "claude/20260920-abc123"

    remote_branches = subprocess.run(
        ["git", "branch", "-r"], cwd=repo_with_remote,
        capture_output=True, text=True, check=True,
    ).stdout
    # fetch しないと remote-tracking には出ないので、bare 側を直接見る
    bare = Path(
        subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo_with_remote,
                       capture_output=True, text=True, check=True).stdout.strip()
    )
    heads = subprocess.run(["git", "branch"], cwd=bare, capture_output=True, text=True).stdout
    assert "claude/20260920-abc123" in heads


def test_push_refuses_default_branch(repo_with_remote):
    result = git_ops.push_to_branch(repo_with_remote, "main")
    assert result.pushed is False
    assert "main" in result.error


def test_push_without_remote_is_reported(tmp_path):
    path = tmp_path / "r"
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    result = git_ops.push_to_branch(path, "claude/x")
    assert result.pushed is False
    assert "リモート" in result.error


def test_push_does_not_switch_local_branch(repo_with_remote):
    before = git_ops.current_branch(repo_with_remote)
    git_ops.push_to_branch(repo_with_remote, "claude/20260920-abc123")
    assert git_ops.current_branch(repo_with_remote) == before


# --------------------------------------------------------------------------
# 報告文
# --------------------------------------------------------------------------

def test_push_report_includes_pr_link():
    text = formatting.push_report(
        "claude/x", ["aaa 変更"], "https://github.com/o/r/compare/main...claude/x?expand=1", None
    )
    assert "Pull Request を作成する" in text
    assert "compare/main...claude/x" in text
    assert "merge は行いません" in text


def test_push_report_warns_about_extra_commits():
    commits = [f"sha{i} コミット{i}" for i in range(9)]
    text = formatting.push_report("claude/x", commits, None, None, own_commit_count=1)
    assert "8 件" in text


def test_push_report_stays_quiet_when_only_own_commit():
    text = formatting.push_report("claude/x", ["aaa 変更"], None, None, own_commit_count=1)
    assert "ローカルに残っていた" not in text


def test_push_skipped_and_failed_notes():
    assert "リモートが設定されていません" in formatting.push_skipped_note(
        "リモートが設定されていません"
    )
    assert "push に失敗" in formatting.push_failed_note("claude/x", "denied")
