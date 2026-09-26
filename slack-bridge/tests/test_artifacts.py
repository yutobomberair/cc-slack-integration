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
