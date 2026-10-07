"""スレッドごとの設定（階層・モデル・推論の強さ）のテスト。

重点は**項目ごとに扱えること**。1つの記録にまとめた結果、`cd: .` で階層を戻したら
モデルの設定まで消える、という事故が起きうるのでそこを固定する。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.thread_prefs import ThreadPrefs  # noqa: E402


@pytest.fixture
def prefs(tmp_path):
    return ThreadPrefs(tmp_path / "prefs.json")


def test_unset_thread_has_no_settings(prefs):
    assert prefs.all("C1", "1.0") == {}
    assert prefs.get("C1", "1.0", "model") == ""


def test_roundtrip(prefs):
    prefs.set("C1", "1.0", "proj", model="sonnet")
    assert prefs.all("C1", "1.0") == {"model": "sonnet"}


def test_fields_are_independent(prefs):
    """cd: . で階層を戻してもモデルの設定は残る。"""
    prefs.set("C1", "1.0", "proj", model="sonnet", effort="max")
    prefs.set("C1", "1.0", "proj", workdir="movie")
    prefs.set("C1", "1.0", "proj", workdir="")          # 直下へ戻す

    remaining = prefs.all("C1", "1.0")
    assert remaining == {"model": "sonnet", "effort": "max"}


def test_clearing_one_field_keeps_the_others(prefs):
    prefs.set("C1", "1.0", "proj", model="sonnet", effort="max")
    prefs.set("C1", "1.0", "proj", model="")
    assert prefs.all("C1", "1.0") == {"effort": "max"}


def test_record_is_dropped_when_everything_is_default(prefs):
    # 既定と同じ状態を記録に残す意味が無い
    prefs.set("C1", "1.0", "proj", model="sonnet")
    prefs.set("C1", "1.0", "proj", model="")
    assert prefs.all("C1", "1.0") == {}


def test_survives_a_restart(tmp_path):
    path = tmp_path / "prefs.json"
    ThreadPrefs(path).set("C1", "1.0", "proj", model="sonnet", workdir="movie")
    reopened = ThreadPrefs(path)
    assert reopened.all("C1", "1.0") == {"workdir": "movie", "model": "sonnet"}


def test_threads_are_independent(prefs):
    prefs.set("C1", "1.0", "proj", model="sonnet")
    assert prefs.all("C1", "2.0") == {}


def test_unknown_field_is_refused(prefs):
    # 綴り間違いを黙って捨てると「設定したのに効かない」になる
    with pytest.raises(ValueError):
        prefs.set("C1", "1.0", "proj", modle="sonnet")


def test_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "prefs.json"
    path.write_text("{ broken", encoding="utf-8")
    assert ThreadPrefs(path).all("C1", "1.0") == {}


def test_old_entries_are_evicted(tmp_path):
    store = ThreadPrefs(tmp_path / "prefs.json", max_entries=2)
    for i in range(4):
        store.set("C1", f"{i}.0", "proj", model="sonnet")
    kept = [t for t in range(4) if store.all("C1", f"{t}.0")]
    assert len(kept) == 2
