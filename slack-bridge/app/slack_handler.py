"""Slack イベントの受信と振り分け（CLAUDE.md §5, §12, §19）。

重要な前提が2つある。

1. Slack はハンドラが数秒以内に ack しないとイベントを再送する。Claude Code の実行は
   分単位でかかるため、ack はイベント受信直後に返し、実処理はワーカースレッドへ逃がす。
2. それでも再送は起こりうる（ネットワーク切断・再接続など）。``event_id`` で冪等化
   しないと同一タスクが多重起動する。調査タスクなら無駄な課金で済むが、実装タスク
   では同じ変更が二重に走る。

このファイルが持つのは「受け付けるかどうか」の判断だけ。受け付けたあとは
``app/task_flow.py`` へ渡す。実行・コミット・ファイル受け渡しはそちらにある。
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor

from slack_bolt import App

from app import artifacts, file_flow, formatting, project_router, task_flow, task_mode
from app import workdir as workdir_mod
from app.concurrency import SeenEvents, ThreadLocks
from app.config import Settings
from app.reply import Thread
from app.session_store import SessionStore
from app.task import BridgeContext, TaskRequest

logger = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@[UW][A-Z0-9]+>")
_HSPACE_RE = re.compile(r"[ \t]+")

#: 添付だけ送られたときの既定の依頼。
_ATTACHMENT_ONLY_PROMPT = "添付されたファイルを確認して、内容を要約してください。"


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
    ctx = BridgeContext(
        settings=settings,
        sessions=SessionStore(settings.runtime.session_store_path),
        outputs=artifacts.ArtifactStore(settings.runtime.artifact_store_path),
        workdirs=workdir_mod.WorkdirStore(settings.runtime.workdir_store_path),
        # 同一スレッドの二重実行を防ぐ。Claude Code のセッションは並行更新できない。
        thread_locks=ThreadLocks(),
    )
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
        thread = Thread(client, channel_id, thread_ts)

        task = _accept(ctx, thread, event)
        if task is None:
            return

        logger.info(
            "受理 project=%s channel=%s thread_ts=%s prompt_len=%d continued=%s "
            "profile=%s model=%s workdir=%s",
            task.project.key,
            channel_id,
            thread_ts,
            len(task.prompt),
            task.continued,
            task.profile_key,
            task.model or settings.runtime.model or "(既定)",
            task.workdir_label or "(直下)",
        )
        executor.submit(task_flow.run_task, ctx, client, task)

    app._slack_bridge_executor = executor  # 終了時に片付けられるよう保持
    return app


def _accept(ctx: BridgeContext, thread: Thread, event: dict) -> TaskRequest | None:
    """受け付けられる依頼なら ``TaskRequest`` を返す。

    断る場合はその理由をスレッドへ投稿して None を返す。関門が一列に並ぶので、
    増えたときも順番に足すだけで済む。
    """
    settings = ctx.settings

    route = project_router.route(settings, thread.channel_id)
    if not route.is_known:
        # 未登録チャンネルでは Claude Code を起動しない（CLAUDE.md §15）。
        logger.warning("未登録チャンネルからのメンション channel_id=%s", thread.channel_id)
        thread.post(project_router.unknown_channel_message(thread.channel_id))
        return None
    project = route.project

    files = event.get("files") or []
    prompt = strip_mentions(event.get("text", ""))
    if not prompt and files:
        prompt = _ATTACHMENT_ONLY_PROMPT
    if not prompt:
        thread.post(formatting.empty_request_message())
        return None

    # 添付は依頼文の判定より先に降ろす。「これを見て」と添付だけ送られた場合でも
    # 保存自体は済ませたいため。
    attached = file_flow.save_attachments(thread, project, files, settings.bot_token)

    # 「共有: 1」は出力済みファイルの受け渡しであって開発依頼ではない。
    # Claude を起動する前にここで片付ける（課金もセッション更新も発生しない）。
    share_argument = artifacts.parse_share_request(prompt)
    if share_argument is not None:
        file_flow.share_on_request(thread, ctx.outputs, project, share_argument)
        return None

    # 「cd: movie」も同じ。作業階層を記録するだけで Claude は起動しない。
    cd_argument = workdir_mod.parse_directive(prompt)
    if cd_argument is not None:
        _change_workdir(ctx, thread, project, cd_argument)
        return None

    mode = task_mode.resolve(
        prompt,
        project.profile,
        project.allow_implement,
        allowed_models=list(settings.runtime.allowed_models),
    )
    if mode.unknown_model:
        # 打ち間違いを黙って既定モデルで走らせない。指定したつもりの利用者が
        # 気づけないまま別のモデルで課金されるのを避ける。
        thread.post(
            task_mode.unknown_model_message(
                mode.unknown_model, list(settings.runtime.allowed_models)
            )
        )
        return None
    if mode.denied:
        logger.warning(
            "コード変更が要求されましたが許可されていません project=%s", project.key
        )
        thread.post(task_mode.implement_not_allowed_message(project.name))
        return None
    if not mode.prompt:
        thread.post(formatting.empty_after_directive_message())
        return None

    # このスレッドで選ばれている階層。cd: していなければプロジェクト直下。
    label = ctx.workdirs.get(thread.channel_id, thread.thread_ts)
    try:
        chosen_dir = workdir_mod.resolve(project.working_directory, label)
    except workdir_mod.WorkdirError:
        # 記録した階層が消えている（リポジトリを整理した等）。直下へ戻して続ける。
        logger.warning("記録された作業階層が使えません project=%s label=%s", project.key, label)
        ctx.workdirs.set(thread.channel_id, thread.thread_ts, project.key, "")
        label, chosen_dir = "", project.working_directory

    continued = settings.runtime.session_continuation and ctx.sessions.is_started(
        thread.channel_id, thread.thread_ts, label
    )
    # 開始メッセージの ts を控える。以降これを chat.update で書き換えていく。
    message_ts = thread.post(
        formatting.start_message(
            project.name,
            continued=continued,
            implement=mode.profile_key == task_mode.IMPLEMENT,
            model=mode.model or settings.runtime.model,
            workdir=label,
        )
    )
    return TaskRequest(
        project=project,
        prompt=mode.prompt,
        channel_id=thread.channel_id,
        thread_ts=thread.thread_ts,
        profile_key=mode.profile_key,
        message_ts=message_ts,
        continued=continued,
        model=mode.model,
        attached=attached,
        workdir=chosen_dir,
    )


def _change_workdir(ctx: BridgeContext, thread: Thread, project, requested: str) -> None:
    """``cd: movie`` を処理する。Claude は起動しない。

    引数が空なら現在の階層を答える。プロジェクトの外を指す指定は弾く。
    """
    options = workdir_mod.candidates(project.working_directory)
    current = ctx.workdirs.get(thread.channel_id, thread.thread_ts)

    if not requested:
        thread.post(workdir_mod.current_message(project.name, current, options))
        return

    try:
        target = workdir_mod.resolve(project.working_directory, requested)
    except workdir_mod.WorkdirError as exc:
        logger.info("作業階層の指定を拒否しました project=%s 指定=%s", project.key, requested)
        thread.post(workdir_mod.error_message(project.name, str(exc), options))
        return

    label = workdir_mod.relative_label(project.working_directory, target)
    ctx.workdirs.set(thread.channel_id, thread.thread_ts, project.key, label)
    logger.info(
        "作業階層を変更しました project=%s thread_ts=%s workdir=%s",
        project.key,
        thread.thread_ts,
        label or "(直下)",
    )
    thread.post(workdir_mod.changed_message(project.name, label))
