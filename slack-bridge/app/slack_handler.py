"""Slack イベントの受信とディスパッチ（CLAUDE.md §5, §12, §19）。

重要な前提が2つある。

1. Slack はハンドラが数秒以内に ack しないとイベントを再送する。Claude Code の実行は
   分単位でかかるため、ack はイベント受信直後に返し、実処理はワーカースレッドへ逃がす。
2. それでも再送は起こりうる（ネットワーク切断・再接続など）。``event_id`` で冪等化
   しないと同一タスクが多重起動する。調査タスクなら無駄な課金で済むが、実装タスク
   では同じ変更が二重に走る。
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from slack_bolt import App

from app import claude_runner, formatting, git_ops, github_ops, project_router, task_mode
from app.config import Project, Settings
from app.progress import ProgressReporter
from app.session_store import SessionStore, derive_session_id

logger = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@[UW][A-Z0-9]+>")
_HSPACE_RE = re.compile(r"[ 	]+")


class SeenEvents:
    """処理済み event_id の有限リングバッファ。"""

    def __init__(self, capacity: int = 2048) -> None:
        self._capacity = capacity
        self._items: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def add_if_new(self, event_id: str | None) -> bool:
        """未処理なら記録して True。既に処理済みなら False。"""
        if not event_id:
            return True
        with self._lock:
            if event_id in self._items:
                return False
            self._items[event_id] = None
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)
            return True


class ThreadLocks:
    """Slack スレッドごとのロック。

    同じスレッドへ続けて依頼が飛んだ場合、Claude Code の同一セッションを2つの
    プロセスが同時に更新してしまう。スレッド単位で直列化してこれを防ぐ。
    別スレッド・別プロジェクト同士は当然並行に走ってよい。
    """

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def for_thread(self, channel_id: str, thread_ts: str) -> threading.Lock:
        key = f"{channel_id}:{thread_ts}"
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock


def strip_mentions(text: str) -> str:
    """`<@U123>` 形式のメンションを除去する（CLAUDE.md §7）。

    メンション跡に残る余分な空白は詰めるが、改行は保持する。Slack から送られる
    依頼は複数行であることが多く、行構造がプロンプトの意味を持つため。
    """
    without = _MENTION_RE.sub(" ", text or "")
    lines = [_HSPACE_RE.sub(" ", line).strip() for line in without.splitlines()]
    return "\n".join(lines).strip()


def create_app(settings: Settings) -> App:
    app = App(token=settings.bot_token)
    seen = SeenEvents()
    sessions = SessionStore(settings.runtime.session_store_path)
    # 同一スレッドの二重実行を防ぐ。Claude Code のセッションは並行更新できないため、
    # 同じスレッドへ連続で依頼が来た場合は順番に処理する必要がある。
    thread_locks = ThreadLocks()
    executor = ThreadPoolExecutor(
        max_workers=settings.runtime.max_workers,
        thread_name_prefix="claude-worker",
    )

    @app.event("app_mention")
    def handle_app_mention(body: dict, event: dict, client) -> None:
        # ここは即座に戻ること。重い処理を書かない。
        event_id = body.get("event_id")
        if not seen.add_if_new(event_id):
            logger.info("再送イベントを無視しました event_id=%s", event_id)
            return

        channel_id = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts")

        route = project_router.route(settings, channel_id)
        if not route.is_known:
            # 未登録チャンネルでは Claude Code を起動しない（CLAUDE.md §15）。
            logger.warning("未登録チャンネルからのメンション channel_id=%s", channel_id)
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=project_router.unknown_channel_message(channel_id),
            )
            return

        project = route.project
        prompt = strip_mentions(event.get("text", ""))
        if not prompt:
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=(
                    ":thinking_face: 依頼内容が空でした。"
                    "メンションに続けて依頼を書いてください。"
                ),
            )
            return

        # 実行モードの決定（CLAUDE.md §20）。既定は常に調査。
        mode = task_mode.resolve(prompt, project.profile, project.allow_implement)
        if mode.denied:
            logger.warning(
                "コード変更が要求されましたが許可されていません project=%s", project.key
            )
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=task_mode.implement_not_allowed_message(project.name),
            )
            return
        if mode.is_implement and not git_ops.is_repo(project.working_directory):
            logger.warning("git 管理外のためコード変更を拒否 project=%s", project.key)
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=task_mode.not_a_repo_message(project.name),
            )
            return
        prompt = mode.prompt

        continued = settings.runtime.session_continuation and sessions.is_started(
            channel_id, thread_ts
        )
        logger.info(
            "受理 project=%s channel=%s thread_ts=%s prompt_len=%d continued=%s",
            project.key,
            channel_id,
            thread_ts,
            len(prompt),
            continued,
        )

        # 開始メッセージの ts を控える。以降これを chat.update で書き換えていく。
        message_ts = None
        try:
            posted = client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=formatting.start_message(
                    project.name, continued=continued, implement=mode.is_implement
                ),
            )
            message_ts = posted.get("ts")
        except Exception:  # noqa: BLE001
            logger.exception("開始メッセージの投稿に失敗しました channel=%s", channel_id)

        executor.submit(
            _run_task,
            settings,
            client,
            sessions,
            thread_locks,
            project,
            prompt,
            channel_id,
            thread_ts,
            message_ts,
            continued,
            mode.profile_key,
        )

    app._slack_bridge_executor = executor  # 終了時に片付けられるよう保持
    return app


def _run_task(
    settings: Settings,
    client,
    sessions: SessionStore,
    thread_locks: "ThreadLocks",
    project: Project,
    prompt: str,
    channel_id: str,
    thread_ts: str,
    message_ts: str | None,
    continued: bool,
    profile_key: str,
) -> None:
    """ワーカースレッド側。ここで例外を外に漏らさない。"""
    implement = profile_key == task_mode.IMPLEMENT
    reporter = ProgressReporter(
        client,
        channel_id,
        message_ts,
        render=lambda elapsed: formatting.progress_message(
            project.name, elapsed, continued=continued, implement=implement
        ),
        interval=settings.runtime.progress_interval_seconds,
    )
    reporter.start()

    session_id = (
        derive_session_id(channel_id, thread_ts)
        if settings.runtime.session_continuation
        else None
    )

    try:
        # 同じスレッドのセッションを並行して更新しないよう直列化する。
        with thread_locks.for_thread(channel_id, thread_ts):
            profile = settings.profiles[profile_key]
            before = git_ops.snapshot(project.working_directory) if implement else None
            result = claude_runner.run(
                prompt=prompt,
                working_directory=project.working_directory,
                profile=profile,
                runtime=settings.runtime,
                session_id=session_id,
                resume=continued,
            )
    except Exception as exc:  # noqa: BLE001 - ワーカーの最終防衛線
        logger.exception("タスク実行中に予期しない例外が発生しました")
        reporter.finish(formatting.failed_header(project.name, reporter.elapsed))
        _post_chunks(
            client,
            channel_id,
            thread_ts,
            formatting.failure_message(project.name, f"{type(exc).__name__}: {exc}"),
        )
        return

    if not result.ok:
        reporter.finish(formatting.failed_header(project.name, reporter.elapsed))
        _post_chunks(
            client,
            channel_id,
            thread_ts,
            formatting.failure_message(project.name, result.error),
        )
        return

    if session_id:
        # 成功したときだけ記録する。失敗した実行を継続対象にしても意味がないため。
        sessions.record(channel_id, thread_ts, session_id, project.key)

    elapsed = reporter.finish(
        formatting.completed_header(
            project.name, reporter.elapsed, result.cost_usd, result.num_turns
        )
    )
    logger.info(
        "完了 project=%s thread_ts=%s elapsed=%.1fs restarted=%s",
        project.key,
        thread_ts,
        elapsed,
        result.session_restarted,
    )

    body = formatting.to_mrkdwn(result.text) + formatting.denials_note(result.permission_denials)
    if result.session_restarted:
        body += formatting.session_restarted_note()
    pushed_sha = None
    if implement and before is not None:
        commit_text, pushed_sha = _commit_and_report(project, prompt, before, thread_ts)
        body += commit_text
    _post_chunks(client, channel_id, thread_ts, body)

    # CI は分単位でかかるので、結果を待たずにここで応答を返し、完了後に追って投稿する。
    # ワーカーを占有しないよう専用スレッドで待つ（max_workers=1 でも他の依頼が詰まらない）。
    if pushed_sha:
        threading.Thread(
            target=_watch_ci,
            args=(settings, client, project, channel_id, thread_ts, pushed_sha),
            daemon=True,
            name="ci-watch",
        ).start()


def _watch_ci(settings, client, project, channel_id, thread_ts, sha) -> None:
    try:
        text = _ci_report(settings, project, sha)
    except Exception:  # noqa: BLE001 - 監視スレッドの最終防衛線
        logger.exception("CI の監視中に例外が発生しました project=%s", project.key)
        return
    if text:
        _post_chunks(client, channel_id, thread_ts, text.lstrip())


def _commit_and_report(
    project: Project, prompt: str, before: git_ops.Snapshot, thread_ts: str
) -> tuple[str, str | None]:
    """この実行で変わったファイルを commit し、報告文を組み立てる（CLAUDE.md §21）。

    Claude Code には Bash を渡していないので、commit は必ずここを通る。
    失敗しても変更は作業ツリーに残るため、その旨を伝えて握り潰さない。
    """
    cwd = project.working_directory
    try:
        changed, preexisting = git_ops.changed_since(cwd, before)
    except (git_ops.GitError, OSError) as exc:
        logger.exception("変更の検出に失敗しました project=%s", project.key)
        return formatting.commit_failed_note(str(exc)), None

    if not changed:
        return (
            formatting.no_changes_note() + formatting.skipped_dirty_note(preexisting),
            None,
        )

    try:
        commit = git_ops.commit_changes(
            cwd, changed, git_ops.build_commit_message(project.name, prompt)
        )
    except (git_ops.GitError, OSError) as exc:
        logger.exception("コミットに失敗しました project=%s", project.key)
        return formatting.commit_failed_note(str(exc)), None

    if not commit.committed:
        return (
            formatting.no_changes_note() + formatting.skipped_dirty_note(preexisting),
            None,
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

    push_text, pushed = _push_and_report(project, thread_ts)
    return report + push_text, (commit.sha if pushed else None)


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


def _post_chunks(client, channel_id: str, thread_ts: str, text: str) -> None:
    chunks = formatting.split_for_slack(text)
    if not chunks:
        chunks = ["(空の応答が返されました)"]
    for chunk in chunks:
        try:
            client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts, text=chunk, mrkdwn=True
            )
        except Exception:  # noqa: BLE001
            logger.exception("Slack への投稿に失敗しました channel=%s", channel_id)
            return
