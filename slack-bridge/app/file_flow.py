"""ファイル受け渡しのフロー（``slack_handler`` から切り出し）。

受け渡しには方向が3つある。判定と整形は ``app/artifacts.py`` と ``app/inbox.py``
が持ち、ここは「いつ何を投稿するか」だけを決める。

* Slack → ローカル … ``_inbox/`` へ降ろし、置き場所を Claude へ伝える
* ローカル → Slack（Claude の意思） … ``_share/`` にあるものを自動で添付する
* ローカル → Slack（利用者の指定） … 一覧を出し、``共有: 1`` で取り出させる

``_share/`` と ``_inbox/`` は **``cd:`` で階層を変えても常にプロジェクト直下**に置く。
階層ごとに散ると ``共有: 1`` の番号と実体の対応が追えなくなるため。Claude が下の
階層で動いている場合は、前置きで ``../_share/`` のような相対パスを伝える。
"""

from __future__ import annotations

import logging

from app import artifacts, inbox, share_queue
from pathlib import Path

from app.config import Project, Settings
from app.reply import Thread
from app.task import TaskRequest

logger = logging.getLogger(__name__)


def build_prompt(task: TaskRequest) -> str:
    """Claude へ渡す実際の依頼文を組み立てる。

    ブリッジしか知らない事情を2つだけ前置きする。どちらも伝えなければ
    Claude には知りようがない。

    * Slack から降ろした添付ファイルの置き場所
    * ``_share/`` に置けば利用者へ届くということ

    ``cd:`` で下の階層にいるときは、そこから見た相対パスで伝える。
    プロジェクト直下の ``_share/`` を ``_share/`` と伝えてしまうと、Claude は
    自分の cwd の下に作ってしまい、添付されない。
    """
    prefix = ""
    if task.attached:
        prefix += inbox.prompt_note(task.attached, task.workdir_label)
    if task.implement:
        prefix += share_queue.share_dir_note(task.workdir_label)
    return prefix + task.prompt


def save_attachments(thread: Thread, project: Project, files: list[dict], token: str):
    """Slack の添付を ``_inbox/`` へ降ろし、結果を報告する。返り値は保存結果。"""
    saved = inbox.save_files(files or [], project.working_directory, token)
    if saved:
        thread.post(inbox.report(saved))
    return saved


def auto_share(thread: Thread, settings: Settings, project: Project) -> None:
    """``_share/`` にあるファイルをスレッドへ添付し、送れたものを退避する。

    ``_share/`` は送信の待ち行列。そこにある = まだ送っていない、と等しいので、
    送信できたら ``_share/sent/`` へ移して行列から外す。失敗したものは残るため
    次の実行で自動的に再試行される。
    """
    pending = share_queue.list_share_files(project.working_directory)
    if not pending:
        return
    items = artifacts.describe(project.working_directory, pending)
    logger.info(
        "_share/ のファイルを添付します project=%s thread_ts=%s files=%d",
        project.key,
        thread.thread_ts,
        len(items),
    )
    sent = _upload(thread, project, items)
    if sent is None:
        return

    stuck = share_queue.archive_sent(
        project.working_directory, sent, settings.runtime.share_retention_days
    )
    if stuck:
        # 退避できないと次の実行でもう一度届く。黙って重複させない。
        logger.warning("退避できなかった送信済みファイル: %s", stuck)

    thread.post(share_queue.auto_share_report(sent, items, stuck))


def report_outputs(
    thread: Thread,
    outputs: artifacts.ArtifactStore,
    project: Project,
    produced: list[str],
    base: Path | None = None,
) -> None:
    """出力ファイルの一覧を投稿し、番号で取り出せるよう記録する。

    ファイル自体は添付しない。実装タスクではソースが十数ファイル変わることがあり、
    それを全部送るとスレッドが埋まって肝心の回答が読めなくなるため。
    """
    if not produced:
        return
    # git 由来のパスはリポジトリルート基準なので、解決の基準も揃える必要がある
    # （プロジェクト直下と一致しないことがある）。
    items = artifacts.describe(base or project.working_directory, produced)
    outputs.record(
        thread.channel_id,
        thread.thread_ts,
        project.key,
        [i.path for i in items],
        base=str(base) if base else None,
    )
    logger.info(
        "出力ファイルを記録しました project=%s thread_ts=%s files=%d",
        project.key,
        thread.thread_ts,
        len(items),
    )
    thread.post_long(artifacts.listing_message(items))


def share_on_request(
    thread: Thread,
    outputs: artifacts.ArtifactStore,
    project: Project,
    argument: str,
) -> None:
    """``共有: 1`` を処理する。Claude は起動しない。"""
    recorded = outputs.get(thread.channel_id, thread.thread_ts)
    if not recorded:
        thread.post(artifacts.no_artifacts_message())
        return

    # 記録時と同じ基準で解決する（git 由来のパスはリポジトリルート基準）。
    base = outputs.get_base(thread.channel_id, thread.thread_ts)
    items = artifacts.describe(Path(base) if base else project.working_directory, recorded)
    chosen, unresolved = artifacts.select(items, argument)
    if unresolved and not chosen:
        thread.post(artifacts.unresolved_message(unresolved, items))
        return

    logger.info(
        "ファイルを共有します project=%s thread_ts=%s files=%d",
        project.key,
        thread.thread_ts,
        len(chosen),
    )
    sent = _upload(thread, project, chosen, base=Path(base) if base else None)
    if sent is None:
        return

    report = artifacts.upload_report(sent, chosen)
    if unresolved:
        missing = "`, `".join(unresolved)
        report += f"\n:warning: `{missing}` は見つかりませんでした。"
    thread.post(report)


def _upload(thread: Thread, project: Project, items, base: Path | None = None) -> list[str] | None:
    """アップロードの共通部分。返り値が None なら打ち切り（原因は投稿済み）。

    スコープ不足は何度試しても同じように失敗するので、手順を伝えて止める。
    """
    try:
        return artifacts.upload(
            thread.client,
            thread.channel_id,
            thread.thread_ts,
            base or project.working_directory,
            items,
        )
    except artifacts.UploadError as exc:
        logger.error("ファイルの共有に失敗しました project=%s: %s", project.key, exc)
        thread.post(f":x: {exc}")
        return None
