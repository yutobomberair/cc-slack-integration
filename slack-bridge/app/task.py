"""1回の依頼を表す型と、実行に必要な共有物。

以前はワーカー関数へ14個の引数を位置指定で渡していた。渡す順番を間違えても
型が合えば動いてしまい、引数を1つ足すだけで3箇所の修正が必要だった。
「何を渡すか」を型にして、増えても呼び出し側が壊れないようにする。

2つに分けているのは寿命が違うため。

* ``BridgeContext`` … 起動時に1度作って使い回す（設定・記録・ロック）
* ``TaskRequest`` … 依頼1件ごとに作って捨てる
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app import artifacts, inbox, task_mode
from app import workdir as workdir_mod
from app.concurrency import ThreadLocks
from app.config import Project, Settings
from app.session_store import SessionStore


@dataclass(frozen=True)
class BridgeContext:
    """起動時に1度だけ作る、実行に必要な共有物。

    Slack クライアントはここに含めない。Bolt がイベントごとに渡してくるもので、
    寿命が ``BridgeContext`` と違うため。
    """

    settings: Settings
    sessions: SessionStore
    outputs: artifacts.ArtifactStore
    thread_locks: ThreadLocks
    #: スレッドごとの作業階層（``cd:``）。省略可能にすると渡し忘れが
    #: 実行時の AttributeError になるので必須にしておく。
    workdirs: workdir_mod.WorkdirStore


@dataclass(frozen=True)
class TaskRequest:
    """受理した依頼1件。

    ``prompt`` はディレクティブ（``調査:`` や ``haiku:``）を剥がした本文。
    ``message_ts`` は「処理を開始しました」の投稿で、進捗表示がこれを
    ``chat.update`` で書き換えていく（投稿に失敗していれば None）。
    """

    project: Project
    prompt: str
    channel_id: str
    thread_ts: str
    profile_key: str
    message_ts: str | None = None
    continued: bool = False
    model: str | None = None
    attached: list[inbox.Saved] = field(default_factory=list)
    #: Claude を起動する階層。プロジェクト直下か、その配下（``cd:`` で選ぶ）。
    #: None ならプロジェクト直下。
    workdir: Path | None = None

    @property
    def implement(self) -> bool:
        return self.profile_key == task_mode.IMPLEMENT

    @property
    def working_directory(self) -> Path:
        """Claude を起動する場所。

        git の操作先とは別物。git は ``git_ops.repo_root`` が返すルートで動かす
        （porcelain はルート基準でパスを返すのに git add は cwd 基準で解釈するため）。
        ``_share/`` と ``_inbox/`` もここではなくプロジェクト直下に置く。
        """
        return self.workdir or self.project.working_directory

    @property
    def workdir_label(self) -> str:
        """プロジェクト直下からの相対表記。直下なら空文字。"""
        return workdir_mod.relative_label(self.project.working_directory, self.working_directory)
