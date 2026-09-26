"""``_share/`` 送信待ち行列のテスト（``app/share_queue.py``）。

「そこにある = 未送信」という状態の持ち方が要点。以前は差分で判定していたため、
送信に失敗したファイルは内容が変わらない限り二度と対象にならず、スコープ不足で
置かれたファイルが永久に届かなかった。その回帰を防ぐテストが中心。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import artifacts, share_queue  # noqa: E402


def write(root: Path, rel: str, body: str = "x") -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def items(*paths):
    return [artifacts.Artifact(path=p, size=10) for p in paths]


def age(path, days):
    """ファイルの更新時刻を days 日前にする。"""
    import os
    import time

    past = time.time() - days * 86400
    os.utime(path, (past, past))


def test_list_share_files_walks_nested_directories(tmp_path):
    write(tmp_path, "_share/a.md")
    write(tmp_path, "_share/nested/b.csv")
    assert share_queue.list_share_files(tmp_path) == [
        "_share/a.md",
        "_share/nested/b.csv",
    ]


def test_list_share_files_without_directory(tmp_path):
    assert share_queue.list_share_files(tmp_path) == []


@pytest.mark.parametrize(
    "path,shared",
    [("_share/a.md", True), ("_share", True), ("_shared/a.md", False), ("docs/a.md", False)],
)
def test_is_shared_path(path, shared):
    assert share_queue.is_shared_path(path) is shared


def test_split_shared_separates_transport_from_content():
    shared, rest = share_queue.split_shared(["_share/out.pdf", "src/app.py", "README.md"])
    assert shared == ["_share/out.pdf"]
    assert rest == ["src/app.py", "README.md"]


def test_share_dir_note_tells_claude_where_to_put_files():
    note = share_queue.share_dir_note()
    assert share_queue.SHARE_DIR in note
    assert note.endswith("\n\n")


def test_auto_share_report_flags_unsent_files():
    targets = [
        artifacts.Artifact(path="_share/ok.pdf", size=10),
        artifacts.Artifact(path="_share/huge.zip", size=artifacts.MAX_UPLOAD_BYTES + 1),
    ]
    report = share_queue.auto_share_report(["_share/ok.pdf"], targets)
    assert "1 件を添付しました" in report
    assert "huge.zip" in report


def test_auto_share_report_is_empty_without_files():
    assert share_queue.auto_share_report([], []) == ""

def test_files_in_share_dir_are_pending(tmp_path):
    write(tmp_path, "_share/a.md", "中身")
    assert share_queue.list_share_files(tmp_path) == ["_share/a.md"]


def test_sent_subdirectory_is_not_pending(tmp_path):
    write(tmp_path, "_share/sent/done.md", "送信済み")
    write(tmp_path, "_share/pending.md", "未送信")
    assert share_queue.list_share_files(tmp_path) == ["_share/pending.md"]


def test_archive_moves_sent_files_out_of_the_queue(tmp_path):
    write(tmp_path, "_share/a.md", "中身")
    assert share_queue.archive_sent(tmp_path, ["_share/a.md"]) == []

    assert share_queue.list_share_files(tmp_path) == []          # もう送らない
    assert (tmp_path / "_share/sent/a.md").read_text(encoding="utf-8") == "中身"
    assert not (tmp_path / "_share/a.md").exists()


def test_archive_preserves_subdirectories(tmp_path):
    write(tmp_path, "_share/docs/a.md", "中身")
    share_queue.archive_sent(tmp_path, ["_share/docs/a.md"])
    assert (tmp_path / "_share/sent/docs/a.md").exists()


def test_archive_does_not_overwrite_previous_versions(tmp_path):
    write(tmp_path, "_share/a.md", "1回目")
    share_queue.archive_sent(tmp_path, ["_share/a.md"])
    write(tmp_path, "_share/a.md", "2回目")
    share_queue.archive_sent(tmp_path, ["_share/a.md"])

    assert (tmp_path / "_share/sent/a.md").read_text(encoding="utf-8") == "1回目"
    assert (tmp_path / "_share/sent/a-1.md").read_text(encoding="utf-8") == "2回目"


def test_failed_upload_stays_in_the_queue_for_retry(tmp_path):
    # 送れなかったものは退避しないので、次の実行でもう一度対象になる
    write(tmp_path, "_share/ok.md", "送れた")
    write(tmp_path, "_share/ng.md", "送れなかった")
    share_queue.archive_sent(tmp_path, ["_share/ok.md"])

    assert share_queue.list_share_files(tmp_path) == ["_share/ng.md"]


def test_same_content_placed_again_is_sent_again(tmp_path):
    # 一度送ったあとに同じ内容を置き直したら、それは新しい共有の意思表示
    write(tmp_path, "_share/a.md", "中身")
    share_queue.archive_sent(tmp_path, ["_share/a.md"])
    write(tmp_path, "_share/a.md", "中身")

    assert share_queue.list_share_files(tmp_path) == ["_share/a.md"]


def test_queue_state_needs_no_stored_record(tmp_path):
    # 状態はディレクトリだけで決まる。state/artifacts.json を失っても重複しない。
    write(tmp_path, "_share/a.md", "中身")
    share_queue.archive_sent(tmp_path, ["_share/a.md"])
    assert share_queue.list_share_files(tmp_path) == []


def test_archive_reports_files_it_could_not_move(tmp_path):
    # 存在しないものは移せない。呼び出し側が警告できるよう返す。
    write(tmp_path, "_share/a.md", "中身")
    assert share_queue.archive_sent(tmp_path, ["_share/gone.md"]) == ["_share/gone.md"]


def test_auto_share_report_warns_about_stuck_files():
    items = [artifacts.Artifact(path="_share/a.md", size=10)]
    report = share_queue.auto_share_report(["_share/a.md"], items, stuck=["_share/a.md"])
    assert "退避できませんでした" in report
    assert "もう一度届きます" in report


def test_auto_share_report_mentions_retry_on_failure():
    targets = [artifacts.Artifact(path="_share/a.md", size=10)]
    report = share_queue.auto_share_report([], targets)  # 送れなかった
    assert "再試行" in report

def test_retention_zero_deletes_immediately(tmp_path):
    write(tmp_path, "_share/a.md", "中身")
    assert share_queue.archive_sent(tmp_path, ["_share/a.md"], retention_days=0) == []

    assert share_queue.list_share_files(tmp_path) == []
    assert not (tmp_path / "_share/sent/a.md").exists()   # 退避もされない
    assert not (tmp_path / "_share/a.md").exists()


def test_retention_keeps_recent_files(tmp_path):
    write(tmp_path, "_share/a.md", "中身")
    share_queue.archive_sent(tmp_path, ["_share/a.md"], retention_days=7)
    assert (tmp_path / "_share/sent/a.md").exists()


def test_old_archived_files_are_pruned(tmp_path):
    write(tmp_path, "_share/sent/old.md", "古い")
    age(tmp_path / "_share/sent/old.md", days=30)
    write(tmp_path, "_share/sent/new.md", "新しい")

    assert share_queue.prune_sent(tmp_path, retention_days=7) == 1
    assert not (tmp_path / "_share/sent/old.md").exists()
    assert (tmp_path / "_share/sent/new.md").exists()


def test_archiving_also_prunes(tmp_path):
    write(tmp_path, "_share/sent/old.md", "古い")
    age(tmp_path / "_share/sent/old.md", days=30)
    write(tmp_path, "_share/a.md", "中身")

    share_queue.archive_sent(tmp_path, ["_share/a.md"], retention_days=7)
    assert not (tmp_path / "_share/sent/old.md").exists()
    assert (tmp_path / "_share/sent/a.md").exists()


def test_pruning_never_touches_the_queue(tmp_path):
    # 未送信のファイルは、どれだけ古くても消さない
    write(tmp_path, "_share/pending.md", "未送信")
    age(tmp_path / "_share/pending.md", days=365)

    share_queue.prune_sent(tmp_path, retention_days=7)
    assert share_queue.list_share_files(tmp_path) == ["_share/pending.md"]


def test_prune_without_sent_directory(tmp_path):
    assert share_queue.prune_sent(tmp_path, retention_days=7) == 0
