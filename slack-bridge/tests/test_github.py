import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import formatting, github_ops  # noqa: E402
from app.github_ops import RunsResult, WorkflowRun  # noqa: E402


def run(name="test", status="completed", conclusion="success", url="https://x/1"):
    return WorkflowRun(name=name, status=status, conclusion=conclusion, html_url=url)


# --------------------------------------------------------------------------
# run の状態判定
# --------------------------------------------------------------------------

def test_completed_success_is_finished_and_succeeded():
    r = run()
    assert r.is_finished and r.succeeded


def test_completed_failure_is_finished_but_not_succeeded():
    r = run(status="completed", conclusion="failure")
    assert r.is_finished and not r.succeeded


def test_in_progress_is_not_finished():
    r = run(status="in_progress", conclusion=None)
    assert not r.is_finished


def test_results_aggregate_state():
    ok = RunsResult(runs=[run(), run(name="lint")])
    assert ok.all_finished and ok.all_succeeded

    mixed = RunsResult(runs=[run(), run(name="lint", conclusion="failure")])
    assert mixed.all_finished and not mixed.all_succeeded

    empty = RunsResult(runs=[])
    assert not empty.all_finished and not empty.all_succeeded


# --------------------------------------------------------------------------
# ポーリング（time は差し替えて即座に回す）
# --------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def wait(monkeypatch, sequence, **kwargs):
    """runs_for_commit が sequence を順に返すようにして wait_for_runs を回す。"""
    calls = {"n": 0}

    def fake_runs(owner, repo, sha, token):
        i = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        result = sequence[i]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(github_ops, "runs_for_commit", fake_runs)
    clock = FakeClock()
    return github_ops.wait_for_runs(
        "o", "r", "sha", "tok", sleep=clock.sleep, now=clock.now, **kwargs
    )


def test_waits_until_runs_complete(monkeypatch):
    sequence = [
        [run(status="queued", conclusion=None)],
        [run(status="in_progress", conclusion=None)],
        [run()],
    ]
    result = wait(monkeypatch, sequence, poll_interval=5, timeout_seconds=600)
    assert result.all_succeeded
    assert not result.timed_out


def test_no_runs_after_grace_means_no_ci(monkeypatch):
    # push しても対象ワークフローが無い場合、待ち続けずに「CI 無し」で返す
    result = wait(monkeypatch, [[]], poll_interval=10, startup_grace=30, timeout_seconds=600)
    assert result.runs == []
    assert not result.timed_out
    assert not result.error


def test_run_appearing_late_is_picked_up(monkeypatch):
    # push 直後は run が未登録のことがある
    sequence = [[], [], [run()]]
    result = wait(monkeypatch, sequence, poll_interval=10, startup_grace=60, timeout_seconds=600)
    assert result.all_succeeded


def test_timeout_is_reported(monkeypatch):
    sequence = [[run(status="in_progress", conclusion=None)]]
    result = wait(monkeypatch, sequence, poll_interval=10, timeout_seconds=30)
    assert result.timed_out
    assert result.runs


def test_api_error_is_returned_not_raised(monkeypatch):
    result = wait(monkeypatch, [github_ops.GitHubError("401 だめ")], poll_interval=5)
    assert "401" in result.error
    assert not result.timed_out


# --------------------------------------------------------------------------
# Slack への報告
# --------------------------------------------------------------------------

def test_success_report():
    text = formatting.ci_report([run(name="test")])
    assert "CI 成功" in text
    assert "`test`" in text
    assert "ログを見る" in text


def test_failure_report_offers_follow_up():
    text = formatting.ci_report([run(name="test", conclusion="failure")])
    assert "CI 失敗" in text
    assert "原因を調べて" in text


def test_mixed_report_is_failure():
    text = formatting.ci_report([run(), run(name="lint", conclusion="failure")])
    assert "CI 失敗" in text


def test_no_runs_report():
    text = formatting.ci_report([])
    assert "GitHub Actions はありません" in text


def test_timeout_report():
    text = formatting.ci_report([run(status="in_progress", conclusion=None)], timed_out=True)
    assert "まだ終わっていません" in text


def test_error_report():
    text = formatting.ci_report([], error="401 認証失敗")
    assert "取得できませんでした" in text
    assert "401" in text
