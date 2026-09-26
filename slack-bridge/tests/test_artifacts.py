import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import artifacts  # noqa: E402


def write(root: Path, rel: str, body: str = "x") -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


# --------------------------------------------------------------------------
# git 管理外プロジェクトでの出力検出
# --------------------------------------------------------------------------

def test_scan_detects_new_and_modified_files(tmp_path):
    write(tmp_path, "keep.md", "そのまま")
    before = artifacts.scan(tmp_path)

    write(tmp_path, "report.md", "新規")
    write(tmp_path, "keep.md", "書き換えた")

    changed = artifacts.changed_since(before, artifacts.scan(tmp_path))
    assert changed == ["keep.md", "report.md"]


def test_scan_skips_noise_directories(tmp_path):
    write(tmp_path, "venv/lib/thing.py")
    write(tmp_path, "node_modules/pkg/index.js")
    write(tmp_path, ".git/config")
    write(tmp_path, "__pycache__/a.pyc")
    write(tmp_path, "docs/report.md")

    assert sorted(artifacts.scan(tmp_path)) == ["docs/report.md"]


def test_unchanged_tree_produces_no_output(tmp_path):
    write(tmp_path, "a.md")
    before = artifacts.scan(tmp_path)
    assert artifacts.changed_since(before, artifacts.scan(tmp_path)) == []


def test_scan_uses_posix_separators(tmp_path):
    write(tmp_path, "docs/nested/report.md")
    assert "docs/nested/report.md" in artifacts.scan(tmp_path)


# --------------------------------------------------------------------------
# 「共有:」の解釈
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    ["共有: 1", "共有：1", "share: 1", "/share: 1", "送って: 1", "ちょうだい: 1"],
)
def test_share_directive_is_recognised(text):
    assert artifacts.parse_share_request(text) == "1"


def test_share_directive_only_matches_first_line():
    # 本文中の「共有:」に反応すると、共有機能そのものを相談できなくなる
    assert artifacts.parse_share_request("この仕組みについて\n共有: の挙動を教えて") is None


def test_plain_request_is_not_a_share():
    assert artifacts.parse_share_request("README を直して") is None


def test_share_directive_keeps_following_lines():
    assert artifacts.parse_share_request("共有: 1\n2") == "1\n2"


# --------------------------------------------------------------------------
# 対象の選択
# --------------------------------------------------------------------------

def items(*paths):
    return [artifacts.Artifact(path=p, size=10) for p in paths]


def test_select_by_number():
    chosen, unresolved = artifacts.select(items("a.md", "b.csv", "c.png"), "2")
    assert [i.path for i in chosen] == ["b.csv"]
    assert unresolved == []


@pytest.mark.parametrize("argument", ["1,3", "1, 3", "1 3", "1、3"])
def test_select_accepts_various_separators(argument):
    chosen, _ = artifacts.select(items("a.md", "b.csv", "c.png"), argument)
    assert [i.path for i in chosen] == ["a.md", "c.png"]


@pytest.mark.parametrize("argument", ["all", "全部", "すべて", ""])
def test_select_all(argument):
    chosen, unresolved = artifacts.select(items("a.md", "b.csv"), argument)
    assert len(chosen) == 2
    assert unresolved == []


def test_select_by_partial_name():
    chosen, _ = artifacts.select(items("docs/report.md", "data/summary.csv"), "report")
    assert [i.path for i in chosen] == ["docs/report.md"]


def test_select_reports_unresolved_tokens():
    chosen, unresolved = artifacts.select(items("a.md"), "9,存在しない")
    assert chosen == []
    assert unresolved == ["9", "存在しない"]


def test_select_deduplicates():
    # 番号と名前で同じものを指しても1回だけ
    chosen, _ = artifacts.select(items("docs/report.md", "b.csv"), "1,report")
    assert [i.path for i in chosen] == ["docs/report.md"]


def test_select_keeps_resolved_when_some_tokens_fail():
    chosen, unresolved = artifacts.select(items("a.md", "b.csv"), "1,99")
    assert [i.path for i in chosen] == ["a.md"]
    assert unresolved == ["99"]


# --------------------------------------------------------------------------
# 一覧とサイズ
# --------------------------------------------------------------------------

def test_describe_marks_missing_files(tmp_path):
    write(tmp_path, "there.md", "abc")
    described = artifacts.describe(tmp_path, ["there.md", "gone.md"])
    assert described[0].size == 3
    assert described[0].missing is False
    assert described[1].missing is True


def test_listing_includes_numbers_and_hint():
    text = artifacts.listing_message(items("docs/report.md", "data/summary.csv"))
    assert "`1` `docs/report.md`" in text
    assert "`2` `data/summary.csv`" in text
    assert "共有: 1" in text


def test_listing_is_empty_without_files():
    assert artifacts.listing_message([]) == ""


def test_listing_truncates_long_lists():
    many = items(*[f"f{i}.md" for i in range(artifacts.MAX_LISTED + 5)])
    text = artifacts.listing_message(many)
    assert "ほか 5 件" in text


def test_oversized_file_is_flagged_not_uploaded():
    big = artifacts.Artifact(path="huge.bin", size=artifacts.MAX_UPLOAD_BYTES + 1)
    assert big.too_large
    assert "大きすぎる" in artifacts.listing_message([big])


def test_human_size():
    assert artifacts.human_size(512) == "512 B"
    assert artifacts.human_size(1536) == "1.5 KB"
    assert artifacts.human_size(-1) == "不明"


# --------------------------------------------------------------------------
# アップロード
# --------------------------------------------------------------------------

class FakeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.uploaded: list[str] = []

    def files_upload_v2(self, **kwargs):
        if self.error:
            raise self.error
        self.uploaded.append(kwargs["file"])


def test_upload_sends_each_file(tmp_path):
    write(tmp_path, "a.md")
    write(tmp_path, "b.md")
    client = FakeClient()
    sent = artifacts.upload(client, "C1", "1.0", tmp_path, items("a.md", "b.md"))
    assert sent == ["a.md", "b.md"]
    assert len(client.uploaded) == 2


def test_upload_skips_oversized_and_missing(tmp_path):
    write(tmp_path, "ok.md")
    client = FakeClient()
    targets = [
        artifacts.Artifact(path="ok.md", size=10),
        artifacts.Artifact(path="huge.bin", size=artifacts.MAX_UPLOAD_BYTES + 1),
        artifacts.Artifact(path="gone.md", size=-1, missing=True),
    ]
    sent = artifacts.upload(client, "C1", "1.0", tmp_path, targets)
    assert sent == ["ok.md"]


def test_missing_scope_raises_actionable_error(tmp_path):
    write(tmp_path, "a.md")
    client = FakeClient(error=Exception("missing_scope: files:write"))
    with pytest.raises(artifacts.UploadError) as excinfo:
        artifacts.upload(client, "C1", "1.0", tmp_path, items("a.md"))
    assert "files:write" in str(excinfo.value)


def test_other_upload_errors_do_not_stop_the_rest(tmp_path):
    write(tmp_path, "a.md")
    client = FakeClient(error=Exception("rate limited"))
    sent = artifacts.upload(client, "C1", "1.0", tmp_path, items("a.md"))
    assert sent == []  # 例外は投げず、送れなかったことだけ返す


def test_upload_report_mentions_skipped_files():
    targets = [
        artifacts.Artifact(path="ok.md", size=10),
        artifacts.Artifact(path="huge.bin", size=artifacts.MAX_UPLOAD_BYTES + 1),
    ]
    report = artifacts.upload_report(["ok.md"], targets)
    assert "1 件を共有しました" in report
    assert "huge.bin" in report


# --------------------------------------------------------------------------
# 記録
# --------------------------------------------------------------------------

def test_store_roundtrip(tmp_path):
    store = artifacts.ArtifactStore(tmp_path / "artifacts.json")
    store.record("C1", "1.0", "proj", ["a.md", "b.md"])
    assert store.get("C1", "1.0") == ["a.md", "b.md"]
    assert store.get("C1", "9.9") == []


def test_store_survives_restart(tmp_path):
    path = tmp_path / "artifacts.json"
    artifacts.ArtifactStore(path).record("C1", "1.0", "proj", ["a.md"])
    assert artifacts.ArtifactStore(path).get("C1", "1.0") == ["a.md"]


def test_store_replaces_previous_listing(tmp_path):
    store = artifacts.ArtifactStore(tmp_path / "artifacts.json")
    store.record("C1", "1.0", "proj", ["old.md"])
    store.record("C1", "1.0", "proj", ["new.md"])
    assert store.get("C1", "1.0") == ["new.md"]


def test_store_evicts_oldest_entries(tmp_path):
    store = artifacts.ArtifactStore(tmp_path / "artifacts.json", max_entries=2)
    for i in range(4):
        store.record("C1", f"{i}.0", "proj", [f"{i}.md"])
    remaining = [t for t in range(4) if store.get("C1", f"{t}.0")]
    assert len(remaining) == 2


def test_store_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "artifacts.json"
    path.write_text("{ broken", encoding="utf-8")
    assert artifacts.ArtifactStore(path).get("C1", "1.0") == []

# --------------------------------------------------------------------------
# _share/ — Claude からの明示的な共有
# --------------------------------------------------------------------------

def test_share_dir_is_excluded_from_normal_scan(tmp_path):
    write(tmp_path, "docs/report.md")
    write(tmp_path, "_share/deliverable.pdf")
    # 通常の走査からは外れる（自動添付の経路で別に扱うため二重に出さない）
    assert sorted(artifacts.scan(tmp_path)) == ["docs/report.md"]


def test_list_share_files_walks_nested_directories(tmp_path):
    write(tmp_path, "_share/a.md")
    write(tmp_path, "_share/nested/b.csv")
    assert artifacts.list_share_files(tmp_path) == [
        "_share/a.md",
        "_share/nested/b.csv",
    ]


def test_list_share_files_without_directory(tmp_path):
    assert artifacts.list_share_files(tmp_path) == []


@pytest.mark.parametrize(
    "path,shared",
    [("_share/a.md", True), ("_share", True), ("_shared/a.md", False), ("docs/a.md", False)],
)
def test_is_shared_path(path, shared):
    assert artifacts.is_shared_path(path) is shared


def test_split_shared_separates_transport_from_content():
    shared, rest = artifacts.split_shared(["_share/out.pdf", "src/app.py", "README.md"])
    assert shared == ["_share/out.pdf"]
    assert rest == ["src/app.py", "README.md"]


def test_share_dir_note_tells_claude_where_to_put_files():
    note = artifacts.share_dir_note()
    assert artifacts.SHARE_DIR in note
    assert note.endswith("\n\n")


def test_auto_share_report_flags_unsent_files():
    targets = [
        artifacts.Artifact(path="_share/ok.pdf", size=10),
        artifacts.Artifact(path="_share/huge.zip", size=artifacts.MAX_UPLOAD_BYTES + 1),
    ]
    report = artifacts.auto_share_report(["_share/ok.pdf"], targets)
    assert "1 件を添付しました" in report
    assert "huge.zip" in report


def test_auto_share_report_is_empty_without_files():
    assert artifacts.auto_share_report([], []) == ""

# --------------------------------------------------------------------------
# _share/ は送信の待ち行列
#
# 「そこにある = 未送信」。送れたら _share/sent/ へ退かす。以前は差分で判定して
# いたため、送信に失敗したファイルは内容が変わらない限り二度と対象にならず、
# スコープ不足で置かれたファイルが永久に届かなかった。その回帰を防ぐ。
# --------------------------------------------------------------------------

def test_files_in_share_dir_are_pending(tmp_path):
    write(tmp_path, "_share/a.md", "中身")
    assert artifacts.list_share_files(tmp_path) == ["_share/a.md"]


def test_sent_subdirectory_is_not_pending(tmp_path):
    write(tmp_path, "_share/sent/done.md", "送信済み")
    write(tmp_path, "_share/pending.md", "未送信")
    assert artifacts.list_share_files(tmp_path) == ["_share/pending.md"]


def test_archive_moves_sent_files_out_of_the_queue(tmp_path):
    write(tmp_path, "_share/a.md", "中身")
    assert artifacts.archive_sent(tmp_path, ["_share/a.md"]) == []

    assert artifacts.list_share_files(tmp_path) == []          # もう送らない
    assert (tmp_path / "_share/sent/a.md").read_text(encoding="utf-8") == "中身"
    assert not (tmp_path / "_share/a.md").exists()


def test_archive_preserves_subdirectories(tmp_path):
    write(tmp_path, "_share/docs/a.md", "中身")
    artifacts.archive_sent(tmp_path, ["_share/docs/a.md"])
    assert (tmp_path / "_share/sent/docs/a.md").exists()


def test_archive_does_not_overwrite_previous_versions(tmp_path):
    write(tmp_path, "_share/a.md", "1回目")
    artifacts.archive_sent(tmp_path, ["_share/a.md"])
    write(tmp_path, "_share/a.md", "2回目")
    artifacts.archive_sent(tmp_path, ["_share/a.md"])

    assert (tmp_path / "_share/sent/a.md").read_text(encoding="utf-8") == "1回目"
    assert (tmp_path / "_share/sent/a-1.md").read_text(encoding="utf-8") == "2回目"


def test_failed_upload_stays_in_the_queue_for_retry(tmp_path):
    # 送れなかったものは退避しないので、次の実行でもう一度対象になる
    write(tmp_path, "_share/ok.md", "送れた")
    write(tmp_path, "_share/ng.md", "送れなかった")
    artifacts.archive_sent(tmp_path, ["_share/ok.md"])

    assert artifacts.list_share_files(tmp_path) == ["_share/ng.md"]


def test_same_content_placed_again_is_sent_again(tmp_path):
    # 一度送ったあとに同じ内容を置き直したら、それは新しい共有の意思表示
    write(tmp_path, "_share/a.md", "中身")
    artifacts.archive_sent(tmp_path, ["_share/a.md"])
    write(tmp_path, "_share/a.md", "中身")

    assert artifacts.list_share_files(tmp_path) == ["_share/a.md"]


def test_queue_state_needs_no_stored_record(tmp_path):
    # 状態はディレクトリだけで決まる。state/artifacts.json を失っても重複しない。
    write(tmp_path, "_share/a.md", "中身")
    artifacts.archive_sent(tmp_path, ["_share/a.md"])
    assert artifacts.list_share_files(tmp_path) == []


def test_archive_reports_files_it_could_not_move(tmp_path):
    # 存在しないものは移せない。呼び出し側が警告できるよう返す。
    write(tmp_path, "_share/a.md", "中身")
    assert artifacts.archive_sent(tmp_path, ["_share/gone.md"]) == ["_share/gone.md"]


def test_auto_share_report_warns_about_stuck_files():
    items = [artifacts.Artifact(path="_share/a.md", size=10)]
    report = artifacts.auto_share_report(["_share/a.md"], items, stuck=["_share/a.md"])
    assert "退避できませんでした" in report
    assert "もう一度届きます" in report


def test_auto_share_report_mentions_retry_on_failure():
    targets = [artifacts.Artifact(path="_share/a.md", size=10)]
    report = artifacts.auto_share_report([], targets)  # 送れなかった
    assert "再試行" in report

# --------------------------------------------------------------------------
# 退避分の保持と掃除
#
# _share/sent/ を放っておくと膨らむ。一方、_share/ に直接生成されたファイルは
# コピー元が無いので即削除だと実体が消える。既定は少し残してから掃除する。
# --------------------------------------------------------------------------

def age(path, days):
    """ファイルの更新時刻を days 日前にする。"""
    import os, time
    past = time.time() - days * 86400
    os.utime(path, (past, past))


def test_retention_zero_deletes_immediately(tmp_path):
    write(tmp_path, "_share/a.md", "中身")
    assert artifacts.archive_sent(tmp_path, ["_share/a.md"], retention_days=0) == []

    assert artifacts.list_share_files(tmp_path) == []
    assert not (tmp_path / "_share/sent/a.md").exists()   # 退避もされない
    assert not (tmp_path / "_share/a.md").exists()


def test_retention_keeps_recent_files(tmp_path):
    write(tmp_path, "_share/a.md", "中身")
    artifacts.archive_sent(tmp_path, ["_share/a.md"], retention_days=7)
    assert (tmp_path / "_share/sent/a.md").exists()


def test_old_archived_files_are_pruned(tmp_path):
    write(tmp_path, "_share/sent/old.md", "古い")
    age(tmp_path / "_share/sent/old.md", days=30)
    write(tmp_path, "_share/sent/new.md", "新しい")

    assert artifacts.prune_sent(tmp_path, retention_days=7) == 1
    assert not (tmp_path / "_share/sent/old.md").exists()
    assert (tmp_path / "_share/sent/new.md").exists()


def test_archiving_also_prunes(tmp_path):
    write(tmp_path, "_share/sent/old.md", "古い")
    age(tmp_path / "_share/sent/old.md", days=30)
    write(tmp_path, "_share/a.md", "中身")

    artifacts.archive_sent(tmp_path, ["_share/a.md"], retention_days=7)
    assert not (tmp_path / "_share/sent/old.md").exists()
    assert (tmp_path / "_share/sent/a.md").exists()


def test_pruning_never_touches_the_queue(tmp_path):
    # 未送信のファイルは、どれだけ古くても消さない
    write(tmp_path, "_share/pending.md", "未送信")
    age(tmp_path / "_share/pending.md", days=365)

    artifacts.prune_sent(tmp_path, retention_days=7)
    assert artifacts.list_share_files(tmp_path) == ["_share/pending.md"]


def test_prune_without_sent_directory(tmp_path):
    assert artifacts.prune_sent(tmp_path, retention_days=7) == 0
