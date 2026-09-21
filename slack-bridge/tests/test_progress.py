import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.formatting import (  # noqa: E402
    completed_header,
    failed_header,
    format_duration,
    progress_message,
    start_message,
)
from app.progress import ProgressReporter  # noqa: E402


class FakeClient:
    """chat_update の呼び出しを記録するだけのスタブ。"""

    def __init__(self, fail: bool = False) -> None:
        self.updates: list[tuple[str, str, str]] = []
        self.fail = fail
        self._lock = threading.Lock()

    def chat_update(self, channel: str, ts: str, text: str):
        if self.fail:
            raise RuntimeError("slack down")
        with self._lock:
            self.updates.append((channel, ts, text))
        return {"ok": True}


def test_format_duration():
    assert format_duration(5) == "5秒"
    assert format_duration(65) == "1分05秒"
    assert format_duration(143) == "2分23秒"
    assert format_duration(3700) == "1時間01分"


def test_start_message_distinguishes_continuation():
    assert "処理を開始しました" in start_message("propose")
    assert "継続" in start_message("propose", continued=True)


def test_progress_and_headers_include_project_name():
    assert "*propose*" in progress_message("propose", 30)
    assert "*propose*" in completed_header("propose", 30)
    assert "*propose*" in failed_header("propose", 30)


def test_completed_header_shows_stats():
    text = completed_header("propose", 143, cost_usd=0.4242, turns=14)
    assert "2分23秒" in text
    assert "14ターン" in text
    assert "$0.42" in text


def test_completed_header_omits_missing_stats():
    text = completed_header("propose", 10)
    assert "ターン" not in text
    assert "$" not in text


def test_finish_writes_final_state():
    client = FakeClient()
    reporter = ProgressReporter(client, "C111", "111.222", lambda e: "…", interval=60)
    reporter.start()
    elapsed = reporter.finish("done")
    assert client.updates[-1] == ("C111", "111.222", "done")
    assert elapsed >= 0


def test_reporter_updates_periodically():
    client = FakeClient()
    reporter = ProgressReporter(
        client, "C111", "111.222", lambda e: f"経過 {int(e * 1000)}", interval=0.05
    )
    reporter.start()
    time.sleep(0.22)
    reporter.finish("done")
    # 定期更新が最低2回は走り、最後に確定表示で上書きされている
    assert len(client.updates) >= 3
    assert client.updates[-1][2] == "done"


def test_no_message_ts_disables_updates():
    client = FakeClient()
    reporter = ProgressReporter(client, "C111", None, lambda e: "…", interval=0.01)
    reporter.start()
    time.sleep(0.05)
    reporter.finish("done")
    assert client.updates == []


def test_slack_failure_does_not_raise():
    client = FakeClient(fail=True)
    reporter = ProgressReporter(client, "C111", "111.222", lambda e: "…", interval=0.01)
    reporter.start()
    time.sleep(0.05)
    reporter.finish("done")  # 例外が外へ漏れないこと
