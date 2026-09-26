"""受理した依頼を実行し、結果を返すまで（``slack_handler`` から切り出し）。

ここはワーカースレッド側。例外を外へ漏らさないのが約束で、漏らすと
``ThreadPoolExecutor`` が黙って飲み込み、Slack には何も返らないまま終わる。

流れは常にこの順。

    進捗表示を開始 → 実行前の状態を控える → Claude 実行 → 結果を投稿
    → コミット/push → _share/ の添付 → 出力一覧 → CI 監視を投げる
"""

from __future__ import annotations

import logging
import subprocess
import threading

from app import (
    artifacts,
    claude_runner,
    file_flow,
    formatting,
    git_ops,
    github_ops,
    share_queue,
)
from app.config import Project, Settings
from app.progress import ProgressReporter
from app.reply import Thread
from app.session_store import derive_session_id
from app.task import BridgeContext, TaskRequest

logger = logging.getLogger(__name__)


def run_task(ctx: BridgeContext, client, task: TaskRequest) -> None:
    """依頼1件を実行する。ここで例外を外に漏らさない。"""
    settings = ctx.settings
    project = task.project
    thread = Thread(client, task.channel_id, task.thread_ts)

    reporter = ProgressReporter(
        client,
        task.channel_id,
        task.message_ts,
        render=lambda elapsed: formatting.progress_message(
            project.name,
            elapsed,
            continued=task.continued,
            implement=task.implement,
            model=task.model or settings.runtime.model,
        ),
        interval=settings.runtime.progress_interval_seconds,
    )
    reporter.start()

    session_id = (
        derive_session_id(task.channel_id, task.thread_ts)
        if settings.runtime.session_continuation
        else None
    )

    try:
        # 同じスレッドのセッションを並行して更新しないよう直列化する。
        with ctx.thread_locks.for_thread(task.channel_id, task.thread_ts):
            before = _snapshot(task)
            result = claude_runner.run(
                prompt=file_flow.build_prompt(task),
                working_directory=project.working_directory,
                profile=settings.profiles[task.profile_key],
                runtime=settings.runtime,
                session_id=session_id,
                resume=task.continued,
                model=task.model,
            )
    except Exception as exc:  # noqa: BLE001 - ワーカーの最終防衛線
        logger.exception("タスク実行中に予期しない例外が発生しました")
        reporter.finish(formatting.failed_header(project.name, reporter.elapsed))
        thread.post_long(
            formatting.failure_message(project.name, f"{type(exc).__name__}: {exc}")
        )
        return

    if not result.ok:
        reporter.finish(formatting.failed_header(project.name, reporter.elapsed))
        thread.post_long(formatting.failure_message(project.name, result.error))
        return

    if session_id:
        # 成功したときだけ記録する。失敗した実行を継続対象にしても意味がないため。
        ctx.sessions.record(task.channel_id, task.thread_ts, session_id, project.key)

    elapsed = reporter.finish(
        formatting.completed_header(
            project.name,
            reporter.elapsed,
            result.cost_usd,
            result.num_turns,
            model=task.model or settings.runtime.model,
        )
    )
    logger.info(
        "完了 project=%s thread_ts=%s elapsed=%.1fs restarted=%s",
        project.key,
        task.thread_ts,
        elapsed,
        result.session_restarted,
    )

    body = formatting.to_mrkdwn(result.text) + formatting.denials_note(
        result.permission_denials
    )
    if result.session_restarted:
        body += formatting.session_restarted_note()

    pushed_sha = None
    produced: list[str] = []
    if task.implement and before.head is not None:
        commit_text, pushed_sha, produced = _commit_and_report(task, before.head)
        body += commit_text
    elif task.implement and before.files is not None:
        produced = artifacts.changed_since(
            before.files, artifacts.scan(project.working_directory)
        )
    thread.post_long(body)

    if task.implement:
        # _share/ に置かれたものは「共有したい」という意思表示なので自動で添付する。
        file_flow.auto_share(thread, settings, project)

    # 残りは一覧だけ出す。中身は送らず、番号で取り出せるようにする。
    _, rest = share_queue.split_shared(produced)
    file_flow.report_outputs(thread, ctx.outputs, project, rest)

    # CI は分単位でかかるので、結果を待たずにここで応答を返し、完了後に追って投稿する。
    # ワーカーを占有しないよう専用スレッドで待つ（max_workers=1 でも他の依頼が詰まらない）。
    if pushed_sha:
        threading.Thread(
            target=_watch_ci,
            args=(settings, thread, project, pushed_sha),
            daemon=True,
            name="ci-watch",
        ).start()


class _Before:
    """実行前の状態。``head`` は git のスナップショット、``files`` は mtime 走査。

    git 管理下なら ``head`` だけ、管理外なら ``files`` だけが入る。どちらも
    調査モードでは取らない（書き込めないので比較する意味が無い）。
    """

    __slots__ = ("head", "files")

    def __init__(self, head, files) -> None:
        self.head = head
        self.files = files


def _snapshot(task: TaskRequest) -> _Before:
    """実行前の状態を控える。

    git 管理外のプロジェクトでは snapshot を取らない。head が None だと後段の
    commit 処理ごと飛ぶので、未管理ディレクトリでも実行はできる（ただし変更の
    自動 commit も差分報告も行われない）。その代わり mtime で出力を検出し、
    「何が出力されたか」を一覧に出すところまでは同じように動かす。
    """
    cwd = task.project.working_directory
    if not task.implement:
        return _Before(None, None)
    if git_ops.is_repo(cwd):
        return _Before(git_ops.snapshot(cwd), None)
    return _Before(None, artifacts.scan(cwd))


def _commit_and_report(
    task: TaskRequest, before: git_ops.Snapshot
) -> tuple[str, str | None, list[str]]:
    """この実行で変わったファイルを commit し、報告文を組み立てる（CLAUDE.md §21）。

    返り値の3つめは、この実行で変わったファイルの一覧。Slack への共有候補として
    呼び出し側が使う（``app/artifacts.py``）。commit に失敗しても一覧は返す。
    ファイル自体は作業ツリーに残っているので、受け渡しはできるため。
    """
    project = task.project
    cwd = project.working_directory
    try:
        changed, preexisting = git_ops.changed_since(cwd, before)
        # _share/ は Slack への転送用の置き場であってプロジェクトの成果物ではない。
        # commit するとリポジトリが共有ファイルで汚れるので、ここで外す。
        _, changed = share_queue.split_shared(changed)
    except (git_ops.GitError, OSError) as exc:
        logger.exception("変更の検出に失敗しました project=%s", project.key)
        return formatting.commit_failed_note(str(exc)), None, []

    if not changed:
        return (
            formatting.no_changes_note() + formatting.skipped_dirty_note(preexisting),
            None,
            [],
        )

    try:
        commit = git_ops.commit_changes(
            cwd, changed, git_ops.build_commit_message(project.name, task.prompt)
        )
    except (git_ops.GitError, OSError) as exc:
        logger.exception("コミットに失敗しました project=%s", project.key)
        return formatting.commit_failed_note(str(exc)), None, changed

    if not commit.committed:
        return (
            formatting.no_changes_note() + formatting.skipped_dirty_note(preexisting),
            None,
            changed,
        )

    logger.info(
        "コミットしました project=%s sha=%s files=%d",
        project.key,
        commit.short_sha,
        len(commit.files),
    )
    report = formatting.commit_report(
        branch=git_ops.current_branch(cwd),
        short_sha=commit.short_sha,
        files=commit.files,
        stat=commit.stat,
        diff=commit.diff,
    ) + formatting.skipped_dirty_note(preexisting)

    push_text, pushed = _push_and_report(project, task.thread_ts)
    return report + push_text, (commit.sha if pushed else None), changed


def _push_and_report(project: Project, thread_ts: str) -> tuple[str, bool]:
    """コミットした内容を claude/<スレッド> ブランチへ push する（CLAUDE.md §21）。

    同じスレッドからは常に同じブランチ名になるので、スレッド内で依頼を重ねると
    1本のブランチにコミットが積まれ、PR も1つで足りる。
    """
    if not project.allow_push:
        return formatting.push_skipped_note(
            "このプロジェクトでは push が許可されていません"
        ), False

    cwd = project.working_directory
    if git_ops.remote_info(cwd) is None:
        return formatting.push_skipped_note("リモートが設定されていません"), False

    branch = git_ops.thread_branch_name(thread_ts)
    try:
        result = git_ops.push_to_branch(cwd, branch)
    except subprocess.TimeoutExpired:
        logger.warning("push がタイムアウトしました project=%s", project.key)
        return formatting.push_failed_note(branch, "push がタイムアウトしました"), False
    except (git_ops.GitError, OSError) as exc:
        logger.exception("push に失敗しました project=%s", project.key)
        return formatting.push_failed_note(branch, str(exc)), False

    if not result.pushed:
        logger.warning("push に失敗しました project=%s: %s", project.key, result.error)
        return formatting.push_failed_note(branch, result.error), False

    logger.info(
        "push しました project=%s branch=%s commits=%d",
        project.key,
        branch,
        len(result.commits),
    )
    return formatting.push_report(
        branch=result.branch,
        commits=result.commits,
        compare_url=result.compare_url,
        branch_url=result.branch_url,
    ), True


def _watch_ci(settings: Settings, thread: Thread, project: Project, sha: str) -> None:
    try:
        text = _ci_report(settings, project, sha)
    except Exception:  # noqa: BLE001 - 監視スレッドの最終防衛線
        logger.exception("CI の監視中に例外が発生しました project=%s", project.key)
        return
    if text:
        thread.post_long(text.lstrip())


def _ci_report(settings: Settings, project: Project, sha: str) -> str:
    """push したコミットの GitHub Actions を待って結果を返す（CLAUDE.md §22）。

    Webhook ではなくポーリングで取る。Webhook にすると自宅 PC を HTTPS で公開する
    必要があり、Socket Mode で不要にしたトンネルが復活してしまう。
    """
    if not settings.github_token or settings.runtime.ci_wait_seconds <= 0:
        return ""

    info = git_ops.remote_info(project.working_directory)
    if info is None or not info.is_github:
        return ""

    logger.info("CI の完了を待ちます project=%s sha=%s", project.key, sha[:8])
    result = github_ops.wait_for_runs(
        owner=info.owner,
        repo=info.repo,
        sha=sha,
        token=settings.github_token,
        timeout_seconds=settings.runtime.ci_wait_seconds,
        poll_interval=settings.runtime.ci_poll_interval_seconds,
    )
    logger.info(
        "CI 待機終了 project=%s runs=%d timed_out=%s error=%s",
        project.key,
        len(result.runs),
        result.timed_out,
        result.error or "-",
    )
    return formatting.ci_report(result.runs, timed_out=result.timed_out, error=result.error)
