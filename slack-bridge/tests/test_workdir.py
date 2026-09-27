"""スレッドごとの作業階層（`cd:`）のテスト。

Claude Code は起動時の cwd から `.claude/` を探すので、プロジェクト内に開発ルールが
複数階層あると、どこで起動するかで読まれる CLAUDE.md と skills が変わる。それを
選べるようにするのがこの機能。

`resolve` はプロジェクト配下に限る。ここに Slack からの入力がそのまま通ると
`config/projects.yaml` による範囲指定の歯止めが効かなくなるので、境界のテストを厚くする。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import workdir  # noqa: E402
from app.paths import relative_prefix  # noqa: E402


@pytest.fixture
def project(tmp_path):
    """propose と同じ形。直下と movie に .claude を持つ。"""
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    (tmp_path / ".claude" / "CLAUDE.md").write_text("ルート", encoding="utf-8")
    (tmp_path / "movie" / ".claude").mkdir(parents=True)
    (tmp_path / "movie" / ".claude" / "CLAUDE.md").write_text("movie", encoding="utf-8")
    (tmp_path / "metting").mkdir()
    return tmp_path


# --------------------------------------------------------------------------
# ディレクティブの解釈
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text", ["cd: movie", "cd：movie", "dir: movie", "/cd: movie", "階層: movie", "移動: movie"]
)
def test_directive_is_recognised(text):
    assert workdir.parse_directive(text) == "movie"


def test_directive_without_argument_is_empty_string():
    # 空文字は「今どこ？」の意味。None（指定なし）と区別する
    assert workdir.parse_directive("cd:") == ""


def test_plain_request_is_not_a_directive():
    assert workdir.parse_directive("movie の構成を教えて") is None


def test_directive_only_matches_the_first_line():
    # 本文中の cd: に反応すると、この機能自体を相談できなくなる
    assert workdir.parse_directive("この仕組みについて\ncd: の挙動を教えて") is None


# --------------------------------------------------------------------------
# 解決（プロジェクト配下に限る）
# --------------------------------------------------------------------------

def test_resolves_subdirectory(project):
    assert workdir.resolve(project, "movie") == (project / "movie").resolve()


def test_resolves_nested_subdirectory(project):
    (project / "movie" / "blend").mkdir()
    assert workdir.resolve(project, "movie/blend") == (project / "movie" / "blend").resolve()


def test_backslashes_are_accepted(project):
    # Windows から手で打つと区切りが \ になりがち
    (project / "movie" / "blend").mkdir()
    assert workdir.resolve(project, "movie\\blend") == (project / "movie" / "blend").resolve()


@pytest.mark.parametrize("token", [".", "/", "root", "ルート", "直下", "", "  "])
def test_root_tokens_return_the_project_root(project, token):
    assert workdir.resolve(project, token) == project.resolve()


def test_surrounding_quotes_are_stripped(project):
    assert workdir.resolve(project, "`movie`") == (project / "movie").resolve()


# --------------------------------------------------------------------------
# 境界（ここが破られると設定による範囲指定の意味が無くなる）
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["..", "../..", "../other", "movie/../..", "movie/../../etc"])
def test_parent_traversal_is_refused(project, bad):
    with pytest.raises(workdir.WorkdirError):
        workdir.resolve(project, bad)


@pytest.mark.parametrize("bad", ["C:/Windows", "C:\\Windows", "/etc/passwd", "//server/share"])
def test_absolute_paths_are_refused(project, bad):
    with pytest.raises(workdir.WorkdirError):
        workdir.resolve(project, bad)


def test_missing_directory_is_refused(project):
    with pytest.raises(workdir.WorkdirError):
        workdir.resolve(project, "nope")


def test_file_is_not_a_directory(project):
    (project / "PLAN.md").write_text("x", encoding="utf-8")
    with pytest.raises(workdir.WorkdirError):
        workdir.resolve(project, "PLAN.md")


def test_error_message_is_actionable(project):
    with pytest.raises(workdir.WorkdirError) as excinfo:
        workdir.resolve(project, "../escape")
    assert "外へは移動できません" in str(excinfo.value)


# --------------------------------------------------------------------------
# 表示
# --------------------------------------------------------------------------

def test_label_is_empty_at_the_project_root(project):
    assert workdir.relative_label(project, project) == ""


def test_label_uses_posix_separators(project):
    (project / "movie" / "blend").mkdir()
    assert workdir.relative_label(project, project / "movie" / "blend") == "movie/blend"


def test_candidates_lists_directories_with_dev_rules(project):
    # 開発ルールがある階層こそが移動先として意味を持つ
    assert workdir.candidates(project) == ["movie"]


def test_candidates_is_empty_without_nested_rules(tmp_path):
    (tmp_path / ".claude").mkdir()
    assert workdir.candidates(tmp_path) == []


# --------------------------------------------------------------------------
# プロジェクト直下への相対接頭辞（_share/ と _inbox/ の伝え方）
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label,prefix", [("", ""), ("movie", "../"), ("movie/blend", "../../"), ("a/b/c", "../../../")]
)
def test_relative_prefix(label, prefix):
    assert relative_prefix(label) == prefix


# --------------------------------------------------------------------------
# 記録
# --------------------------------------------------------------------------

def test_store_roundtrip(tmp_path):
    store = workdir.WorkdirStore(tmp_path / "w.json")
    store.set("C1", "1.0", "proj", "movie")
    assert store.get("C1", "1.0") == "movie"


def test_unset_thread_is_the_project_root(tmp_path):
    assert workdir.WorkdirStore(tmp_path / "w.json").get("C1", "1.0") == ""


def test_store_survives_restart(tmp_path):
    path = tmp_path / "w.json"
    workdir.WorkdirStore(path).set("C1", "1.0", "proj", "movie")
    assert workdir.WorkdirStore(path).get("C1", "1.0") == "movie"


def test_returning_to_root_clears_the_record(tmp_path):
    # 既定と同じ状態を記録に残す意味が無い
    store = workdir.WorkdirStore(tmp_path / "w.json")
    store.set("C1", "1.0", "proj", "movie")
    store.set("C1", "1.0", "proj", "")
    assert store.get("C1", "1.0") == ""


def test_threads_are_independent(tmp_path):
    store = workdir.WorkdirStore(tmp_path / "w.json")
    store.set("C1", "1.0", "proj", "movie")
    assert store.get("C1", "2.0") == ""


def test_store_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "w.json"
    path.write_text("{ broken", encoding="utf-8")
    assert workdir.WorkdirStore(path).get("C1", "1.0") == ""
