"""GitHub Actions の実行結果を取得する（CLAUDE.md §22）。

**Webhook ではなくポーリングを使う。** Webhook で受けるには自宅 PC を HTTPS で公開する
必要があり、Socket Mode によって不要にしたトンネルを復活させることになる。ポーリング
なら outbound の HTTPS だけで済み、公開ポートゼロという性質を保てる。

さらに重要なのは返信先。Bridge は「どの Slack スレッドがどのコミットを push したか」を
知っているので、結果を**依頼元のスレッドへ**返せる。Actions から Slack の Incoming
Webhook を直接叩く方式では固定チャンネルにしか送れず、スレッドから切り離される。

プライベートリポジトリの run を読むには Personal Access Token が必要
（fine-grained なら Actions: Read-only、classic なら repo スコープ）。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"

# 実行中とみなす status
_PENDING_STATUSES = {"queued", "in_progress", "waiting", "requested", "pending"}


class GitHubError(RuntimeError):
    """GitHub API の呼び出しに失敗した。"""


@dataclass
class WorkflowRun:
    name: str
    status: str            # queued / in_progress / completed
    conclusion: str | None  # success / failure / cancelled / skipped / ...
    html_url: str
    run_started_at: str | None = None
    updated_at: str | None = None

    @property
    def is_finished(self) -> bool:
        return self.status == "completed"

    @property
    def succeeded(self) -> bool:
        return self.conclusion == "success"


@dataclass
class RunsResult:
    runs: list[WorkflowRun] = field(default_factory=list)
    timed_out: bool = False
    error: str = ""

    @property
    def all_finished(self) -> bool:
        return bool(self.runs) and all(r.is_finished for r in self.runs)

    @property
    def all_succeeded(self) -> bool:
        return bool(self.runs) and all(r.succeeded for r in self.runs)


def _get(url: str, token: str, timeout: int = 30) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {token}",
            "User-Agent": "slack-bridge",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        if exc.code == 401:
            raise GitHubError(
                "GitHub の認証に失敗しました (401)。GITHUB_TOKEN を確認してください。"
            ) from exc
        if exc.code == 403:
            raise GitHubError(
                "GitHub に拒否されました (403)。トークンの権限か、レート制限の可能性があります。"
            ) from exc
        if exc.code == 404:
            raise GitHubError(
                "対象が見つかりません (404)。プライベートリポジトリを読むには "
                "トークンに Actions の読み取り権限が必要です。"
            ) from exc
        raise GitHubError(f"GitHub API エラー ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise GitHubError(f"GitHub へ接続できませんでした: {exc.reason}") from exc


def runs_for_commit(owner: str, repo: str, sha: str, token: str) -> list[WorkflowRun]:
    """コミットに紐づく workflow run を取得する。"""
    url = f"{API_ROOT}/repos/{owner}/{repo}/actions/runs?head_sha={sha}&per_page=20"
    payload = _get(url, token)
    runs = []
    for item in payload.get("workflow_runs") or []:
        runs.append(
            WorkflowRun(
                name=item.get("name") or "(無名)",
                status=item.get("status") or "unknown",
                conclusion=item.get("conclusion"),
                html_url=item.get("html_url") or "",
                run_started_at=item.get("run_started_at"),
                updated_at=item.get("updated_at"),
            )
        )
    return runs


def wait_for_runs(
    owner: str,
    repo: str,
    sha: str,
    token: str,
    timeout_seconds: int = 900,
    poll_interval: int = 15,
    startup_grace: int = 60,
    sleep=time.sleep,
    now=time.monotonic,
) -> RunsResult:
    """コミットの run が出揃って完了するまで待つ。

    push 直後は run がまだ登録されていないことがあるため、``startup_grace`` 秒までは
    「run が0件」を待機として扱う。それを過ぎても0件なら、その push に対する CI は
    無い（＝ワークフローの対象外）と判断して終了する。
    """
    started = now()
    last_runs: list[WorkflowRun] = []

    while True:
        try:
            last_runs = runs_for_commit(owner, repo, sha, token)
        except GitHubError as exc:
            return RunsResult(runs=last_runs, error=str(exc))

        elapsed = now() - started

        if not last_runs:
            if elapsed >= startup_grace:
                return RunsResult(runs=[])  # CI 対象外
        elif all(r.is_finished for r in last_runs):
            return RunsResult(runs=last_runs)

        if elapsed >= timeout_seconds:
            return RunsResult(runs=last_runs, timed_out=True)

        sleep(poll_interval)


def is_pending(run: WorkflowRun) -> bool:
    return run.status in _PENDING_STATUSES
