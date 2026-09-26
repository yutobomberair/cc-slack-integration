"""``_share/`` — Claude から利用者へファイルを渡すための送信待ち行列。

``app/artifacts.py`` から切り出した。あちらは「この実行で何が出力されたか」を
数えて一覧にする話で、こちらは「送りたいものを置く場所」の話。混ざっていると
どちらの都合で書かれたコードなのか読めなくなる。

考え方は郵便の差出箱。

* ``_share/`` にあるもの = まだ送っていないもの
* 送れたら ``_share/sent/`` へ移して行列から外す
* 送信に失敗したものは残るので、次の実行で自動的に再試行される
* 状態は記録ファイルではなくディレクトリそのもの

以前は送信済みの内容ハッシュを記録する方式だったが、記録の上限を超えたときと
記録ファイルを失ったときに重複して届く経路が残った。ディレクトリを状態にすれば
どちらも起こらない。

削除ではなく移動なのは、``_share/`` のファイルにコピー元があるとは限らないため。
Claude が成果物を最初から ``_share/`` に書くことがある。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from app.artifacts import Artifact, human_size
from app.paths import unique_path

logger = logging.getLogger(__name__)

#: Claude が「Slack へ送りたい」ものを置くディレクトリ（プロジェクト直下）。
SHARE_DIR = "_share"

#: 送信できたファイルの退避先（``_share/`` の直下）。走査対象からは外す。
SENT_DIR = "sent"


def list_share_files(cwd: Path) -> list[str]:
    """``_share/`` にある未送信ファイルの相対パス。``_share/sent/`` は含めない。

    git 管理の有無に関わらず常にこちらで見る。``.gitignore`` に ``_share/`` が
    書かれていると git の差分には出てこないが、それでも共有はできてほしいため。

    内容の比較はしない。``_share/`` は送信待ちの置き場で、送れたものは
    ``archive_sent`` が退かすので、「そこにある = まだ送っていない」と等しい。
    """
    directory = Path(cwd) / SHARE_DIR
    if not directory.is_dir():
        return []
    sent_dir = directory / SENT_DIR
    found: list[str] = []
    for entry in directory.rglob("*"):
        try:
            if not entry.is_file():
                continue
            if sent_dir in entry.parents:
                continue
        except OSError:
            continue
        found.append(entry.relative_to(Path(cwd)).as_posix())
    return sorted(found)


def archive_sent(cwd: Path, paths: list[str], retention_days: int = 7) -> list[str]:
    """送信できたファイルを待ち行列から外す。返り値は外せなかったパス。

    ``retention_days`` が正なら ``_share/sent/`` へ移し、その日数を過ぎた退避分を
    掃除する。0 以下なら移さずに削除する。

    既定で少し残すのは、``_share/`` に直接生成されたファイルはコピー元が無く、
    即削除するとPC上から実体が消えるため。送信に成功していれば Slack 側には
    残っているので致命的ではないが、気づいて拾い直す猶予があるほうが安全。

    移動・削除に失敗しても送信自体は済んでいるので例外にはしない。ただし次の実行で
    もう一度届いてしまうため、呼び出し側が警告を出せるよう結果を返す。
    """
    root = Path(cwd)
    failed: list[str] = []
    for rel in paths:
        source = root / rel
        try:
            if retention_days <= 0:
                source.unlink()
                continue
            # _share/nested/a.md -> _share/sent/nested/a.md（階層を保つ）
            inner = Path(rel).relative_to(SHARE_DIR)
            destination = root / SHARE_DIR / SENT_DIR / inner
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(unique_path(destination))
        except OSError:
            logger.exception("送信済みファイルを退避できませんでした: %s", rel)
            failed.append(rel)

    if retention_days > 0:
        prune_sent(cwd, retention_days)
    return failed


def prune_sent(cwd: Path, retention_days: int) -> int:
    """``_share/sent/`` のうち保持期間を過ぎたものを消す。返り値は消した件数。

    ここを掃除しないと退避先が延々と膨らむ。送信済みのものしか入っていないので、
    期間を過ぎたら消してよい（Slack 側には残っている）。
    """
    directory = Path(cwd) / SHARE_DIR / SENT_DIR
    if not directory.is_dir():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for entry in directory.rglob("*"):
        try:
            if not entry.is_file() or entry.stat().st_mtime >= cutoff:
                continue
            entry.unlink()
            removed += 1
        except OSError:
            logger.debug("退避分を削除できませんでした: %s", entry, exc_info=True)
    if removed:
        logger.info("%s/%s から %d 件を削除しました（保持 %d 日）",
                    SHARE_DIR, SENT_DIR, removed, retention_days)
    return removed


def is_shared_path(rel: str) -> bool:
    return rel == SHARE_DIR or rel.startswith(SHARE_DIR + "/")


def split_shared(paths: list[str]) -> tuple[list[str], list[str]]:
    """(``_share/`` のもの, それ以外) に分ける。"""
    shared = [p for p in paths if is_shared_path(p)]
    rest = [p for p in paths if not is_shared_path(p)]
    return shared, rest


def auto_share_report(
    sent: list[str], items: list[Artifact], stuck: list[str] | None = None
) -> str:
    """``_share/`` から自動添付したときの報告。

    ``stuck`` は送信はできたが ``_share/sent/`` へ退避できなかったもの。放っておくと
    次の実行でもう一度届くので、黙って重複させずに知らせる。
    """
    if not items:
        return ""
    lines = []
    if sent:
        lines.append(f":outbox_tray: `{SHARE_DIR}/` のファイル {len(sent)} 件を添付しました。")
    for item in items:
        if item.missing or item.too_large:
            reason = "削除済み" if item.missing else f"{human_size(item.size)} は大きすぎます"
            lines.append(f":warning: `{item.path}` は送れませんでした（{reason}）。")
    skipped = sum(1 for i in items if i.missing or i.too_large)
    unsent = len(items) - len(sent) - skipped
    if unsent > 0:
        lines.append(
            f":warning: {unsent} 件を送れませんでした。"
            f"`{SHARE_DIR}/` に残してあるので、次の実行で再試行します。"
        )
    for path in stuck or []:
        lines.append(
            f":warning: `{path}` を `{SHARE_DIR}/{SENT_DIR}/` へ退避できませんでした。"
            "このままだと次の実行でもう一度届きます。"
        )
    return "\n".join(lines)


def share_dir_note() -> str:
    """Claude へ ``_share/`` の使い方を伝える一文。

    伝えないと存在を知りようがないので、実装モードの依頼文の前に付ける。
    """
    return (
        "[この依頼は Slack 経由です。"
        f"利用者に渡したいファイルは `{SHARE_DIR}/` へ置けば、"
        "このスレッドへ自動で添付されます]\n\n"
    )
