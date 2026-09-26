import io
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import inbox  # noqa: E402


class FakeResponse:
    def __init__(self, payload: bytes, content_type: str = "application/octet-stream"):
        self._body = io.BytesIO(payload)
        self.headers = {"Content-Type": content_type}

    def read(self, size=-1):
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(payload: bytes, content_type="application/octet-stream"):
    def _open(request, timeout=None):
        # Bearer が付いていないと Slack は実体を返さない
        assert request.headers["Authorization"].startswith("Bearer ")
        return FakeResponse(payload, content_type)

    return _open


def meta(name="report.md", size=10, url="https://files.slack.com/x"):
    return {"name": name, "size": size, "url_private_download": url}


# --------------------------------------------------------------------------
# 保存
# --------------------------------------------------------------------------

def test_saves_attachment_into_inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox.urllib.request, "urlopen", fake_urlopen(b"hello"))
    saved = inbox.save_files([meta()], tmp_path, "xoxb-test")

    assert saved[0].ok
    assert saved[0].path == "_inbox/report.md"
    assert (tmp_path / "_inbox" / "report.md").read_bytes() == b"hello"


def test_same_name_does_not_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox.urllib.request, "urlopen", fake_urlopen(b"one"))
    inbox.save_files([meta()], tmp_path, "t")
    monkeypatch.setattr(inbox.urllib.request, "urlopen", fake_urlopen(b"two"))
    saved = inbox.save_files([meta()], tmp_path, "t")

    assert saved[0].path == "_inbox/report-1.md"
    assert (tmp_path / "_inbox" / "report.md").read_bytes() == b"one"
    assert (tmp_path / "_inbox" / "report-1.md").read_bytes() == b"two"


def test_no_files_is_a_noop(tmp_path):
    assert inbox.save_files([], tmp_path, "t") == []
    assert not (tmp_path / "_inbox").exists()


def test_each_file_is_independent(tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("boom")
        return FakeResponse(b"ok")

    monkeypatch.setattr(inbox.urllib.request, "urlopen", flaky)
    saved = inbox.save_files([meta("a.md"), meta("b.md")], tmp_path, "t")

    assert saved[0].ok is False
    assert saved[1].ok is True


# --------------------------------------------------------------------------
# 安全性
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [
        # 区切りを潰したうえで先頭のドットも落とすので、親ディレクトリを指せない
        ("../../escape.md", "_.._escape.md"),
        ("a/b.md", "a_b.md"),
        ("bad:name?.md", "bad_name_.md"),
        ("", "file"),
        ("...", "file"),
    ],
)
def test_safe_name_cannot_escape_inbox(name, expected):
    assert inbox.safe_name(name) == expected


def test_traversal_attempt_stays_inside_inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox.urllib.request, "urlopen", fake_urlopen(b"x"))
    saved = inbox.save_files([meta(name="../../etc/passwd")], tmp_path, "t")

    written = tmp_path / saved[0].path
    assert written.resolve().parent == (tmp_path / "_inbox").resolve()


def test_oversized_declared_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox.urllib.request, "urlopen", fake_urlopen(b"x"))
    saved = inbox.save_files(
        [meta(size=inbox.MAX_DOWNLOAD_BYTES + 1)], tmp_path, "t"
    )
    assert saved[0].ok is False
    assert "上限" in saved[0].error


def test_oversized_actual_body_is_refused(tmp_path, monkeypatch):
    # size が過少申告でも、実体が大きければ保存しない
    payload = b"x" * (inbox.MAX_DOWNLOAD_BYTES + 1)
    monkeypatch.setattr(inbox.urllib.request, "urlopen", fake_urlopen(payload))
    saved = inbox.save_files([meta(size=1)], tmp_path, "t")
    assert saved[0].ok is False
    assert not (tmp_path / "_inbox" / "report.md").exists()


def test_file_without_url_is_skipped(tmp_path):
    saved = inbox.save_files([{"name": "drive.doc", "size": 1}], tmp_path, "t")
    assert saved[0].ok is False
    assert "URL" in saved[0].error


# --------------------------------------------------------------------------
# スコープ不足
# --------------------------------------------------------------------------

def test_missing_scope_is_detected_and_stops_early(tmp_path, monkeypatch):
    # スコープが無いと Slack は 403 ではなく HTML のログインページを返す
    monkeypatch.setattr(
        inbox.urllib.request, "urlopen", fake_urlopen(b"<html>login</html>", "text/html")
    )
    saved = inbox.save_files([meta("a.md"), meta("b.md")], tmp_path, "t")

    assert len(saved) == 1  # 2件目は試さない
    assert "files:read" in saved[0].error


# --------------------------------------------------------------------------
# 報告文
# --------------------------------------------------------------------------

def test_prompt_note_lists_saved_paths():
    note = inbox.prompt_note([inbox.Saved("_inbox/a.csv", "a.csv", 10)])
    assert "_inbox/a.csv" in note
    assert note.endswith("\n\n")


def test_prompt_note_is_empty_when_nothing_saved():
    assert inbox.prompt_note([inbox.Saved("", "a", 0, error="失敗")]) == ""


def test_report_mentions_successes_and_failures():
    text = inbox.report(
        [
            inbox.Saved("_inbox/a.csv", "a.csv", 10),
            inbox.Saved("", "b.csv", 0, error="取得失敗"),
        ]
    )
    assert "_inbox/a.csv" in text
    assert "b.csv" in text
    assert "取得失敗" in text


def test_report_is_empty_without_attachments():
    assert inbox.report([]) == ""
